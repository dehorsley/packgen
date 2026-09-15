"""The command line interface."""

from __future__ import annotations

import hashlib
from pathlib import Path
from textwrap import dedent

import pytest

from packgen.__main__ import main

HEADER = dedent(
    """
    #ifndef PACKET_H
    #define PACKET_H
    #include <stdint.h>

    typedef struct {
        uint32_t a;
        char b[4];
    } s_t;
    #endif
    """
)


@pytest.fixture
def header(tmp_path: Path) -> Path:
    path = tmp_path / "packet.h"
    path.write_text(HEADER, encoding="utf-8")
    return path


def test_writes_all_four_files_next_to_the_header(header: Path):
    assert main([str(header)]) == 0
    for name in (
        "packet_unpack.h",
        "packet_unpack.c",
        "packet_json.h",
        "packet_json.c",
    ):
        assert (header.parent / name).exists()


def test_output_directory(header: Path, tmp_path: Path):
    out = tmp_path / "build" / "generated"
    assert main([str(header), "-o", str(out)]) == 0
    assert (out / "packet_unpack.c").exists()
    # The generated code includes the original header by name, so the caller
    # has to put it on the include path themselves.
    assert '#include "packet.h"' in (out / "packet_unpack.h").read_text()


def test_no_json(header: Path):
    assert main([str(header), "--no-json"]) == 0
    assert (header.parent / "packet_unpack.c").exists()
    assert not (header.parent / "packet_json.c").exists()


def test_defaults_to_big_endian(header: Path):
    assert main([str(header)]) == 0
    source = (header.parent / "packet_unpack.c").read_text()
    assert "((uint32_t)p[3] << 0)" in source


def test_little_endian_flag(header: Path):
    assert main([str(header), "--little"]) == 0
    source = (header.parent / "packet_unpack.c").read_text()
    assert "((uint32_t)p[0] << 0)" in source


def test_big_endian_flag_overrides_little(header: Path):
    assert main([str(header), "--little", "--big"]) == 0
    source = (header.parent / "packet_unpack.c").read_text()
    assert "((uint32_t)p[3] << 0)" in source


def test_output_is_deterministic(header: Path, tmp_path: Path):
    """Byte-identical output for identical input.

    Generated code gets checked into trees and diffed in review; output that
    shifts between runs makes that unusable, and it is an easy property to
    lose to set or dict iteration order.
    """
    digests = set()
    for run in range(4):
        out = tmp_path / f"run{run}"
        assert main([str(header), "-o", str(out)]) == 0
        digests.add(
            tuple(
                (p.name, hashlib.sha256(p.read_bytes()).hexdigest())
                for p in sorted(out.iterdir())
            )
        )
    assert len(digests) == 1


def test_json_failure_points_at_no_json(tmp_path: Path, capsys, monkeypatch):
    """A JSON-only problem should not read as a total failure.

    No field currently reaches this: the JSON generator accepts everything
    the pack generator does, and `char *` besides. It is a safety net for
    any JSON-only restriction added later, so it is driven with a stub.
    """
    from packgen import UnsupportedTypeError
    from packgen.__main__ import json_gen

    def boom(*args, **kwargs):
        raise UnsupportedTypeError("some JSON-only limitation")

    monkeypatch.setattr(json_gen, "generate", boom)

    path = tmp_path / "packet.h"
    path.write_text("typedef struct { uint32_t a; } s_t;\n", encoding="utf-8")

    assert main([str(path)]) == 1
    error = capsys.readouterr().err
    assert "some JSON-only limitation" in error
    assert "--no-json" in error
    # Nothing is written, so there is no half-generated tree to clean up.
    assert not (tmp_path / "packet_unpack.c").exists()


def test_if_zero_structs_are_not_emitted(tmp_path: Path):
    """The generated code must not name a type the header does not define."""
    path = tmp_path / "dead.h"
    path.write_text(
        "#if 0\n"
        "typedef struct { uint8_t old; uint16_t junk; } dead_t;\n"
        "#endif\n"
        "typedef struct { uint8_t x; } live_t;\n",
        encoding="utf-8",
    )
    assert main([str(path), "--no-json"]) == 0
    for generated in ("dead_unpack.h", "dead_unpack.c"):
        text = (tmp_path / generated).read_text()
        assert "dead_t" not in text
        assert "live_t" in text


def test_missing_file(tmp_path: Path, capsys):
    assert main([str(tmp_path / "nope.h")]) == 1
    assert "packgen:" in capsys.readouterr().err


def test_header_with_no_structs(tmp_path: Path, capsys):
    path = tmp_path / "empty.h"
    path.write_text("#define FOO 1\n", encoding="utf-8")
    assert main([str(path)]) == 1
    assert "no typedef'd structs" in capsys.readouterr().err


def test_unsupported_construct_reports_and_writes_nothing(tmp_path: Path, capsys):
    path = tmp_path / "bad.h"
    path.write_text("typedef struct { uint32_t a : 3; } s_t;\n", encoding="utf-8")
    assert main([str(path)]) == 1
    assert "bit field" in capsys.readouterr().err
    assert not (tmp_path / "bad_unpack.c").exists()


def test_unpackable_field_does_not_leave_a_half_written_file(tmp_path: Path, capsys):
    path = tmp_path / "bad.h"
    path.write_text(
        "typedef struct { uint32_t a; } good_t;\n"
        "typedef struct { uint32_t *p; } bad_t;\n",
        encoding="utf-8",
    )
    assert main([str(path)]) == 1
    assert "pointer" in capsys.readouterr().err
    assert not (tmp_path / "bad_unpack.c").exists()


class TestBanner:
    """Generated code has to say what produced it.

    Generated output gets committed, so a year later the banner is the
    only way to tell which packgen wrote a file and at which byte order.
    """

    def test_names_the_version_and_the_source_header(self, header: Path):
        assert main([str(header)]) == 0
        for name in ("packet_unpack.h", "packet_unpack.c", "packet_json.c"):
            head = (header.parent / name).read_text().splitlines()[0]
            assert "Generated by packgen" in head
            assert f"from {header.name}" in head

    @pytest.mark.parametrize(
        ("flag", "expected"),
        [("--big", "big endian"), ("--little", "little endian")],
    )
    def test_records_the_byte_order(self, header: Path, flag: str, expected: str):
        assert main([str(header), flag]) == 0
        for name in ("packet_unpack.h", "packet_unpack.c"):
            assert f"Byte order: {expected}" in (header.parent / name).read_text()

    def test_carries_no_timestamp(self, header: Path, tmp_path: Path):
        """A timestamp would make every regeneration a spurious diff."""
        assert main([str(header)]) == 0
        first = (header.parent / "packet_unpack.c").read_text()
        other = tmp_path / "again"
        assert main([str(header), "-o", str(other)]) == 0
        assert (other / "packet_unpack.c").read_text() == first


class TestCheck:
    def test_passes_on_freshly_generated_output(self, header: Path, capsys):
        assert main([str(header)]) == 0
        capsys.readouterr()
        assert main([str(header), "--check"]) == 0
        assert capsys.readouterr().err == ""

    def test_fails_when_a_file_is_stale(self, header: Path, capsys):
        assert main([str(header)]) == 0
        target = header.parent / "packet_unpack.c"
        target.write_text(target.read_text() + "\n/* edited by hand */\n")
        capsys.readouterr()
        assert main([str(header), "--check"]) == 1
        assert "out of date" in capsys.readouterr().err

    def test_fails_when_a_file_is_missing(self, header: Path, capsys):
        assert main([str(header)]) == 0
        (header.parent / "packet_json.c").unlink()
        capsys.readouterr()
        assert main([str(header), "--check"]) == 1
        assert "missing" in capsys.readouterr().err

    def test_detects_the_wrong_byte_order(self, header: Path, capsys):
        """The whole reason the banner records it."""
        assert main([str(header), "--big"]) == 0
        capsys.readouterr()
        assert main([str(header), "--little", "--check"]) == 1
        assert "out of date" in capsys.readouterr().err

    def test_writes_nothing(self, header: Path):
        assert main([str(header), "--check"]) == 1
        assert not (header.parent / "packet_unpack.c").exists()


class TestOverwriteGuard:
    def test_refuses_to_clobber_a_hand_written_file(self, header: Path, capsys):
        target = header.parent / "packet_unpack.c"
        target.write_text("/* hand written */\nint mine;\n")
        capsys.readouterr()
        assert main([str(header)]) == 1
        assert "not generated by packgen" in capsys.readouterr().err
        assert target.read_text() == "/* hand written */\nint mine;\n"

    def test_leaves_every_other_file_alone_too(self, header: Path):
        """All or nothing -- a refusal must not half-generate the tree."""
        (header.parent / "packet_unpack.c").write_text("/* hand written */\n")
        assert main([str(header)]) == 1
        assert not (header.parent / "packet_json.c").exists()
        assert not (header.parent / "packet_unpack.h").exists()

    def test_force_overwrites(self, header: Path):
        target = header.parent / "packet_unpack.c"
        target.write_text("/* hand written */\n")
        assert main([str(header), "--force"]) == 0
        assert "Generated by packgen" in target.read_text()

    def test_regenerating_over_its_own_output_is_fine(self, header: Path):
        assert main([str(header)]) == 0
        assert main([str(header)]) == 0

    def test_leaves_no_temporary_files(self, header: Path):
        assert main([str(header)]) == 0
        assert not list(header.parent.glob("*packgen-tmp*"))
