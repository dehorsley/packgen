"""The text of the generated pack/unpack code."""

from __future__ import annotations

from textwrap import dedent

import pytest

from packgen import UnsupportedTypeError, parse_source
from packgen.generators import pack


def generate(source: str, endian: str = "big"):
    schema = parse_source(dedent(source))
    return pack.generate(
        schema,
        source_header="packet.h",
        generated_header="packet_unpack.h",
        endian=endian,
    )


class TestHeader:
    def test_include_guard_and_includes(self):
        header = generate("typedef struct { uint8_t a; } s_t;").header
        assert "#ifndef PACKGEN_PACKET_UNPACK_H_INCLUDED" in header
        assert "#define PACKGEN_PACKET_UNPACK_H_INCLUDED" in header
        assert header.rstrip().endswith("#endif /* PACKGEN_PACKET_UNPACK_H_INCLUDED */")
        assert '#include "packet.h"' in header
        assert "#include <stdint.h>" in header

    def test_declares_both_directions(self):
        header = generate("typedef struct { uint8_t a; } s_t;").header
        assert (
            "ptrdiff_t unmarshal_s_t(s_t *t, const uint8_t *data, size_t n);" in header
        )
        assert "ptrdiff_t marshal_s_t(const s_t *t, uint8_t *data, size_t n);" in header

    def test_uses_only_iso_c_types(self):
        # ssize_t is POSIX and does not exist on MSVC; ptrdiff_t is C89.
        header = generate("typedef struct { uint8_t a; } s_t;").header
        assert "ssize_t" not in header
        assert "sys/types.h" not in header

    def test_exports_the_length_as_a_compile_time_constant(self):
        # A macro, not an `extern const size_t`, so callers can use it as an
        # array bound and in static assertions.
        header = generate("typedef struct { uint8_t a; } s_t;").header
        assert "#define len_s_t ((size_t)1)" in header

    def test_cplusplus_linkage(self):
        header = generate("typedef struct { uint8_t a; } s_t;").header
        assert 'extern "C" {' in header


class TestLengths:
    @pytest.mark.parametrize(
        "type_, size",
        [
            ("char", 1),
            ("uint8_t", 1),
            ("int8_t", 1),
            ("bool", 1),
            ("uint16_t", 2),
            ("int16_t", 2),
            ("uint32_t", 4),
            ("int32_t", 4),
            ("float", 4),
            ("uint64_t", 8),
            ("int64_t", 8),
            ("double", 8),
        ],
    )
    def test_scalar_sizes(self, type_, size):
        schema = parse_source(f"typedef struct {{ {type_} a; }} s_t;")
        assert schema.size_of("s_t") == size

    @pytest.mark.parametrize(
        "type_, size",
        [("char", 1), ("uint16_t", 2), ("uint32_t", 4), ("double", 8)],
    )
    def test_array_sizes(self, type_, size):
        schema = parse_source(f"typedef struct {{ {type_} a[10]; }} s_t;")
        assert schema.size_of("s_t") == size * 10

    def test_mixed_struct(self):
        schema = parse_source(
            "typedef struct { int8_t a; uint16_t b; int32_t c; uint64_t d; } s_t;"
        )
        assert schema.size_of("s_t") == 15

    def test_nested_struct_contributes_its_own_size(self):
        schema = parse_source(
            dedent(
                """
                typedef struct { uint32_t a; char b[5]; } inner_t;
                typedef struct { inner_t a[10]; } outer_t;
                """
            )
        )
        assert schema.size_of("inner_t") == 9
        assert schema.size_of("outer_t") == 90

    def test_static_assert_ties_the_length_to_the_struct(self):
        source = generate("typedef struct { uint32_t a; } s_t;").source
        assert "PACKGEN_STATIC_ASSERT(sizeof(s_t) >= 4," in source


class TestByteOrder:
    def test_big_endian_puts_the_high_byte_first(self):
        source = generate("typedef struct { uint16_t a; } s_t;", "big").source
        assert (
            "t->a = (uint16_t)(((uint16_t)p[1] << 0) | ((uint16_t)p[0] << 8));"
            in source
        )

    def test_little_endian_puts_the_low_byte_first(self):
        source = generate("typedef struct { uint16_t a; } s_t;", "little").source
        assert (
            "t->a = (uint16_t)(((uint16_t)p[0] << 0) | ((uint16_t)p[1] << 8));"
            in source
        )

    def test_marshal_mirrors_unmarshal(self):
        source = generate("typedef struct { uint16_t a; } s_t;", "big").source
        assert "p[1] = (uint8_t)(((uint16_t)t->a) >> 0);" in source
        assert "p[0] = (uint8_t)(((uint16_t)t->a) >> 8);" in source

    def test_unknown_endianness_is_rejected(self):
        with pytest.raises(ValueError, match="endian"):
            generate("typedef struct { uint16_t a; } s_t;", "middle")


class TestFieldCode:
    def test_signed_values_go_through_the_unsigned_type(self):
        source = generate("typedef struct { int16_t a; } s_t;").source
        assert "t->a = (int16_t)(uint16_t)(" in source

    def test_reals_are_copied_bit_for_bit(self):
        source = generate("typedef struct { float a; } s_t;").source
        assert "uint32_t raw = " in source
        assert "memcpy(&t->a, &raw, 4);" in source
        assert "PACKGEN_STATIC_ASSERT(sizeof(float) == 4," in source

    def test_bools_are_normalised(self):
        source = generate("typedef struct { bool a; } s_t;").source
        assert "t->a = (*p++ != 0);" in source
        assert "*p++ = (uint8_t)(t->a ? 1 : 0);" in source

    def test_byte_arrays_use_memcpy(self):
        source = generate("typedef struct { char a[16]; } s_t;").source
        assert "memcpy(t->a, p, 16);" in source
        assert "memcpy(p, t->a, 16);" in source

    def test_wide_arrays_use_a_loop(self):
        source = generate("typedef struct { uint32_t a[4]; } s_t;").source
        assert "for (size_t i0 = 0; i0 < 4; i0++)" in source
        assert "t->a[i0] = " in source

    def test_multidimensional_arrays_nest_loops(self):
        source = generate("typedef struct { uint32_t a[2][3]; } s_t;").source
        assert "for (size_t i0 = 0; i0 < 2; i0++)" in source
        assert "for (size_t i1 = 0; i1 < 3; i1++)" in source
        assert "t->a[i0][i1] = " in source

    def test_nested_structs_call_a_shared_core(self):
        source = generate(
            """
            typedef struct { uint32_t a; } inner_t;
            typedef struct { inner_t a; } outer_t;
            """
        ).source
        assert "p = packgen_unpack_inner_t(&t->a, p);" in source
        assert "p = packgen_pack_inner_t(&t->a, p);" in source


def _function_body(source: str, signature: str) -> str:
    body = source[source.index(signature) :]
    return body[: body.index("\n}")]


NESTED = """
    typedef struct { uint32_t a; char tag[2]; } inner_t;
    typedef struct { inner_t a[4]; uint8_t b; } middle_t;
    typedef struct { middle_t m[2]; uint16_t c; } outer_t;
    """


class TestSharedCores:
    """The default: one routine per struct, shared by everything embedding it.

    The caller has already proved the buffer holds len_X bytes, and len_X
    counts every nested field, so the core needs no length and no check of
    its own -- but it stays a real function, so the code is emitted once.
    """

    def test_cores_are_static_and_take_no_length(self):
        source = generate(NESTED).source
        assert (
            "static const uint8_t *packgen_unpack_outer_t(outer_t *t, "
            "const uint8_t *p)" in source
        )
        assert (
            "static uint8_t *packgen_pack_outer_t(const outer_t *t, uint8_t *p)"
            in source
        )

    def test_cores_are_forward_declared_before_use(self):
        source = generate(NESTED).source
        declaration = (
            "static const uint8_t *packgen_unpack_inner_t(inner_t *t, "
            "const uint8_t *p);"
        )
        assert declaration in source
        assert source.index(declaration) < source.index(
            "static const uint8_t *packgen_unpack_inner_t(inner_t *t, "
            "const uint8_t *p)\n"
        )

    def test_the_recursion_carries_no_bounds_check(self):
        source = generate(NESTED).source
        core = _function_body(source, "static const uint8_t *packgen_unpack_outer_t")
        assert "return -1" not in core
        assert "len_" not in core

    def test_the_entry_point_carries_the_checks(self):
        source = generate(NESTED).source
        entry = _function_body(source, "ptrdiff_t unmarshal_outer_t")
        assert "if (t == NULL || data == NULL) return -1;" in entry
        assert "if (n < len_outer_t) return -1;" in entry
        assert "return packgen_unpack_outer_t(t, data) - data;" in entry

    def test_a_shared_type_is_emitted_once(self):
        source = generate(NESTED).source
        assert source.count("static const uint8_t *packgen_unpack_inner_t") == 2


class TestGuards:
    def test_length_is_checked_before_anything_is_written(self):
        source = generate("typedef struct { uint32_t a; } s_t;").source
        body = _function_body(source, "ptrdiff_t unmarshal_s_t")
        assert body.index("if (n < len_s_t) return -1;") < body.index(
            "packgen_unpack_s_t(t, data)"
        )

    def test_null_pointers_are_rejected(self):
        source = generate("typedef struct { uint32_t a; } s_t;").source
        assert "if (t == NULL || data == NULL) return -1;" in source

    def test_pointer_fields_are_rejected(self):
        with pytest.raises(UnsupportedTypeError, match="pointer"):
            generate("typedef struct { char *a; } s_t;")
