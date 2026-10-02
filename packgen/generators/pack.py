# This file covered by GPL 3 license
# C. David Horsley 2025
"""Binary pack/unpack routines for the structs in a schema.

For every struct ``X`` this emits::

    ptrdiff_t unmarshal_X(X *t, const uint8_t *data, size_t n);
    ptrdiff_t marshal_X(const X *t, uint8_t *data, size_t n);

Both return the number of bytes consumed or produced, or ``-1`` if the buffer
is too short.  The two are exact inverses, which is what makes the generated
code testable by round trip.  ``ptrdiff_t`` rather than the more idiomatic
``ssize_t`` because only the former is ISO C; ``ssize_t`` is POSIX, and does
not exist on MSVC.

Each one is a thin checked wrapper around a ``static`` core that threads a
pointer and takes no length.  The wrapper has already proved the buffer holds
``len_X`` bytes, and ``len_X`` counts every nested field, so the core cannot
run off the end, and a nested struct costs a plain call rather than a second
bounds check and a branch.  Whether that call survives is the compiler's
decision, and measurement says its judgement is good: forcing the nesting
open, either by emitting it inline or with always_inline, produced identical
throughput on a 6 KB packet for 15% more text.
"""

from __future__ import annotations

from packgen.generators import lengths
from packgen.generators.common import (
    BYTE_TYPES,
    GeneratedPair,
    array_loops,
    check_no_macro_collisions,
    check_no_name_collisions,
    check_packable,
    pack_statements,
    unpack_expression,
)
from packgen.model import (
    BOOL_TYPES,
    INT_TYPES,
    PRIMITIVE_SIZES,
    REAL_TYPES,
    SIGNED_INT_TYPES,
    Field,
    Schema,
    Struct,
    unsigned_equivalent,
)
from packgen.writer import Writer, banner, include_guard

STATIC_ASSERT = """\
#ifndef PACKGEN_STATIC_ASSERT
#if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
#define PACKGEN_STATIC_ASSERT(cond, msg) _Static_assert(cond, #msg)
#elif defined(__cplusplus) && __cplusplus >= 201103L
#define PACKGEN_STATIC_ASSERT(cond, msg) static_assert(cond, #msg)
#else
#define PACKGEN_STATIC_ASSERT(cond, msg) \\
    typedef char packgen_assert_##msg[(cond) ? 1 : -1]
#endif
#endif"""


def unmarshal_signature(name: str) -> str:
    return f"ptrdiff_t unmarshal_{name}({name} *t, const uint8_t *data, size_t n)"


def marshal_signature(name: str) -> str:
    return f"ptrdiff_t marshal_{name}(const {name} *t, uint8_t *data, size_t n)"


def unpack_core_signature(name: str) -> str:
    return f"static const uint8_t *unmarshal_{name}_core({name} *t, const uint8_t *p)"


def pack_core_signature(name: str) -> str:
    return f"static uint8_t *marshal_{name}_core(const {name} *t, uint8_t *p)"


def generated_names(schema: Schema) -> list[tuple[str, str]]:
    """Every file-scope name the pack routines emit, with what it is for."""
    names = []
    for struct in schema:
        label = f"struct {struct.name!r}"
        names += [
            (lengths.length_name(struct.name), f"the packed length of {label}"),
            (f"unmarshal_{struct.name}", f"the unmarshaller of {label}"),
            (f"marshal_{struct.name}", f"the marshaller of {label}"),
            (f"unmarshal_{struct.name}_core", f"the unmarshal core of {label}"),
            (f"marshal_{struct.name}_core", f"the marshal core of {label}"),
        ]
    return names


def generate(
    schema: Schema,
    *,
    source_header: str,
    generated_header: str,
    endian: str = "big",
) -> GeneratedPair:
    """Generate the pack/unpack header and source for ``schema``."""
    if endian not in {"little", "big"}:
        raise ValueError(f"endian must be 'little' or 'big', not {endian!r}")

    names = generated_names(schema)
    check_no_name_collisions(schema, names)
    check_no_macro_collisions(schema, (name for name, _ in names))

    # Resolving every field up front means a bad header fails before any
    # output is produced, rather than emitting half a file.
    resolved = {
        struct.name: [check_packable(schema, f, struct.name) for f in struct.fields]
        for struct in schema
    }

    return GeneratedPair(
        header=_header(schema, source_header, generated_header, endian),
        source=_source(schema, resolved, source_header, generated_header, endian),
    )


def _header(
    schema: Schema, source_header: str, generated_header: str, endian: str
) -> str:
    guard = include_guard(generated_header)
    writer = Writer()
    writer.lines(
        banner(source_header=source_header, endian=endian),
        f"#ifndef {guard}",
        f"#define {guard}",
        "",
        "#include <stdbool.h>",
        "#include <stddef.h>",
        "#include <stdint.h>",
        "",
        f'#include "{source_header}"',
        "",
        STATIC_ASSERT,
        "",
        "#ifdef __cplusplus",
        'extern "C" {',
        "#endif",
        "",
        "/* Packed size, in bytes, of each struct. */",
    )
    lengths.write_declarations(writer, schema)
    writer.line()
    writer.lines(
        "/*",
        " * unmarshal_*: decode `n` bytes at `data` into `*t`.",
        " * marshal_*:   encode `*t` into the `n` bytes at `data`.",
        " *",
        " * Both return the number of bytes used, or -1 if the buffer is too",
        " * short or either pointer is NULL.",
        " */",
    )
    for struct in schema:
        writer.line(f"{unmarshal_signature(struct.name)};")
        writer.line(f"{marshal_signature(struct.name)};")
    writer.lines(
        "",
        "#ifdef __cplusplus",
        "}",
        "#endif",
        "",
        f"#endif /* {guard} */",
    )
    return writer.getvalue()


def _source(
    schema: Schema,
    resolved: dict[str, list[str]],
    source_header: str,
    generated_header: str,
    endian: str,
) -> str:
    writer = Writer()
    writer.lines(
        banner(source_header=source_header, endian=endian),
        "#include <string.h>",
        "",
        f'#include "{generated_header}"',
        "",
    )

    used = {type_ for types in resolved.values() for type_ in types}
    for real in ("float", "double"):
        if real in used:
            writer.line(
                f"PACKGEN_STATIC_ASSERT(sizeof({real}) == {REAL_TYPES[real]}, "
                f"{real}_is_not_ieee754);"
            )
    writer.line()

    writer.line("/* The packed layout must still fit in the in-memory struct. */")
    lengths.write_definitions(writer, schema)

    writer.line()
    writer.lines(
        "/*",
        " * Unchecked cores, shared between the entry point for a struct and",
        " * every struct that embeds it.  The caller has already proved the",
        " * buffer holds len_* bytes, and len_* counts every nested field, so",
        " * a core consumes exactly what was checked for and needs no length",
        " * of its own.",
        " */",
    )
    for struct in schema:
        writer.line(f"{unpack_core_signature(struct.name)};")
        writer.line(f"{pack_core_signature(struct.name)};")

    for struct in schema:
        for unpack in (True, False):
            writer.line()
            _core(writer, schema, struct, endian, unpack)
            writer.line()
            _entry_point(writer, schema, struct, endian, unpack)

    return writer.getvalue()


def _core(
    writer: Writer, schema: Schema, struct: Struct, endian: str, unpack: bool
) -> None:
    """The shared, unchecked body for one struct."""
    signature = (
        unpack_core_signature(struct.name)
        if unpack
        else pack_core_signature(struct.name)
    )
    with writer.function(signature):
        _emit_struct(writer, schema, struct, endian, unpack)
        writer.line()
        writer.line("return p;")


def _entry_point(
    writer: Writer, schema: Schema, struct: Struct, endian: str, unpack: bool
) -> None:
    signature = (
        unmarshal_signature(struct.name) if unpack else marshal_signature(struct.name)
    )
    verb = "unmarshal" if unpack else "marshal"

    with writer.function(signature):
        writer.line("if (t == NULL || data == NULL) return -1;")
        writer.line(f"if (n < {lengths.length_name(struct.name)}) return -1;")
        writer.line()
        writer.line(f"return {verb}_{struct.name}_core(t, data) - data;")


def _emit_struct(
    writer: Writer, schema: Schema, struct: Struct, endian: str, unpack: bool
) -> None:
    for field in struct.fields:
        _emit_field(writer, schema, field, endian, unpack, struct.name)


def _emit_field(
    writer: Writer,
    schema: Schema,
    field: Field,
    endian: str,
    unpack: bool,
    struct_name: str,
) -> None:
    type_ = check_packable(schema, field, struct_name)

    # A run of single-byte elements is contiguous in both representations.
    if field.is_array and type_ in BYTE_TYPES:
        target = f"t->{field.name}"
        if unpack:
            writer.line(f"memcpy({target}, p, {field.count});")
        else:
            writer.line(f"memcpy(p, {target}, {field.count});")
        writer.line(f"p += {field.count};")
        return

    with array_loops(writer, field) as (accessor, _depth):
        if schema.is_struct(type_):
            verb = "unmarshal" if unpack else "marshal"
            writer.line(f"p = {verb}_{type_}_core(&{accessor}, p);")
        elif unpack:
            _unmarshal_value(writer, accessor, type_, endian)
        else:
            _marshal_value(writer, accessor, type_, endian)


def _unmarshal_value(writer: Writer, accessor: str, type_: str, endian: str) -> None:
    if type_ == "uint8_t":
        writer.line(f"{accessor} = *p++;")
        return

    if type_ in BYTE_TYPES:
        # int8_t, and char where it is signed: converting a byte above 127
        # to a signed type is implementation defined, copying it is not.
        writer.line(f"memcpy(&{accessor}, p, 1);")
        writer.line("p += 1;")
        return

    if type_ in BOOL_TYPES:
        writer.line(f"{accessor} = (*p++ != 0);")
        return

    if type_ in REAL_TYPES or type_ in SIGNED_INT_TYPES:
        # Assemble the bits in the unsigned type of the same width, then
        # copy them.  For float and double that is the only portable way
        # to reinterpret bits.  For intN_t it avoids the cast back from
        # unsigned, which is implementation defined above INTN_MAX (C99
        # 6.3.1.3p3), while intN_t is guaranteed two's complement with no
        # padding (7.18.1.1), so the copy is exact.  Either way compilers
        # emit the same load and byte swap as a cast would.
        size = PRIMITIVE_SIZES[type_]
        bits = unsigned_equivalent(type_)
        with writer.block():
            # The cast matters at 16 bits, where the shifts promote to int.
            expression = unpack_expression(bits, size, endian)
            writer.line(f"{bits} raw = ({bits})({expression});")
            writer.line(f"memcpy(&{accessor}, &raw, {size});")
        writer.line(f"p += {size};")
        return

    if type_ in INT_TYPES:
        size = INT_TYPES[type_]
        bits = unsigned_equivalent(type_)
        writer.line(f"{accessor} = ({bits})({unpack_expression(bits, size, endian)});")
        writer.line(f"p += {size};")
        return

    raise AssertionError(f"not a primitive type: {type_!r}")


def _marshal_value(writer: Writer, accessor: str, type_: str, endian: str) -> None:
    if type_ in BYTE_TYPES:
        writer.line(f"*p++ = (uint8_t){accessor};")
        return

    if type_ in BOOL_TYPES:
        writer.line(f"*p++ = (uint8_t)({accessor} ? 1 : 0);")
        return

    if type_ in REAL_TYPES:
        size = REAL_TYPES[type_]
        bits = unsigned_equivalent(type_)
        with writer.block():
            writer.line(f"{bits} raw;")
            writer.line(f"memcpy(&raw, &{accessor}, {size});")
            writer.lines(*pack_statements("raw", size, endian))
        writer.line(f"p += {size};")
        return

    if type_ in INT_TYPES:
        size = INT_TYPES[type_]
        bits = unsigned_equivalent(type_)
        writer.lines(*pack_statements(f"({bits}){accessor}", size, endian))
        writer.line(f"p += {size};")
        return

    raise AssertionError(f"not a primitive type: {type_!r}")
