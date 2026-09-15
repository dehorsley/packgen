#!/usr/bin/env python3
"""Assert that every line and branch of the generated C actually executes.

A sanitiser only checks the code it runs, so the sanitiser results in the
test suite are only worth as much as the coverage behind them.  This builds
the generated code with clang's source-based coverage, runs the harness that
drives every entry point, and fails if anything was missed.

    python tools/check_c_coverage.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packgen import parse_header
from packgen.generators import pack as pack_gen
from tests.reference import exercise_all

HEADER = Path(__file__).resolve().parent.parent / "tests" / "headers" / "dbbcpacket.h"


def llvm_tool(name: str) -> list[str]:
    """Locate an llvm tool, whether bare, versioned, or behind xcrun."""
    if shutil.which(name):
        return [name]
    for suffix in ("-19", "-18", "-17", "-16", "-15", "-14"):
        if shutil.which(name + suffix):
            return [name + suffix]
    if shutil.which("xcrun"):
        return ["xcrun", name]
    raise SystemExit(f"could not find {name}; install llvm")


def check(directory: Path, endian: str) -> bool:
    schema = parse_header(HEADER)
    generated = pack_gen.generate(
        schema,
        source_header="dbbcpacket.h",
        generated_header="dbbcpacket_unpack.h",
        endian=endian,
    )
    shutil.copy(HEADER, directory / "dbbcpacket.h")
    (directory / "dbbcpacket_unpack.h").write_text(generated.header)
    (directory / "dbbcpacket_unpack.c").write_text(generated.source)
    (directory / "main.c").write_text(exercise_all(schema, "dbbcpacket_unpack.h"))

    compiler = os.environ.get("CC", "clang")
    subprocess.run(
        [
            compiler,
            "-std=c99",
            "-O0",
            "-fprofile-instr-generate",
            "-fcoverage-mapping",
            f"-I{directory}",
            str(directory / "dbbcpacket_unpack.c"),
            str(directory / "main.c"),
            "-o",
            str(directory / "cov"),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [str(directory / "cov")],
        check=True,
        capture_output=True,
        env={**os.environ, "LLVM_PROFILE_FILE": str(directory / "c.profraw")},
    )
    subprocess.run(
        [
            *llvm_tool("llvm-profdata"),
            "merge",
            "-sparse",
            str(directory / "c.profraw"),
            "-o",
            str(directory / "c.profdata"),
        ],
        check=True,
        capture_output=True,
    )
    report = subprocess.run(
        [
            *llvm_tool("llvm-cov"),
            "export",
            str(directory / "cov"),
            f"-instr-profile={directory / 'c.profdata'}",
            str(directory / "dbbcpacket_unpack.c"),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    totals = json.loads(report)["data"][0]["totals"]
    label = f"{endian:<7}"
    ok = True
    for kind in ("regions", "functions", "lines", "branches"):
        percent = totals[kind]["percent"]
        if percent < 100.0:
            ok = False
        print(f"  {label} {kind:<10} {percent:6.2f}%")
    return ok


def main() -> int:
    failed = False
    with tempfile.TemporaryDirectory() as tmp:
        for endian in ("big", "little"):
            directory = Path(tmp) / endian
            directory.mkdir()
            if not check(directory, endian):
                failed = True
    if failed:
        print(
            "\nGenerated C is not fully covered: the sanitiser runs in the "
            "test suite are only checking part of it."
        )
        return 1
    print("\nEvery region, function, line and branch of the generated C executes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
