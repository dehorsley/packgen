#!/usr/bin/env python3
"""Sweep randomly generated headers against the Python reference.

The committed test suite runs a handful of seeds so it stays quick.  This
runs as many as you ask for, in parallel, and is what CI uses.  A failure
prints the seed, which reproduces the header exactly.

    python tools/fuzz_sweep.py --cases 400
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packgen import parse_source
from packgen.generators import pack as pack_gen
from tests.random_headers import random_header
from tests.reference import (
    expected_output,
    harness,
    leaves,
    pack,
    random_values,
)

CFLAGS = [
    "-std=c99",
    "-pedantic",
    "-Wall",
    "-Wextra",
    "-Wconversion",
    "-Wsign-conversion",
    "-Wshadow",
    "-Werror",
    "-fsanitize=address,undefined",
    "-fno-sanitize-recover=all",
]


def one_case(args: tuple[int, str]) -> str | None:
    """Return a description of the failure, or ``None`` if the case passed."""
    seed, endian = args
    where = f"seed={seed} endian={endian}"
    source, top = random_header(seed, structs=5, max_fields=7)
    guarded = f"#ifndef P_H\n#define P_H\n{source}\n#endif\n"

    try:
        schema = parse_source(guarded, filename="packet.h")
        generated = pack_gen.generate(
            schema,
            source_header="packet.h",
            generated_header="packet_unpack.h",
            endian=endian,
        )
    except Exception as exc:
        return f"{where}: generate raised {type(exc).__name__}: {exc}"

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        (directory / "packet.h").write_text(guarded)
        (directory / "packet_unpack.h").write_text(generated.header)
        (directory / "packet_unpack.c").write_text(generated.source)

        leafs = list(leaves(schema, top))
        values = random_values(leafs, seed=seed)
        wire = pack(leafs, values, endian)
        if len(wire) != schema.size_of(top):
            return (
                f"{where}: reference says {len(wire)} bytes, "
                f"schema says {schema.size_of(top)}"
            )

        (directory / "main.c").write_text(
            harness(schema, top, leafs, "packet_unpack.h")
        )
        compiler = os.environ.get("CC", "cc")
        build = subprocess.run(
            [
                compiler,
                *CFLAGS,
                f"-I{directory}",
                str(directory / "packet_unpack.c"),
                str(directory / "main.c"),
                "-o",
                str(directory / "a.out"),
            ],
            capture_output=True,
            text=True,
        )
        if build.returncode != 0:
            first = build.stderr.strip().splitlines()[:1]
            return f"{where}: did not compile: {first}"

        run = subprocess.run(
            [str(directory / "a.out")], input=wire, capture_output=True
        )
        if run.returncode != 0:
            return f"{where}: exited {run.returncode}: {run.stderr[:200]!r}"
        if run.stdout.decode() != expected_output(leafs, values):
            return f"{where}: decoded the wrong values\n{source}"
        if run.stderr != wire:
            return f"{where}: did not re-encode exactly\n{source}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=100, help="number of seeds")
    parser.add_argument("--start", type=int, default=0, help="first seed")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    args = parser.parse_args()

    cases = [
        (seed, endian)
        for seed in range(args.start, args.start + args.cases)
        for endian in ("big", "little")
    ]
    print(f"{len(cases)} cases across {args.jobs} workers...", flush=True)

    failures = []
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        for done, result in enumerate(pool.map(one_case, cases, chunksize=4), 1):
            if result is not None:
                failures.append(result)
                print(f"  FAIL {result}", flush=True)
            if done % 200 == 0:
                print(f"  {done}/{len(cases)}", flush=True)

    print(f"\n{len(cases)} cases, {len(failures)} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
