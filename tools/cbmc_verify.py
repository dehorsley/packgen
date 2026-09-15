# This file covered by GPL 3 license
# C. David Horsley 2025
"""Prove properties of the generated pack/unpack code with CBMC.

The differential tests in tests/reference.py check the wire format against
an independent implementation at a handful of byte patterns.  This proves
the same properties for *every* byte pattern at once.

CBMC is a bounded model checker: `nondet_uint8_t()` returns an
unconstrained byte, so filling a buffer with it reasons about all
256**len_X buffers simultaneously.  Bounded model checking is normally
incomplete -- a bug past the unwind limit is invisible -- but every loop
packgen emits has a constant trip count fixed by the struct layout, never
by the data, so --unwinding-assertions passes and the result is a proof
rather than a sampled approximation.

Two properties per struct:

  idempotence  f(b) = marshal(unmarshal(b)) satisfies f(f(b)) == f(b).
               Holds for every struct.
  identity     f(b) == b.  The stronger claim, and only true when the
               struct holds no bool: a wire byte of 0x05 decodes to true
               and re-encodes as 0x01, so a bool normalises on the first
               pass.  That is the one documented asymmetry in the format,
               and this is what makes it precise.

plus CBMC's automatic checks -- array bounds, pointer validity, signed and
unsigned arithmetic overflow -- over the same universally quantified input.

Note --conversion-check is deliberately NOT enabled.  It asks whether a
conversion is value preserving, and packing is deliberately lossy:
(uint8_t)(x >> 8) is a byte extraction, well defined in C99 6.3.1.3p2 but
not value preserving.  The one conversion that really is implementation
defined, (int32_t)(uint32_t)v when v exceeds INT32_MAX, is the documented
choice to decode signed integers through their unsigned counterpart.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packgen.generators import pack as pack_gen
from packgen.model import BOOL_TYPES, Schema, Struct
from packgen.parser import parse_header

CHECKS = [
    "--bounds-check",
    "--pointer-check",
    "--signed-overflow-check",
    "--unsigned-overflow-check",
    "--unwinding-assertions",
]


def holds_a_bool(
    schema: Schema, struct: Struct, seen: frozenset[str] = frozenset()
) -> bool:
    """Whether `struct` contains a bool anywhere, however deeply nested."""
    if struct.name in seen:
        return False
    seen = seen | {struct.name}
    for field in struct.fields:
        type_ = schema.resolve(field.type)
        if type_ in BOOL_TYPES:
            return True
        if schema.is_struct(type_) and holds_a_bool(schema, schema[type_], seen):
            return True
    return False


def harness(name: str, header: str, identity: bool) -> str:
    """A CBMC main() proving the round-trip property for one struct."""
    strong = (
        "    /* No bool anywhere in this struct, so the round trip is exact. */\n"
        f"    for (i = 0; i < len_{name}; i++) assert(once[i] == in[i]);\n"
        if identity
        else "    /* Holds a bool, which normalises on the first pass. */\n"
    )
    return f"""\
#include <assert.h>
#include "{header}"

uint8_t nondet_uint8_t(void);

int main(void)
{{
    uint8_t in[len_{name}], once[len_{name}], twice[len_{name}];
    {name} a, b;
    size_t i;

    for (i = 0; i < len_{name}; i++) in[i] = nondet_uint8_t();

    assert(unmarshal_{name}(&a, in,    sizeof in)    == (ptrdiff_t)len_{name});
    assert(marshal_{name}(  &a, once,  sizeof once)  == (ptrdiff_t)len_{name});
    assert(unmarshal_{name}(&b, once,  sizeof once)  == (ptrdiff_t)len_{name});
    assert(marshal_{name}(  &b, twice, sizeof twice) == (ptrdiff_t)len_{name});

{strong}
    /* f(f(b)) == f(b), for every struct. */
    for (i = 0; i < len_{name}; i++) assert(twice[i] == once[i]);

    /* The guards the entry points promise. */
    assert(unmarshal_{name}(&a, in, len_{name} - 1) == -1);
    assert(marshal_{name}(&a, once, len_{name} - 1) == -1);
    assert(unmarshal_{name}(NULL, in, sizeof in) == -1);
    assert(marshal_{name}(NULL, once, sizeof once) == -1);

    return 0;
}}
"""


def verify(header_path: Path, endian: str, timeout: int, only: str | None) -> int:
    schema = parse_header(header_path)
    generated_header = f"{header_path.stem}_unpack.h"
    generated = pack_gen.generate(
        schema,
        source_header=header_path.name,
        generated_header=generated_header,
        endian=endian,
    )

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / header_path.name).write_text(
            header_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (work / generated_header).write_text(generated.header, encoding="utf-8")
        source = work / f"{header_path.stem}_unpack.c"
        source.write_text(generated.source, encoding="utf-8")

        for struct in schema:
            if only and struct.name != only:
                continue
            size = schema.size_of(struct)
            identity = not holds_a_bool(schema, struct)
            main = work / f"harness_{struct.name}.c"
            main.write_text(
                harness(struct.name, generated_header, identity), encoding="utf-8"
            )
            label = "identity" if identity else "idempotence"
            print(f"  {struct.name:<28} {size:>6} B  {label:<12} ", end="", flush=True)
            try:
                result = subprocess.run(
                    ["cbmc", str(main), str(source), f"-I{work}", *CHECKS],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                print(f"TIMEOUT after {timeout}s")
                failures += 1
                continue
            if "VERIFICATION SUCCESSFUL" in result.stdout:
                total = ""
                for line in result.stdout.splitlines():
                    if line.startswith("** 0 of"):
                        total = line.split()[3]
                print(f"proved ({total} checks)")
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
    parser.add_argument("--endian", choices=["big", "little"], default="big")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--struct", default=None, help="verify only this struct")
    args = parser.parse_args(argv)

    if not shutil.which("cbmc"):
        print("cbmc not installed; see https://www.cprover.org/cbmc/", file=sys.stderr)
        return 2

    failures = 0
    for header in args.headers:
        print(f"{header} ({args.endian} endian)")
        failures += verify(header, args.endian, args.timeout, args.struct)

    if args.random:
        from tests.random_headers import random_header

        with tempfile.TemporaryDirectory() as tmp:
            for seed in range(args.random):
                text, _ = random_header(seed)
                path = Path(tmp) / f"random_{seed}.h"
                path.write_text(text, encoding="utf-8")
                print(f"random header seed {seed} ({args.endian} endian)")
                failures += verify(path, args.endian, args.timeout, args.struct)
    print()
    print("all properties proved" if not failures else f"{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
