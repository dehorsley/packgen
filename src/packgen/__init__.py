from __future__ import annotations

from pathlib import Path

from pycparser import c_ast, parse_file

from packgen.packgen import (
    JsonMarshalGenerator,
    LengthGenerator,
    UnmarshalGenerator,
)


def parse_file_with_fake_libc(filename: str) -> c_ast.FileAST:
    ast = parse_file(
        filename,
        use_cpp=True,
        cpp_path="gcc",
        cpp_args=[
            "-E",
            "-I%s" % (Path(__file__).resolve().parent / "fake_libc_include"),
        ],  # type: ignore
    )
    return ast


__all__ = [
    "UnmarshalGenerator",
    "JsonMarshalGenerator",
    "LengthGenerator",
    "parse_file_with_fake_libc",
]
