# This file covered by GPL 3 license
# C. David Horsley 2025
"""Prove the JSON marshallers handle allocation failure correctly.

Every ``marshal_json_X`` has a NULL-return path for every allocation it
makes, and no test reaches any of them: jansson does not fail on demand,
so the error handling in the generated code is written but never
executed.  That has been the softest spot in the suite.

CBMC reaches those paths by construction.  This replaces jansson with a
model whose allocators fail *nondeterministically* -- so CBMC explores
every combination of which allocations succeed and which fail, all at
once -- and tracks ownership so it can tell a leak from a clean unwind.

Three properties, over every failure interleaving:

  no leak on failure   marshal_json_X returned NULL, so nothing it
                       allocated may still be live
  clean on success     after the caller's json_decref, nothing is live
  no double free       decref is never handed an already freed object

The model reflects two things about jansson's real contract.  The
``*_set_new`` functions steal the reference they are given and release
it even when they fail, which is why the generated error paths only ever
drop ``root``.  And ownership nests -- an array inside an array inside
the root object -- so freeing the root has to free transitively, which
the model does by sweeping to a fixed point.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packgen.generators import json as json_gen
from packgen.generators import pack as pack_gen
from packgen.parser import parse_header

CHECKS = [
    "--bounds-check",
    "--pointer-check",
    "--unwinding-assertions",
]

#: Stands in for <jansson.h>.  Only what the generated code uses.
JANSSON_H = """\
#ifndef PACKGEN_FAKE_JANSSON_H
#define PACKGEN_FAKE_JANSSON_H

#include <stddef.h>

typedef long long json_int_t;

typedef struct json_t {
    int id;
} json_t;

json_t *json_object(void);
json_t *json_array(void);
json_t *json_string(const char *value);
json_t *json_stringn(const char *value, size_t len);
json_t *json_integer(json_int_t value);
json_t *json_real(double value);
json_t *json_boolean_impl(int value);
json_t *json_null(void);
void json_decref(json_t *json);
int json_object_set_new(json_t *object, const char *key, json_t *value);
int json_array_append_new(json_t *array, json_t *value);

#define json_boolean(v) json_boolean_impl((v) ? 1 : 0)

extern int packgen_live;

#endif
"""

#: The model itself.  MAX_OBJECTS bounds how many json_t a single
#: marshal_json_* may allocate; DEPTH bounds how deeply they nest.
JANSSON_C = """\
#include <assert.h>
#include "jansson.h"

#define MAX_OBJECTS 48
#define DEPTH 8

#define FREE 0
#define LIVE 1
#define OWNED 2

static json_t pool[MAX_OBJECTS];
static int state[MAX_OBJECTS];
static int owner[MAX_OBJECTS];
static int next_slot = 0;

int packgen_live = 0;

int nondet_int(void);

static json_t *packgen_alloc(void)
{
    int i;

    /* The whole point: any allocation may fail, at any time. */
    if (nondet_int()) return NULL;
    if (next_slot >= MAX_OBJECTS) return NULL;

    i = next_slot++;
    pool[i].id = i;
    state[i] = LIVE;
    owner[i] = -1;
    packgen_live++;
    return &pool[i];
}

json_t *json_object(void) { return packgen_alloc(); }
json_t *json_array(void) { return packgen_alloc(); }
json_t *json_string(const char *v) { (void)v; return packgen_alloc(); }
json_t *json_stringn(const char *v, size_t n)
{ (void)v; (void)n; return packgen_alloc(); }
json_t *json_integer(json_int_t v) { (void)v; return packgen_alloc(); }
json_t *json_real(double v) { (void)v; return packgen_alloc(); }
json_t *json_boolean_impl(int v) { (void)v; return packgen_alloc(); }
json_t *json_null(void) { return packgen_alloc(); }

void json_decref(json_t *json)
{
    int i, j, pass;

    if (json == NULL) return;
    i = json->id;

    /* Handing decref something already released is the bug this
       whole model exists to rule out. */
    assert(state[i] != FREE);

    state[i] = FREE;
    packgen_live--;

    /* Ownership nests, so free transitively: sweep to a fixed point. */
    for (pass = 0; pass < DEPTH; pass++)
        for (j = 0; j < MAX_OBJECTS; j++)
            if (state[j] == OWNED && state[owner[j]] == FREE) {
                state[j] = FREE;
                packgen_live--;
            }
}

/* jansson's *_set_new steal the reference they are handed, and release
   it even when they fail.  Getting this wrong in the model would make
   the generated error paths look buggy when they are not. */
static int packgen_steal(json_t *container, json_t *value)
{
    if (value == NULL) return -1;
    if (container == NULL) { json_decref(value); return -1; }
    if (nondet_int()) { json_decref(value); return -1; }

    state[value->id] = OWNED;
    owner[value->id] = container->id;
    return 0;
}

int json_object_set_new(json_t *object, const char *key, json_t *value)
{
    (void)key;
    return packgen_steal(object, value);
}

int json_array_append_new(json_t *array, json_t *value)
{
    return packgen_steal(array, value);
}
"""


def harness(name: str, pack_header: str, json_header: str) -> str:
    return f"""\
#include <assert.h>
#include "{pack_header}"
#include "{json_header}"

uint8_t nondet_uint8_t(void);

int main(void)
{{
    uint8_t in[len_{name}];
    {name} t;
    json_t *result;
    size_t i;

    for (i = 0; i < len_{name}; i++) in[i] = nondet_uint8_t();
    assert(unmarshal_{name}(&t, in, sizeof in) == (ptrdiff_t)len_{name});

    result = marshal_json_{name}(&t);

    if (result == NULL) {{
        /* Every allocation it made must have been released. */
        assert(packgen_live == 0);
    }} else {{
        json_decref(result);
        assert(packgen_live == 0);
    }}

    return 0;
}}
"""


def verify(header_path: Path, timeout: int, only: str | None) -> int:
    schema = parse_header(header_path)
    pack_header = f"{header_path.stem}_unpack.h"
    json_header = f"{header_path.stem}_json.h"
    packed = pack_gen.generate(
        schema, source_header=header_path.name, generated_header=pack_header
    )
    marshalled = json_gen.generate(
        schema, source_header=header_path.name, generated_header=json_header
    )

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)

        def write(name: str, text: str) -> None:
            (work / name).write_text(text, encoding="utf-8")

        write(header_path.name, header_path.read_text(encoding="utf-8"))
        write(pack_header, packed.header)
        write(f"{header_path.stem}_unpack.c", packed.source)
        write(json_header, marshalled.header)
        write(f"{header_path.stem}_json.c", marshalled.source)
        write("jansson.h", JANSSON_H)
        write("jansson_model.c", JANSSON_C)

        sources = [
            str(work / f"{header_path.stem}_unpack.c"),
            str(work / f"{header_path.stem}_json.c"),
            str(work / "jansson_model.c"),
        ]

        for struct in schema:
            if only and struct.name != only:
                continue
            main = work / f"harness_{struct.name}.c"
            main.write_text(
                harness(struct.name, pack_header, json_header), encoding="utf-8"
            )
            print(f"  {struct.name:<28} ", end="", flush=True)
            try:
                result = subprocess.run(
                    ["cbmc", str(main), *sources, f"-I{work}", *CHECKS],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                print(f"TIMEOUT after {timeout}s")
                failures += 1
                continue
            if "VERIFICATION SUCCESSFUL" in result.stdout:
                count = ""
                for line in result.stdout.splitlines():
                    if line.startswith("** 0 of"):
                        count = line.split()[3]
                print(f"proved ({count} checks)")
            else:
                print("FAILED")
                for line in result.stdout.splitlines():
                    if line.endswith(": FAILURE"):
                        print(f"      {line}")
                failures += 1
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("headers", nargs="*", type=Path)
    parser.add_argument(
        "--random",
        type=int,
        default=0,
        metavar="N",
        help="also verify N randomly generated headers (seeds 0..N-1)",
    )
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--struct", default=None)
    args = parser.parse_args(argv)

    if not shutil.which("cbmc"):
        print("cbmc not installed; see https://www.cprover.org/cbmc/", file=sys.stderr)
        return 2

    failures = 0
    for header in args.headers:
        print(f"{header} (jansson allocation failure)")
        failures += verify(header, args.timeout, args.struct)

    if args.random:
        from tests.random_headers import random_header

        with tempfile.TemporaryDirectory() as tmp:
            for seed in range(args.random):
                text, _ = random_header(seed, structs=2, max_fields=3, max_bytes=64)
                path = Path(tmp) / f"random_{seed}.h"
                path.write_text(text, encoding="utf-8")
                print(f"random header seed {seed} (jansson allocation failure)")
                failures += verify(path, args.timeout, args.struct)
    print()
    print("all properties proved" if not failures else f"{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
