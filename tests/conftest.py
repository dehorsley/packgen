"""Shared fixtures: parsing helpers and a C toolchain for the end-to-end tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

from packgen import parse_source
from packgen.generators import json as json_gen
from packgen.generators import pack as pack_gen
from packgen.model import Schema

HEADERS = Path(__file__).parent / "headers"

WARNINGS = [
    "-Wall",
    "-Wextra",
    "-Wconversion",
    "-Wsign-conversion",
    "-Wshadow",
    "-Werror",
]

# The generated code targets ISO C99.  -pedantic is what actually holds it to
# that: without it a compiler quietly accepts its own extensions.
BASE_CFLAGS = ["-std=c99", "-pedantic", *WARNINGS]

#: Every standard the generated code claims to build under.
C_STANDARDS = ["c99", "c11", "c17"]

# The default `undefined` group leaves out the checks a packer is most likely
# to trip.  Only clang has most of these -- gcc rejects an unknown
# -fsanitize= argument outright -- so they are probed rather than assumed.
BASE_SANITIZERS = "address,undefined"
EXTRA_SANITIZERS = ["integer", "implicit-conversion", "local-bounds", "nullability"]

PROBE = "int main(void) { return 0; }\n"


def find_compiler() -> str | None:
    for name in (os.environ.get("CC"), "cc", "clang", "gcc"):
        if name and shutil.which(name):
            return name
    return None


@cache
def sanitizer_flags(compiler: str) -> tuple[str, ...]:
    """The strongest sanitiser set this particular compiler accepts.

    Compilers disagree about what lives under ``-fsanitize=``: gcc has neither
    ``integer`` nor ``implicit-conversion`` nor ``nullability``, and spells
    clang's ``local-bounds`` as ``bounds``.  Handing it one of those is a hard
    error, so each is tried against a trivial program and kept only if it both
    compiles and links.
    """
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "probe.c"
        source.write_text(PROBE, encoding="utf-8")

        def accepts(names: str) -> bool:
            result = subprocess.run(
                [
                    compiler,
                    f"-fsanitize={names}",
                    "-fno-sanitize-recover=all",
                    str(source),
                    "-o",
                    str(Path(tmp) / "probe"),
                ],
                capture_output=True,
            )
            return result.returncode == 0

        if not accepts(BASE_SANITIZERS):
            # No usable sanitiser at all; the tests still check behaviour.
            return ()

        enabled = [BASE_SANITIZERS]
        for extra in EXTRA_SANITIZERS:
            if accepts(",".join([*enabled, extra])):
                enabled.append(extra)
        return (f"-fsanitize={','.join(enabled)}", "-fno-sanitize-recover=all")


def find_jansson() -> tuple[list[str], list[str]] | None:
    """``(cflags, ldflags)`` for jansson, or ``None`` if it is not installed."""
    if not shutil.which("pkg-config"):
        return None
    try:
        cflags = subprocess.run(
            ["pkg-config", "--cflags", "jansson"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        ldflags = subprocess.run(
            ["pkg-config", "--libs", "jansson"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except (subprocess.CalledProcessError, OSError):
        return None
    return cflags, ldflags


@dataclass
class Project:
    """A directory holding a header and the code packgen generated from it."""

    directory: Path
    header_name: str
    schema: Schema
    sources: list[Path]

    @property
    def header(self) -> Path:
        return self.directory / self.header_name


@pytest.fixture(scope="session")
def compiler() -> str:
    cc = find_compiler()
    if cc is None:
        pytest.skip("no C compiler available")
    return cc


@pytest.fixture(scope="session")
def jansson() -> tuple[list[str], list[str]]:
    flags = find_jansson()
    if flags is None:
        pytest.skip("jansson not installed")
    return flags


@pytest.fixture
def generate(tmp_path: Path):
    """Write a header plus its generated code into a temp directory."""

    def _generate(
        source: str,
        *,
        endian: str = "big",
        name: str = "packet",
        with_json: bool = False,
    ) -> Project:
        guard = f"{name.upper()}_H"
        text = f"#ifndef {guard}\n#define {guard}\n{source}\n#endif\n"
        header_name = f"{name}.h"
        (tmp_path / header_name).write_text(text, encoding="utf-8")

        schema = parse_source(text, filename=header_name)
        sources: list[Path] = []

        unpack = pack_gen.generate(
            schema,
            source_header=header_name,
            generated_header=f"{name}_unpack.h",
            endian=endian,
        )
        (tmp_path / f"{name}_unpack.h").write_text(unpack.header, encoding="utf-8")
        (tmp_path / f"{name}_unpack.c").write_text(unpack.source, encoding="utf-8")
        sources.append(tmp_path / f"{name}_unpack.c")

        if with_json:
            marshal = json_gen.generate(
                schema,
                source_header=header_name,
                generated_header=f"{name}_json.h",
            )
            (tmp_path / f"{name}_json.h").write_text(marshal.header, encoding="utf-8")
            (tmp_path / f"{name}_json.c").write_text(marshal.source, encoding="utf-8")
            sources.append(tmp_path / f"{name}_json.c")

        return Project(
            directory=tmp_path,
            header_name=header_name,
            schema=schema,
            sources=sources,
        )

    return _generate


@pytest.fixture
def build(compiler: str):
    """Compile a project plus a ``main.c`` into a runnable binary."""

    def _build(
        project: Project,
        main: str,
        *,
        extra_cflags: tuple[str, ...] = (),
        extra_ldflags: tuple[str, ...] = (),
        sanitize: bool = True,
    ) -> Path:
        main_path = project.directory / "main.c"
        main_path.write_text(main, encoding="utf-8")
        binary = project.directory / "a.out"

        command = [
            compiler,
            *BASE_CFLAGS,
            *(sanitizer_flags(compiler) if sanitize else []),
            f"-I{project.directory}",
            *extra_cflags,
            *[str(s) for s in project.sources],
            str(main_path),
            "-o",
            str(binary),
            *extra_ldflags,
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            pytest.fail(
                f"generated code did not compile:\n{result.stderr}\n"
                f"command: {' '.join(command)}"
            )
        return binary

    return _build
