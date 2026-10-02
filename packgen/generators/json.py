# This file covered by GPL 3 license
# C. David Horsley 2025
"""JSON marshalling built on jansson.

For every struct ``X`` this emits ``json_t *marshal_json_X(const X *t)``,
returning a new reference or ``NULL`` on allocation failure.  jansson's
``*_new`` functions steal the reference they are handed and release it even
when they fail, so the error paths only ever have to drop ``root``.

That contract only holds if allocation failure is the *only* way a value
constructor can return ``NULL``.  jansson also refuses NaN and infinity and
strings that are not UTF-8, so those go through helpers that render them
some other way rather than failing the whole struct.
"""

from __future__ import annotations

from contextlib import ExitStack

from packgen.errors import UnsupportedTypeError
from packgen.generators import pack
from packgen.generators.common import (
    GeneratedPair,
    check_no_macro_collisions,
    check_no_name_collisions,
    check_packable,
)
from packgen.model import BOOL_TYPES, INT_TYPES, REAL_TYPES, Field, Schema, Struct
from packgen.writer import Writer, banner, include_guard

FAIL = "{ json_decref(root); return NULL; }"

#: jansson's json_int_t is signed, so uint64_t values above INT64_MAX cannot
#: round trip as JSON numbers.  They go out as decimal strings instead, the
#: same compromise proto3's JSON mapping makes.
UINT64_HELPER = """\
static json_t *packgen_json_uint64(uint64_t value)
{
    char text[21];
    int written = snprintf(text, sizeof text, "%" PRIu64, value);

    if (written < 0 || (size_t)written >= sizeof text) return NULL;
    return json_string(text);
}"""

#: JSON has no NaN or infinity, and json_real() returns NULL for them, which
#: would read as an allocation failure.  They go out as the strings proto3's
#: JSON mapping uses, so a sentinel NaN still survives the trip.
REAL_HELPER = """\
static json_t *packgen_json_real(double value)
{
    if (isnan(value)) return json_string("NaN");
    if (isinf(value)) return json_string(value > 0 ? "Infinity" : "-Infinity");
    return json_real(value);
}"""

#: jansson refuses a string that is not valid UTF-8, and a char array
#: unmarshalled off the wire can hold anything.  Each byte that does not
#: begin a valid sequence becomes U+FFFD instead.  The checks are the ones
#: jansson makes -- no overlong forms, surrogates or code points past
#: U+10FFFF -- so whatever passes here, json_stringn() accepts.
STRING_HELPER = """\
static size_t packgen_utf8_length(const unsigned char *s, size_t n)
{
    size_t length;
    size_t i;
    uint32_t code;

    if (s[0] < 0x80) return 1;
    if (s[0] >= 0xC2 && s[0] <= 0xDF) { length = 2; code = s[0] & 0x1Fu; }
    else if (s[0] >= 0xE0 && s[0] <= 0xEF) { length = 3; code = s[0] & 0x0Fu; }
    else if (s[0] >= 0xF0 && s[0] <= 0xF4) { length = 4; code = s[0] & 0x07u; }
    else return 0;
    if (length > n) return 0;
    for (i = 1; i < length; i++) {
        if ((s[i] & 0xC0u) != 0x80u) return 0;
        code = (code << 6) | (s[i] & 0x3Fu);
    }
    if (length == 3 && code < 0x800u) return 0;
    if (length == 4 && code < 0x10000u) return 0;
    if (code >= 0xD800u && code <= 0xDFFFu) return 0;
    if (code > 0x10FFFFu) return 0;
    return length;
}

static json_t *packgen_json_string(const char *s, size_t n)
{
    const unsigned char *u = (const unsigned char *)s;
    size_t i = 0;
    size_t out = 0;
    size_t length;
    char *copy;
    json_t *v;

    while (i < n && (length = packgen_utf8_length(u + i, n - i)) != 0) i += length;
    if (i == n) return json_stringn(s, n);

    /* At worst every byte is replaced by three. */
    if (n > SIZE_MAX / 3) return NULL;
    copy = malloc(n * 3);
    if (copy == NULL) return NULL;
    for (i = 0; i < n; i += length) {
        length = packgen_utf8_length(u + i, n - i);
        if (length == 0) {
            copy[out++] = (char)0xEF;
            copy[out++] = (char)0xBF;
            copy[out++] = (char)0xBD;
            length = 1;
        } else {
            memcpy(copy + out, s + i, length);
            out += length;
        }
    }
    v = json_stringn(copy, out);
    free(copy);
    return v;
}"""

#: `strnlen` is POSIX, not ISO C: glibc hides it under -std=c11, where
#: __STRICT_ANSI__ suppresses _DEFAULT_SOURCE.  Spelling the scan out keeps
#: the generated code free of feature-test macros.
STRNLEN_HELPER = """\
static size_t packgen_strnlen(const char *s, size_t limit)
{
    size_t length = 0;

    while (length < limit && s[length] != '\\0') length++;
    return length;
}"""


def signature(name: str) -> str:
    return f"json_t *marshal_json_{name}(const {name} *t)"


def generate(
    schema: Schema, *, source_header: str, generated_header: str
) -> GeneratedPair:
    """Generate the JSON marshalling header and source for ``schema``."""
    helpers = [
        "packgen_json_uint64",
        "packgen_json_real",
        "packgen_json_string",
        "packgen_utf8_length",
        "packgen_strnlen",
    ]
    marshallers = [
        (
            f"marshal_json_{struct.name}",
            f"the JSON marshaller of struct {struct.name!r}",
        )
        for struct in schema
    ]
    # The JSON routines are generated alongside the pack ones, and a struct
    # named json_X would have its pack routine called marshal_json_X.
    check_no_name_collisions(schema, [*pack.generated_names(schema), *marshallers])
    check_no_macro_collisions(schema, [*helpers, *(name for name, _ in marshallers)])
    resolved = {
        struct.name: [_resolve(schema, f, struct.name) for f in struct.fields]
        for struct in schema
    }
    return GeneratedPair(
        header=_header(schema, source_header, generated_header),
        source=_source(schema, resolved, source_header, generated_header),
    )


def _resolve(schema: Schema, field: Field, struct_name: str) -> str:
    if field.is_pointer:
        # A `char *` is the one pointer with an obvious JSON rendering.
        if (
            field.pointer == 1
            and not field.is_array
            and schema.resolve(field.type) == "char"
        ):
            return "char *"
        raise UnsupportedTypeError(
            f"in struct {struct_name!r}: field {field.name!r} is a pointer; "
            f"only a plain `char *` can be marshalled to JSON"
        )
    return check_packable(schema, field, struct_name)


def _header(schema: Schema, source_header: str, generated_header: str) -> str:
    guard = include_guard(generated_header)
    writer = Writer()
    writer.lines(
        banner(source_header=source_header),
        f"#ifndef {guard}",
        f"#define {guard}",
        "",
        "#include <jansson.h>",
        "#include <stdbool.h>",
        "#include <stddef.h>",
        "#include <stdint.h>",
        "",
        f'#include "{source_header}"',
        "",
        "#ifdef __cplusplus",
        'extern "C" {',
        "#endif",
        "",
        "/* Each returns a new reference, or NULL on allocation failure. */",
    )
    for struct in schema:
        writer.line(f"{signature(struct.name)};")
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
) -> str:
    used = {type_ for types in resolved.values() for type_ in types}
    needs_uint64 = "uint64_t" in used
    needs_real = bool(used & REAL_TYPES.keys())
    needs_strnlen = any(
        _is_string(field, type_)
        for struct in schema
        for field, type_ in zip(struct.fields, resolved[struct.name], strict=True)
    )
    needs_string = needs_strnlen or "char *" in used

    includes = []
    if needs_uint64:
        includes += ["#include <inttypes.h>", "#include <stdio.h>"]
    if needs_real:
        includes.append("#include <math.h>")
    includes.append("#include <stddef.h>")
    if needs_string:
        includes += ["#include <stdlib.h>", "#include <string.h>"]

    writer = Writer()
    writer.line(banner(source_header=source_header))
    writer.lines(*includes, "", f'#include "{generated_header}"', "")

    if needs_uint64:
        writer.line()
        writer.line(UINT64_HELPER)
    if needs_real:
        writer.line()
        writer.line(REAL_HELPER)
    if needs_string:
        writer.line()
        writer.line(STRING_HELPER)
    if needs_strnlen:
        writer.line()
        writer.line(STRNLEN_HELPER)
    for struct in schema:
        writer.line()
        _function(writer, struct, resolved[struct.name])
    return writer.getvalue()


def _is_string(field: Field, type_: str) -> bool:
    """A char array is rendered as a string, not an array of numbers."""
    return type_ == "char" and field.is_array


def _array_levels(field: Field, type_: str) -> int:
    """How many nested JSON arrays a field needs.

    For a char array the innermost dimension is the string itself, so
    ``char names[4][8]`` is one array of four strings, not two arrays.
    """
    if not field.is_array:
        return 0
    return len(field.dims) - 1 if _is_string(field, type_) else len(field.dims)


def _emit_string(writer: Writer, accessor: str, size: int) -> None:
    # A fixed-size char field is conventionally NUL padded, so stop at the
    # first NUL rather than emitting the padding into the JSON string.
    writer.line(
        f"v = packgen_json_string({accessor}, packgen_strnlen({accessor}, {size}));"
    )


def _function(writer: Writer, struct: Struct, types: list[str]) -> None:
    pairs = list(zip(struct.fields, types, strict=True))
    # One array handle per nesting level, reused across fields.
    depth = max((_array_levels(f, t) for f, t in pairs), default=0)

    with writer.function(signature(struct.name)):
        writer.line("json_t *root;")
        if pairs:
            writer.line("json_t *v;")
        for level in range(depth):
            writer.line(f"json_t *a{level};")
        writer.line()
        writer.line("if (t == NULL) return NULL;")
        writer.line("root = json_object();")
        writer.line("if (root == NULL) return NULL;")
        writer.line()
        for field, type_ in pairs:
            _field(writer, field, type_)
        writer.line("return root;")


def _field(writer: Writer, field: Field, type_: str) -> None:
    string = _is_string(field, type_)
    levels = _array_levels(field, type_)

    def leaf(accessor: str) -> None:
        if string:
            _emit_string(writer, accessor, field.dims[-1])
        else:
            _value(writer, accessor, type_)

    if levels == 0:
        leaf(f"t->{field.name}")
        _set_member(writer, field.name)
        return

    writer.line("a0 = json_array();")
    writer.line(f"if (a0 == NULL) {FAIL}")
    writer.line(f'if (json_object_set_new(root, "{field.name}", a0) != 0) {FAIL}')

    with ExitStack() as stack:
        accessor = f"t->{field.name}"
        for level in range(levels - 1):
            index = f"i{level}"
            stack.enter_context(
                writer.block(
                    f"for (size_t {index} = 0; "
                    f"{index} < {field.dims[level]}; {index}++)"
                )
            )
            accessor += f"[{index}]"
            child = f"a{level + 1}"
            writer.line(f"{child} = json_array();")
            writer.line(f"if ({child} == NULL) {FAIL}")
            writer.line(f"if (json_array_append_new(a{level}, {child}) != 0) {FAIL}")

        last = levels - 1
        index = f"i{last}"
        with writer.block(
            f"for (size_t {index} = 0; {index} < {field.dims[last]}; {index}++)"
        ):
            leaf(f"{accessor}[{index}]")
            writer.line(f"if (v == NULL) {FAIL}")
            writer.line(f"if (json_array_append_new(a{last}, v) != 0) {FAIL}")
    writer.line()


def _set_member(writer: Writer, name: str) -> None:
    writer.line(f"if (v == NULL) {FAIL}")
    writer.line(f'if (json_object_set_new(root, "{name}", v) != 0) {FAIL}')
    writer.line()


def _value(writer: Writer, accessor: str, type_: str) -> None:
    if type_ == "char *":
        writer.line(
            f"v = {accessor} != NULL ? "
            f"packgen_json_string({accessor}, strlen({accessor})) : json_null();"
        )
    elif type_ in BOOL_TYPES:
        writer.line(f"v = json_boolean({accessor});")
    elif type_ in REAL_TYPES:
        writer.line(f"v = packgen_json_real((double){accessor});")
    elif type_ == "uint64_t":
        writer.line(f"v = packgen_json_uint64({accessor});")
    elif type_ in INT_TYPES:
        writer.line(f"v = json_integer((json_int_t){accessor});")
    else:
        writer.line(f"v = marshal_json_{type_}(&{accessor});")
