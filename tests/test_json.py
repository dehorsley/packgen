"""The text of the generated JSON marshalling code."""

from __future__ import annotations

from textwrap import dedent

import pytest

from packgen import UnsupportedTypeError, parse_source
from packgen.generators import json as json_gen


def generate(source: str):
    schema = parse_source(dedent(source))
    return json_gen.generate(
        schema, source_header="packet.h", generated_header="packet_json.h"
    )


class TestHeader:
    def test_include_guard_and_jansson(self):
        header = generate("typedef struct { uint8_t a; } s_t;").header
        assert "#ifndef PACKGEN_PACKET_JSON_H_INCLUDED" in header
        assert "#include <jansson.h>" in header
        assert '#include "packet.h"' in header

    def test_signature(self):
        header = generate("typedef struct { uint8_t a; } s_t;").header
        assert "json_t *marshal_json_s_t(const s_t *t);" in header


class TestValues:
    @pytest.mark.parametrize(
        "type_, expected",
        [
            ("uint8_t", "v = json_integer((json_int_t)t->a);"),
            ("int32_t", "v = json_integer((json_int_t)t->a);"),
            ("bool", "v = json_boolean(t->a);"),
            ("float", "v = json_real((double)t->a);"),
            ("double", "v = json_real((double)t->a);"),
        ],
    )
    def test_scalar_rendering(self, type_, expected):
        assert expected in generate(f"typedef struct {{ {type_} a; }} s_t;").source

    def test_uint64_becomes_a_string(self):
        # json_int_t is signed, so a large uint64_t would come back negative.
        source = generate("typedef struct { uint64_t a; } s_t;").source
        assert "v = packgen_json_uint64(t->a);" in source
        assert "static json_t *packgen_json_uint64(uint64_t value)" in source

    def test_the_uint64_helper_is_only_emitted_when_needed(self):
        source = generate("typedef struct { uint32_t a; } s_t;").source
        assert "packgen_json_uint64" not in source

    def test_char_array_is_a_string_trimmed_at_the_first_nul(self):
        source = generate("typedef struct { char a[8]; } s_t;").source
        assert "json_stringn(t->a, packgen_strnlen(t->a, 8))" in source

    def test_the_length_scan_is_spelled_out_rather_than_using_posix_strnlen(self):
        # glibc hides strnlen under -std=c11, so the generated code carries
        # its own instead of relying on a feature test macro.
        source = generate("typedef struct { char a[8]; } s_t;").source
        assert "static size_t packgen_strnlen(const char *s, size_t limit)" in source
        assert "_POSIX_C_SOURCE" not in source

    def test_the_length_scan_is_only_emitted_when_needed(self):
        source = generate("typedef struct { uint32_t a; } s_t;").source
        assert "packgen_strnlen" not in source

    def test_nested_struct_delegates(self):
        source = generate(
            """
            typedef struct { uint8_t a; } inner_t;
            typedef struct { inner_t a; } outer_t;
            """
        ).source
        assert "v = marshal_json_inner_t(&t->a);" in source


class TestArrays:
    def test_array_of_scalars(self):
        source = generate("typedef struct { uint16_t a[4]; } s_t;").source
        assert "a0 = json_array();" in source
        assert 'json_object_set_new(root, "a", a0)' in source
        assert "for (size_t i0 = 0; i0 < 4; i0++)" in source
        assert "json_array_append_new(a0, v)" in source

    def test_nested_arrays_get_one_handle_per_level(self):
        source = generate("typedef struct { uint16_t a[2][3]; } s_t;").source
        assert "json_t *a0;" in source
        assert "json_t *a1;" in source
        assert "json_array_append_new(a0, a1)" in source
        assert "json_array_append_new(a1, v)" in source

    def test_array_handles_are_not_declared_when_unused(self):
        source = generate("typedef struct { uint16_t a; } s_t;").source
        assert "json_t *a0;" not in source


class TestErrorHandling:
    def test_allocation_failure_releases_root(self):
        source = generate("typedef struct { uint16_t a; } s_t;").source
        assert "if (v == NULL) { json_decref(root); return NULL; }" in source

    def test_set_failures_are_checked(self):
        source = generate("typedef struct { uint16_t a; } s_t;").source
        assert 'if (json_object_set_new(root, "a", v) != 0)' in source

    def test_null_input(self):
        source = generate("typedef struct { uint16_t a; } s_t;").source
        assert "if (t == NULL) return NULL;" in source


class TestRejections:
    def test_char_pointer_is_allowed(self):
        source = generate("typedef struct { char *a; } s_t;").source
        assert "v = t->a != NULL ? json_string(t->a) : json_null();" in source

    def test_other_pointers_are_rejected(self):
        with pytest.raises(UnsupportedTypeError, match="pointer"):
            generate("typedef struct { uint32_t *a; } s_t;")

    def test_arrays_of_strings_become_an_array_of_strings(self):
        # The innermost dimension of a char array is the string itself, so
        # char a[2][8] is two strings, not two arrays of numbers.
        source = generate("typedef struct { char a[2][8]; } s_t;").source
        assert "for (size_t i0 = 0; i0 < 2; i0++)" in source
        assert "json_stringn(t->a[i0], packgen_strnlen(t->a[i0], 8))" in source
        assert "json_t *a1;" not in source
