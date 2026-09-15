"""Differential test: random headers, generated C, checked against Python.

The C emitter and ``tests.reference`` were written separately, so agreement
between them on a header neither was written for is real evidence. A
disagreement prints the seed, which reproduces the case exactly.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.random_headers import random_header
from tests.reference import expected_output, harness, leaves, pack, random_values

SEEDS = list(range(12))


@pytest.mark.parametrize("endian", ["big", "little"])
@pytest.mark.parametrize("seed", SEEDS)
def test_random_header_matches_the_reference(generate, build, seed, endian):
    source, top = random_header(seed)
    project = generate(source, endian=endian)

    leafs = list(leaves(project.schema, top))
    values = random_values(leafs, seed=seed)
    wire = pack(leafs, values, endian)

    assert len(wire) == project.schema.size_of(top), (
        f"seed={seed}: reference and schema disagree on size\n{source}"
    )

    binary = build(project, harness(project.schema, top, leafs, "packet_unpack.h"))
    result = subprocess.run([str(binary)], input=wire, capture_output=True)
    assert result.returncode == 0, f"seed={seed} exited {result.returncode}\n{source}"

    assert result.stdout.decode() == expected_output(leafs, values), (
        f"seed={seed} decoded the wrong values\n{source}"
    )
    assert result.stderr == wire, f"seed={seed} did not re-encode exactly\n{source}"
