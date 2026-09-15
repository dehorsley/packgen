# This file covered by GPL 3 license
# C. David Horsley 2025
"""Pieces shared by the code generators."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

from packgen.errors import UnsupportedTypeError
from packgen.model import Field, Schema
from packgen.writer import Writer

#: Types that occupy one byte and can be block-copied without conversion.
BYTE_TYPES = frozenset({"char", "int8_t", "uint8_t"})


@dataclass(frozen=True)
class GeneratedPair:
    """A generated ``.h``/``.c`` pair."""

    header: str
    source: str


def check_no_macro_collisions(schema: Schema, names: Iterable[str]) -> None:
    """Refuse to emit a name the source header already defines as a macro.

    Generated code includes the source header, so emitting ``len_X`` when
    the header already has ``#define len_X 999`` is a macro
    redefinition: a hard error under ``-Werror``, and worse without it,
    where it is only a warning and packgen's definition silently wins --
    quietly changing what the caller's own macro means.

    packgen already knows every macro the header defines, so this is
    catchable up front rather than in the user's build.
    """
    clashes = sorted(set(names) & schema.macros)
    if not clashes:
        return
    listed = ", ".join(repr(name) for name in clashes)
    plural = len(clashes) > 1
    raise UnsupportedTypeError(
        f"the header already defines {listed} as "
        f"{'macros' if plural else 'a macro'}, and packgen needs "
        f"{'those names' if plural else 'that name'} for the code it "
        f"generates; rename to avoid the collision"
    )


def check_packable(schema: Schema, field: Field, struct_name: str) -> str:
    """Validate a field and return its resolved type name."""
    if field.is_pointer:
        raise UnsupportedTypeError(
            f"in struct {struct_name!r}: field {field.name!r} is a pointer; "
            f"packed structs cannot contain pointers"
        )
    # Raises for unknown or unpackable types, and warms the size cache.
    schema.size_of_field(field)
    return schema.resolve(field.type)


@contextmanager
def array_loops(writer: Writer, field: Field) -> Iterator[tuple[str, int]]:
    """Open one loop per array dimension, however many there are.

    Yields ``(accessor, depth)``: the expression naming a single element, and
    the number of loops opened.
    """
    with ExitStack() as stack:
        accessor = f"t->{field.name}"
        depth = 0
        for extent in field.dims:
            index = f"i{depth}"
            depth += 1
            stack.enter_context(
                writer.block(f"for (size_t {index} = 0; {index} < {extent}; {index}++)")
            )
            accessor += f"[{index}]"
        yield accessor, depth


def unpack_expression(type_: str, size: int, endian: str, buffer_: str = "p") -> str:
    """A C expression reading ``size`` big/little endian bytes from ``buffer_``."""
    if size == 1:
        return f"({type_}){buffer_}[0]"
    order = range(size) if endian == "little" else reversed(range(size))
    terms = [
        f"(({type_}){buffer_}[{index}] << {shift * 8})"
        for shift, index in enumerate(order)
    ]
    return " | ".join(terms)


def pack_statements(
    value: str, size: int, endian: str, buffer_: str = "p"
) -> list[str]:
    """Statements writing ``value`` out as ``size`` bytes in ``endian`` order."""
    if size == 1:
        return [f"{buffer_}[0] = (uint8_t)({value});"]
    order = range(size) if endian == "little" else reversed(range(size))
    return [
        f"{buffer_}[{index}] = (uint8_t)(({value}) >> {shift * 8});"
        for shift, index in enumerate(order)
    ]
