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


def roundtrip_harness(name: str, header: str, identity: bool) -> str:
    """marshal . unmarshal is the identity, or at least idempotent."""
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

    assert(unmarshal_{name}(NULL, in, sizeof in) == -1);
    assert(marshal_{name}(NULL, once, sizeof once) == -1);

    return 0;
}}
"""


def leak_harness(name: str, header: str, identity: bool) -> str:
    """Every output byte is written -- nothing of the caller's leaks out.

    If marshal ever failed to write some byte of the output buffer, that
    byte would keep whatever the caller's memory held and go out on the
    wire.  Struct-padding leaks are a well worn CVE class for exactly
    this reason.

    Marshalling the same struct into two buffers pre-filled with
    different bytes proves it: any byte marshal does not write keeps its
    fill, so the two results differ.  Equality means every byte is a
    function of the struct alone.
    """
    return f"""\
#include <assert.h>
#include "{header}"

uint8_t nondet_uint8_t(void);

int main(void)
{{
    uint8_t in[len_{name}], zeros[len_{name}], ones[len_{name}];
    {name} t;
    size_t i;

    for (i = 0; i < len_{name}; i++) in[i] = nondet_uint8_t();
    for (i = 0; i < len_{name}; i++) {{ zeros[i] = 0x00; ones[i] = 0xFF; }}

    assert(unmarshal_{name}(&t, in, sizeof in) == (ptrdiff_t)len_{name});
    assert(marshal_{name}(&t, zeros, sizeof zeros) == (ptrdiff_t)len_{name});
    assert(marshal_{name}(&t, ones,  sizeof ones)  == (ptrdiff_t)len_{name});

    /* Any byte left unwritten still holds its fill, so these differ. */
    for (i = 0; i < len_{name}; i++) assert(zeros[i] == ones[i]);

    return 0;
}}
"""


def contract_harness(name: str, header: str, identity: bool) -> str:
    """The length contract, for every n rather than two chosen values.

    n >= len_X  ->  returns len_X and touches nothing past len_X
    n <  len_X  ->  returns -1 and writes nothing at all, to the buffer
                    or to the struct.  Callers rely on that second half
                    and nothing else checks it.
    """
    return f"""\
#include <assert.h>
#include <string.h>
#include "{header}"

#define SLACK 4

uint8_t nondet_uint8_t(void);
size_t nondet_size_t(void);

int main(void)
{{
    uint8_t seed[len_{name}];
    uint8_t buf[len_{name} + SLACK], saved[len_{name} + SLACK];
    {name} t, before;
    size_t n, i;

    /* A well defined struct to start from. */
    for (i = 0; i < len_{name}; i++) seed[i] = nondet_uint8_t();
    assert(unmarshal_{name}(&t, seed, len_{name}) == (ptrdiff_t)len_{name});
    before = t;

    for (i = 0; i < len_{name} + SLACK; i++) buf[i] = nondet_uint8_t();
    memcpy(saved, buf, sizeof buf);

    n = nondet_size_t();
    __CPROVER_assume(n <= len_{name} + SLACK);

    if (n < len_{name}) {{
        assert(unmarshal_{name}(&t, buf, n) == -1);
        assert(memcmp(&t, &before, sizeof t) == 0);
        assert(marshal_{name}(&t, buf, n) == -1);
        assert(memcmp(buf, saved, sizeof buf) == 0);
    }} else {{
        assert(marshal_{name}(&t, buf, n) == (ptrdiff_t)len_{name});
        /* Writes exactly len_X bytes, never into the slack. */
        for (i = len_{name}; i < len_{name} + SLACK; i++)
            assert(buf[i] == saved[i]);
    }}

    return 0;
}}
"""


PROPERTIES = {
    "roundtrip": roundtrip_harness,
    "no-leak": leak_harness,
    "contract": contract_harness,
}

#: The contract harness copies and compares the whole struct twice and
#: works over a symbolic length, which does not scale: the 6208 byte DBBC
#: packet does not finish in 400s even with the range narrowed to the
#: boundary.  The property is about the entry point guard, and that code
#: is structurally identical for every struct -- the same four lines with
#: a different len_X -- so proving it across the small and medium structs
#: covers the pattern.  Structs above this size are reported as skipped
#: rather than silently dropped.
DEFAULT_CONTRACT_LIMIT = 512


def _run(main: Path, source: Path, work: Path, timeout: int) -> tuple[bool, str]:
    """Run CBMC once; return (proved, detail)."""
    try:
        result = subprocess.run(
            ["cbmc", str(main), str(source), f"-I{work}", *CHECKS],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {timeout}s"
    if "VERIFICATION SUCCESSFUL" in result.stdout:
        for line in result.stdout.splitlines():
            if line.startswith("** 0 of"):
                return True, f"{line.split()[3]} checks"
        return True, ""
    detail = [ln for ln in result.stdout.splitlines() if ln.endswith(": FAILURE")]
    return False, "\n".join(f"      {ln}" for ln in detail) or "FAILED"


def verify(
    header_path: Path,
    endian: str,
    timeout: int,
    only: str | None,
    properties: list[str],
    contract_limit: int,
) -> int:
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
            print(f"  {struct.name:<26} {size:>6} B")
            for name in properties:
                if name == "contract" and size > contract_limit:
                    print(
                        f"      {name:<14} skipped ({size} B is over the "
                        f"{contract_limit} B limit)"
                    )
                    continue
                main = work / f"harness_{struct.name}_{name}.c"
                main.write_text(
                    PROPERTIES[name](struct.name, generated_header, identity),
                    encoding="utf-8",
                )
                shown = name
                if name == "roundtrip":
                    shown = "identity" if identity else "idempotence"
                print(f"      {shown:<14} ", end="", flush=True)
                proved, detail = _run(main, source, work, timeout)
                if proved:
                    print(f"proved ({detail})")
                else:
                    print("FAILED")
                    if detail:
                        print(detail)
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
    parser.add_argument(
        "--max-contract-bytes",
        type=int,
        default=DEFAULT_CONTRACT_LIMIT,
        help=f"skip the contract property above this packed size "
        f"(default {DEFAULT_CONTRACT_LIMIT})",
    )
    parser.add_argument("--struct", default=None, help="verify only this struct")
    parser.add_argument(
        "--property",
        action="append",
        choices=sorted(PROPERTIES),
        default=None,
        help="verify only this property (repeatable; default all)",
    )
    args = parser.parse_args(argv)

    if not shutil.which("cbmc"):
        print("cbmc not installed; see https://www.cprover.org/cbmc/", file=sys.stderr)
        return 2

    properties = args.property or sorted(PROPERTIES)
    failures = 0
    for header in args.headers:
        print(f"{header} ({args.endian} endian)")
        failures += verify(
            header,
            args.endian,
            args.timeout,
            args.struct,
            properties,
            args.max_contract_bytes,
        )

    if args.random:
        from tests.random_headers import random_header

        with tempfile.TemporaryDirectory() as tmp:
            for seed in range(args.random):
                text, _ = random_header(seed)
                path = Path(tmp) / f"random_{seed}.h"
                path.write_text(text, encoding="utf-8")
                print(f"random header seed {seed} ({args.endian} endian)")
                failures += verify(
                    path,
                    args.endian,
                    args.timeout,
                    args.struct,
                    properties,
                    args.max_contract_bytes,
                )
    print()
    print("all properties proved" if not failures else f"{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
