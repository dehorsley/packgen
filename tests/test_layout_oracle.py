"""packgen's parse, checked against the C compiler's view of the header.

See tests/layout_oracle.py for why this exists: every other test walks
the Schema packgen produced, so none of them can see a parser bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from packgen import parse_source
from packgen.parser import parse_header
from tests.conftest import HEADERS
from tests.layout_oracle import check
from tests.random_headers import random_header


def test_the_real_world_header_matches_the_compiler(compiler: str, tmp_path: Path):
    header = HEADERS / "dbbcpacket.h"
    schema = parse_header(header)
    assert check(compiler, header, schema, tmp_path) == []


@pytest.mark.parametrize("seed", range(6))
def test_random_headers_match_the_compiler(compiler: str, tmp_path: Path, seed: int):
    text, _ = random_header(seed)
    header = tmp_path / "random.h"
    header.write_text(text, encoding="utf-8")
    assert check(compiler, header, parse_header(header), tmp_path) == []


def _written(tmp_path: Path, source: str) -> Path:
    header = tmp_path / "h.h"
    header.write_text(source, encoding="utf-8")
    return header


class TestTheOracleActuallyCatchesThings:
    """A check that never fires is indistinguishable from one that works.

    Each of these corrupts the Schema the way a parser bug would, and
    asserts the compiler contradicts it.
    """

    SOURCE = (
        "#include <stdint.h>\n"
        "typedef struct { uint16_t a; uint32_t b; uint8_t c[4]; } s_t;\n"
    )

    def test_reordered_fields_are_caught(self, compiler: str, tmp_path: Path):
        """The exact bug that passes 60 differential cases and every proof."""
        header = _written(tmp_path, self.SOURCE)
        schema = parse_source(self.SOURCE.encode())
        struct = schema["s_t"]
        object.__setattr__(struct, "fields", tuple(reversed(struct.fields)))
        problems = check(compiler, header, schema, tmp_path)
        assert problems, "reversing the field order went unnoticed"
        assert any("overlap" in p or "unexplained" in p for p in problems)

    def test_a_wrong_array_bound_is_caught(self, compiler: str, tmp_path: Path):
        header = _written(tmp_path, self.SOURCE)
        schema = parse_source(self.SOURCE.encode())
        struct = schema["s_t"]
        wrong = struct.fields[2].__class__(name="c", type="uint8_t", dims=(8,))
        object.__setattr__(struct, "fields", (*struct.fields[:2], wrong))
        problems = check(compiler, header, schema, tmp_path)
        assert any("is 4 bytes, but packgen expects 8" in p for p in problems)

    def test_a_misresolved_type_is_caught(self, compiler: str, tmp_path: Path):
        header = _written(tmp_path, self.SOURCE)
        schema = parse_source(self.SOURCE.encode())
        struct = schema["s_t"]
        wrong = struct.fields[1].__class__(name="b", type="uint16_t")
        object.__setattr__(
            struct, "fields", (struct.fields[0], wrong, *struct.fields[2:])
        )
        problems = check(compiler, header, schema, tmp_path)
        assert any("is 4 bytes, but packgen expects 2" in p for p in problems)

    def test_a_missed_field_is_caught(self, compiler: str, tmp_path: Path):
        header = _written(tmp_path, self.SOURCE)
        schema = parse_source(self.SOURCE.encode())
        struct = schema["s_t"]
        object.__setattr__(struct, "fields", (struct.fields[0], struct.fields[2]))
        problems = check(compiler, header, schema, tmp_path)
        assert any("unexplained" in p for p in problems)

    def test_the_uncorrupted_schema_is_clean(self, compiler: str, tmp_path: Path):
        header = _written(tmp_path, self.SOURCE)
        schema = parse_source(self.SOURCE.encode())
        assert check(compiler, header, schema, tmp_path) == []
