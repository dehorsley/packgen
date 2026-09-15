# This file covered by GPL 3 license
# C. David Horsley 2025
"""Packed-length constants for each struct."""

from __future__ import annotations

from packgen.model import Schema
from packgen.writer import Writer


def length_name(struct_name: str) -> str:
    return f"len_{struct_name}"


def write_declarations(writer: Writer, schema: Schema) -> None:
    """Length macros, for the generated header.

    These are macros rather than ``const size_t`` objects so that callers can
    use them as array bounds and in static assertions, which an object with
    external linkage cannot be.
    """
    for struct in schema:
        writer.line(
            f"#define {length_name(struct.name)} ((size_t){schema.size_of(struct)})"
        )


def write_definitions(writer: Writer, schema: Schema) -> None:
    """Static assertions tying each length back to its struct."""
    for struct in schema:
        size = schema.size_of(struct)
        # The packed form has no padding, so the in-memory struct is never
        # smaller.  Catches a header that drifted away from its generated code.
        writer.line(
            f"PACKGEN_STATIC_ASSERT(sizeof({struct.name}) >= {size}, "
            f"{struct.name}_is_smaller_than_its_packed_form);"
        )
