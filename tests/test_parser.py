"""Parsing C headers into the schema."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from packgen import ParseError, UnknownTypeError, UnsupportedTypeError, parse_source
from packgen.model import Field
from packgen.parser import parse_header

HEADERS = Path(__file__).parent / "headers"


def one_struct(source: str):
    schema = parse_source(dedent(source))
    assert len(schema) == 1
    return schema.structs[0]


def test_scalar_fields():
    struct = one_struct(
        """
        typedef struct {
            uint32_t a;
            char b;
        } my_struct;
        """
    )
    assert struct.name == "my_struct"
    assert struct.fields == (
        Field(name="a", type="uint32_t"),
        Field(name="b", type="char"),
    )


def test_array_field():
    struct = one_struct("typedef struct { uint16_t a[4]; } s_t;")
    assert struct.fields == (Field(name="a", type="uint16_t", dims=(4,)),)


@pytest.mark.parametrize(
    "declaration, dims",
    [
        ("a[2]", (2,)),
        ("a[2][3]", (2, 3)),
        ("a[2][3][4]", (2, 3, 4)),
        ("a[2][3][4][5]", (2, 3, 4, 5)),
        ("a[2][3][4][5][6]", (2, 3, 4, 5, 6)),
    ],
)
def test_array_rank_and_order(declaration, dims):
    """Extents come out outermost first, however many there are.

    tree-sitter nests array declarators inside out, so getting this backwards
    would put the loops and the accessor out of step.
    """
    struct = one_struct(f"typedef struct {{ uint8_t {declaration}; }} s_t;")
    (field,) = struct.fields
    assert field.dims == dims


def test_several_declarators_in_one_declaration():
    struct = one_struct("typedef struct { int8_t a, b, c[2]; } s_t;")
    assert struct.fields == (
        Field(name="a", type="int8_t"),
        Field(name="b", type="int8_t"),
        Field(name="c", type="int8_t", dims=(2,)),
    )


def test_comments_are_ignored():
    struct = one_struct(
        """
        typedef struct {
            uint8_t a; // trailing
            /* leading */
            uint8_t b;
        } s_t;
        """
    )
    assert [f.name for f in struct.fields] == ["a", "b"]


def test_tagged_struct_uses_the_typedef_name():
    struct = one_struct("typedef struct tag { uint8_t a; } named_t;")
    assert struct.name == "named_t"


def test_one_typedef_may_declare_several_names():
    schema = parse_source("typedef struct { uint8_t a; } first_t, second_t;")
    assert [s.name for s in schema] == ["first_t", "second_t"]


def test_include_guards_do_not_hide_the_body():
    schema = parse_source(
        dedent(
            """
            #ifndef PACKET_H
            #define PACKET_H
            typedef struct { uint8_t a; } s_t;
            #endif
            """
        )
    )
    assert [s.name for s in schema] == ["s_t"]


def test_pragma_once_is_ignored():
    schema = parse_source("#pragma once\ntypedef struct { uint8_t a; } s_t;")
    assert [s.name for s in schema] == ["s_t"]


def test_includes_are_not_followed():
    schema = parse_source("#include <stdint.h>\ntypedef struct { uint8_t a; } s_t;")
    assert [s.name for s in schema] == ["s_t"]


class TestExternC:
    """The `extern "C"` wrapper almost every header carries.

    Guarded by `#ifdef __cplusplus`, it opens a brace in one conditional
    block and closes it in another, which leaves tree-sitter looking at an
    unbalanced file unless the C++ branches are dropped first.
    """

    def test_guarded_wrapper(self):
        schema = parse_source(
            dedent(
                """
                #ifdef __cplusplus
                extern "C" {
                #endif

                typedef struct { uint8_t a; } s_t;

                #ifdef __cplusplus
                }
                #endif
                """
            )
        )
        assert [s.name for s in schema] == ["s_t"]

    def test_defined_form_of_the_guard(self):
        schema = parse_source(
            dedent(
                """
                #if defined(__cplusplus)
                extern "C" {
                #endif
                typedef struct { uint8_t a; } s_t;
                """
            )
        )
        assert [s.name for s in schema] == ["s_t"]

    def test_unguarded_wrapper(self):
        schema = parse_source('extern "C" { typedef struct { uint8_t a; } s_t; }')
        assert [s.name for s in schema] == ["s_t"]

    def test_the_c_branch_of_a_cplusplus_block_is_kept(self):
        schema = parse_source(
            dedent(
                """
                #ifdef __cplusplus
                #define N 8
                #else
                #define N 4
                #endif
                typedef struct { uint8_t a[N]; } s_t;
                """
            )
        )
        assert schema.size_of("s_t") == 4

    def test_ifndef_cplusplus_keeps_its_body(self):
        schema = parse_source(
            dedent(
                """
                #ifndef __cplusplus
                typedef struct { uint8_t a[3]; } s_t;
                #endif
                """
            )
        )
        assert schema.size_of("s_t") == 3

    def test_a_nested_conditional_does_not_unbalance_the_scan(self):
        schema = parse_source(
            dedent(
                """
                #ifdef __cplusplus
                #if 1
                extern "C" {
                #endif
                #endif
                typedef struct { uint8_t a; } s_t;
                """
            )
        )
        assert [s.name for s in schema] == ["s_t"]

    def test_a_commented_out_guard_is_not_acted_on(self):
        schema = parse_source(
            dedent(
                """
                /*
                 * #ifdef __cplusplus
                 * extern "C" {
                 * #endif
                 */
                typedef struct { uint8_t a; } s_t;
                """
            )
        )
        assert [s.name for s in schema] == ["s_t"]

    def test_line_numbers_survive_the_stripping(self):
        source = dedent(
            """
            #ifdef __cplusplus
            extern "C" {
            #endif
            typedef struct {
                uint32_t a : 3;
            } s_t;
            """
        )
        with pytest.raises(UnsupportedTypeError, match="line 6"):
            parse_source(source)


def test_nested_struct_field():
    schema = parse_source(
        dedent(
            """
            typedef struct { uint32_t a; } inner_t;
            typedef struct { inner_t a[2]; } outer_t;
            """
        )
    )
    assert schema["outer_t"].fields == (Field(name="a", type="inner_t", dims=(2,)),)
    assert schema.is_struct("inner_t")


def test_alias_typedef_is_resolved():
    schema = parse_source(
        dedent(
            """
            typedef uint32_t word_t;
            typedef struct { word_t a; } s_t;
            """
        )
    )
    assert schema.aliases == {"word_t": "uint32_t"}
    assert schema.resolve("word_t") == "uint32_t"
    assert schema.size_of("s_t") == 4


def test_real_world_header_parses():
    schema = parse_header(HEADERS / "dbbcpacket.h")
    assert schema.size_of("dbbc3_ddc_multicast_t") == 6208
    assert schema.size_of("gcomo_t") == 8


class TestConstantFolding:
    @pytest.mark.parametrize(
        "definition, expected",
        [
            ("#define N 4", 4),
            ("#define N 0x10", 16),
            ("#define N 010", 8),
            ("#define N 4u", 4),
            ("#define N 4UL", 4),
            ("#define N (2 + 3)", 5),
            ("#define N (2 * 3 + 1)", 7),
            ("#define N (1 << 3)", 8),
            ("#define M 2\n#define N (M * 5)", 10),
        ],
    )
    def test_define_as_array_bound(self, definition, expected):
        schema = parse_source(f"{definition}\ntypedef struct {{ uint8_t a[N]; }} s_t;")
        assert schema.size_of("s_t") == expected

    def test_enumerator_as_array_bound(self):
        schema = parse_source(
            "enum { FIRST, SECOND, THIRD };\ntypedef struct { uint8_t a[THIRD]; } s_t;"
        )
        assert schema.size_of("s_t") == 2

    def test_explicit_enumerator_values(self):
        schema = parse_source(
            dedent(
                """
                typedef enum { A = 5, B } tags_t;
                typedef struct { uint8_t a[B]; } s_t;
                """
            )
        )
        assert schema.size_of("s_t") == 6

    def test_unknown_constant_is_reported(self):
        with pytest.raises(UnsupportedTypeError, match="NOT_DEFINED"):
            parse_source("typedef struct { uint8_t a[NOT_DEFINED]; } s_t;")

    def test_division_truncates_toward_zero_like_c(self):
        schema = parse_source(
            "#define N (-7 / 2 + 6)\ntypedef struct { uint8_t a[N]; } s_t;"
        )
        # C gives -3, not Python's -4.
        assert schema.size_of("s_t") == 3

    def test_negative_remainder_follows_c(self):
        # C's -7 % 3 is -1, which is not a usable bound; Python's is 2.
        with pytest.raises(UnsupportedTypeError, match="positive"):
            parse_source("#define N (-7 % 3)\ntypedef struct { uint8_t a[N]; } s_t;")

    def test_division_by_zero(self):
        with pytest.raises(UnsupportedTypeError, match="division by zero"):
            parse_source("#define N (1 / 0)\ntypedef struct { uint8_t a[N]; } s_t;")

    def test_a_foldable_condition_picks_one_definition(self):
        # WIDE is not defined in this file, so the #else branch is the live
        # one and there is no conflict to resolve.
        source = dedent(
            """
            #ifdef WIDE
            #define N 8
            #else
            #define N 4
            #endif
            typedef struct { uint8_t a[N]; } s_t;
            """
        )
        assert parse_source(source).size_of("s_t") == 4

    @pytest.mark.parametrize(
        "define",
        [
            "#define N 4 // four",
            "#define N (4) // four",
            "#define N 4 /* four */",
            "#define N /* four */ 4",
        ],
    )
    def test_a_comment_after_the_value(self, define):
        # A trailing // comment used to swallow the parenthesis packgen
        # wraps the value in, so the constant came out unusable.
        source = f"{define}\ntypedef struct {{ uint8_t a[N]; }} s_t;"
        assert parse_source(source).size_of("s_t") == 4

    def test_a_commented_value_still_folds_in_a_condition(self):
        source = dedent(
            """
            #define VERSION 2 // current wire format
            #if VERSION >= 2
            typedef struct { uint32_t a; } s_t;
            #else
            typedef struct { uint16_t a; } s_t;
            #endif
            """
        )
        assert parse_source(source).size_of("s_t") == 4

    def test_a_constant_defined_two_ways_is_refused(self):
        # An unfoldable condition means both branches are walked, so silently
        # keeping the last one would generate a layout for whichever build the
        # caller might not be using.
        source = dedent(
            """
            #if CONFIG_MACRO(1)
            #define N 8
            #else
            #define N 4
            #endif
            typedef struct { uint8_t a[N]; } s_t;
            """
        )
        with pytest.raises(UnsupportedTypeError, match="more than once"):
            parse_source(source)

    def test_the_same_value_twice_is_fine(self):
        source = dedent(
            """
            #ifdef WIDE
            #define N 4
            #else
            #define N 4
            #endif
            typedef struct { uint8_t a[N]; } s_t;
            """
        )
        assert parse_source(source).size_of("s_t") == 4

    def test_enumerators_after_an_unevaluatable_one_are_not_guessed(self):
        # A = 1, B = sizeof(int), C.  C is 5 in C, but packgen cannot know
        # that, and must not silently number it 2.
        source = dedent(
            """
            enum { A = 1, B = sizeof(int), C };
            typedef struct { uint8_t a[C]; } s_t;
            """
        )
        with pytest.raises(UnsupportedTypeError, match="could not evaluate"):
            parse_source(source)

    def test_enumerators_before_an_unevaluatable_one_are_still_usable(self):
        source = dedent(
            """
            enum { A = 1, B, C = sizeof(int), D };
            typedef struct { uint8_t a[B]; } s_t;
            """
        )
        assert parse_source(source).size_of("s_t") == 2

    def test_a_later_enum_recovers(self):
        source = dedent(
            """
            enum { A = sizeof(int) };
            enum { P = 3, Q };
            typedef struct { uint8_t a[Q]; } s_t;
            """
        )
        assert parse_source(source).size_of("s_t") == 4


class TestRejections:
    def test_bit_fields(self):
        with pytest.raises(UnsupportedTypeError, match="bit field"):
            parse_source("typedef struct { uint32_t a : 3; } s_t;")

    def test_plain_int_is_not_fixed_width(self):
        with pytest.raises(UnsupportedTypeError, match="fixed-width"):
            parse_source("typedef struct { unsigned int a; } s_t;")

    def test_flexible_array_member(self):
        with pytest.raises(UnsupportedTypeError, match="fixed size"):
            parse_source("typedef struct { uint8_t a[]; } s_t;")

    def test_zero_length_array(self):
        with pytest.raises(UnsupportedTypeError, match="positive"):
            parse_source("typedef struct { uint8_t a[0]; } s_t;")

    def test_anonymous_embedded_struct(self):
        with pytest.raises(UnsupportedTypeError):
            parse_source("typedef struct { struct { uint8_t a; } inner; } s_t;")

    def test_conditional_compilation_inside_a_struct(self):
        source = dedent(
            """
            typedef struct {
                uint8_t a;
            #ifdef WIDE
                uint32_t b;
            #endif
            } s_t;
            """
        )
        with pytest.raises(UnsupportedTypeError, match="conditional compilation"):
            parse_source(source)

    @pytest.mark.parametrize(
        "first, second",
        [
            ("typedef uint16_t w_t;", "typedef uint32_t w_t;"),
            ("typedef struct { uint32_t a; } w_t;", "typedef uint8_t w_t;"),
            ("typedef uint8_t w_t;", "typedef struct { uint32_t a; } w_t;"),
            ("typedef uint8_t w_t;", "typedef union { uint8_t a; } w_t;"),
            ("typedef struct { uint8_t a; } w_t;", "typedef union { uint8_t a; } w_t;"),
        ],
    )
    def test_typedef_meaning_two_things_under_an_unfoldable_condition(
        self, first, second
    ):
        # Constants and structs were already refused here; a typedef that
        # silently kept its last meaning would pick a layout for a build
        # the caller may not be using.
        source = dedent(
            f"""
            #if CONFIG_MACRO(1)
            {first}
            #else
            {second}
            #endif
            """
        )
        with pytest.raises(UnsupportedTypeError, match="more than once"):
            parse_source(source)

    def test_an_identical_typedef_twice_is_fine(self):
        # Repeating a typedef identically is legal C11.
        source = dedent(
            """
            typedef uint16_t w_t;
            typedef uint16_t w_t;
            typedef struct { w_t a; } s_t;
            """
        )
        assert parse_source(source).size_of("s_t") == 2

    def test_a_forward_typedef_then_its_body_is_fine(self):
        source = dedent(
            """
            typedef struct s s_t;
            typedef struct s { uint32_t a; } s_t;
            """
        )
        assert parse_source(source).size_of("s_t") == 4

    def test_two_unpackable_meanings_do_not_poison_the_header(self):
        # u_t cannot be packed either way; the rest of the header can.
        source = dedent(
            """
            #if CONFIG_MACRO(1)
            typedef union { uint8_t a; } u_t;
            #else
            typedef unsigned int u_t;
            #endif
            typedef struct { uint8_t a; } s_t;
            """
        )
        assert parse_source(source).size_of("s_t") == 1

    def test_struct_defined_twice_under_an_unfoldable_condition(self):
        source = dedent(
            """
            #if CONFIG_MACRO(1)
            typedef struct { uint32_t a; } s_t;
            #else
            typedef struct { uint16_t a; } s_t;
            #endif
            """
        )
        with pytest.raises(UnsupportedTypeError, match="more than once"):
            parse_source(source)

    def test_syntax_error(self):
        with pytest.raises(ParseError):
            parse_source("typedef struct { uint8_t a; s_t;")

    def test_a_syntax_error_is_located(self):
        with pytest.raises(ParseError, match="line 3"):
            parse_source("typedef struct {\n    uint8_t a;\n} ;;; s_t;\n")

    def test_a_missing_final_newline_is_not_an_error(self):
        # A directive on an unterminated last line otherwise fails to parse.
        schema = parse_source(b"typedef struct { uint8_t a; } s_t;\n#define X 1")
        assert [s.name for s in schema] == ["s_t"]

    def test_const_members(self):
        with pytest.raises(UnsupportedTypeError, match="const"):
            parse_source("typedef struct { const uint8_t a; } s_t;")

    def test_error_message_names_the_file_and_line(self):
        source = "typedef struct {\n    uint32_t a : 3;\n} s_t;"
        with pytest.raises(UnsupportedTypeError, match=r"packet\.h: line 2"):
            parse_source(source, filename="packet.h")


class TestConditionalCompilation:
    """`#if` is folded where it can be, so dead branches are not emitted.

    Walking every branch is what makes include guards work, but applied
    blindly it also emits the structs inside `#if 0` -- and the generated code
    then names a type the header does not define, so it will not compile.
    """

    def test_if_zero_is_skipped(self):
        schema = parse_source(
            dedent(
                """
                #if 0
                typedef struct { uint8_t old; uint16_t junk; } dead_t;
                #endif
                typedef struct { uint8_t x; } live_t;
                """
            )
        )
        assert [s.name for s in schema] == ["live_t"]

    def test_if_one_is_kept(self):
        schema = parse_source("#if 1\ntypedef struct { uint8_t a; } k_t;\n#endif")
        assert [s.name for s in schema] == ["k_t"]

    def test_ifdef_of_an_unknown_macro_is_skipped(self):
        # packgen does not follow #include, so a macro it has not seen is
        # treated as undefined -- the same assumption a default build makes.
        schema = parse_source(
            dedent(
                """
                #ifdef DEBUG
                typedef struct { uint8_t d; } dbg_t;
                #endif
                typedef struct { uint8_t x; } live_t;
                """
            )
        )
        assert [s.name for s in schema] == ["live_t"]

    def test_the_else_branch_is_taken_instead(self):
        schema = parse_source(
            dedent(
                """
                #ifdef DEBUG
                typedef struct { uint8_t d; } picked_t;
                #else
                typedef struct { uint16_t r; } picked_t;
                #endif
                """
            )
        )
        assert schema.size_of("picked_t") == 2

    def test_elif_chains(self):
        schema = parse_source(
            dedent(
                """
                #if 0
                typedef struct { uint8_t a; } one_t;
                #elif 1
                typedef struct { uint8_t b; } two_t;
                #else
                typedef struct { uint8_t c; } three_t;
                #endif
                """
            )
        )
        assert [s.name for s in schema] == ["two_t"]

    def test_defined_and_comparisons_fold(self):
        schema = parse_source(
            dedent(
                """
                #if defined(FOO) && VER > 2
                typedef struct { uint8_t a; } no_t;
                #endif
                typedef struct { uint8_t x; } live_t;
                """
            )
        )
        assert [s.name for s in schema] == ["live_t"]

    def test_a_known_macro_makes_the_branch_live(self):
        schema = parse_source(
            dedent(
                """
                #define VER 3
                #if VER >= 2
                typedef struct { uint8_t a; } yes_t;
                #endif
                """
            )
        )
        assert [s.name for s in schema] == ["yes_t"]

    def test_an_unfoldable_condition_still_takes_every_branch(self):
        # Nothing is known about CONFIG_MACRO(1), so dropping the struct would
        # be a guess in the other direction.
        schema = parse_source(
            dedent(
                """
                #if CONFIG_MACRO(1)
                typedef struct { uint8_t a; } maybe_t;
                #endif
                """
            )
        )
        assert [s.name for s in schema] == ["maybe_t"]


class TestHostileInput:
    """packgen should refuse bad headers, never crash on them.

    A generator that dies with a Python traceback is both a bad experience
    and a sign something unbounded slipped through.
    """

    def test_random_bytes(self):
        with pytest.raises(ParseError):
            parse_source(bytes(range(256)), filename="t.h")

    def test_non_utf8_bytes_in_a_comment(self):
        # Latin-1 in a comment is common in older headers.
        schema = parse_source(
            "typedef struct { uint8_t a; } s_t; /* caf\xe9 */".encode("latin-1")
        )
        assert [s.name for s in schema] == ["s_t"]

    def test_utf8_bom(self):
        schema = parse_source(b"\xef\xbb\xbftypedef struct { uint8_t a; } s_t;")
        assert [s.name for s in schema] == ["s_t"]

    @pytest.mark.parametrize("expression", ["(8 >> -1)", "(1 << -3)", "(1 << 4000)"])
    def test_undefined_shifts_are_refused_cleanly(self, expression):
        # Python raises ValueError for a negative shift and eats memory for a
        # huge one; both are undefined in C and must read as packgen errors.
        source = f"#define N {expression}\ntypedef struct {{ uint8_t a[N]; }} s_t;"
        with pytest.raises(UnsupportedTypeError, match="shift"):
            parse_source(source).size_of("s_t")

    def test_deeply_nested_parentheses(self):
        source = (
            "#define N " + "(" * 500 + "1" + ")" * 500 + "\n"
            "typedef struct { uint8_t a[N]; } s_t;"
        )
        assert parse_source(source).size_of("s_t") == 1

    @pytest.mark.parametrize("bound", ["99999999999", "2147483648", "(1 << 62)"])
    def test_a_struct_too_big_for_ptrdiff_t_is_refused(self, bound):
        # The routines return ptrdiff_t, only guaranteed to 2**31-1 on a
        # 32-bit target, so a bigger struct would silently truncate there.
        source = f"#define N {bound}\ntypedef struct {{ uint8_t a[N]; }} s_t;"
        with pytest.raises(UnsupportedTypeError, match="byte limit"):
            parse_source(source).size_of("s_t")

    def test_the_largest_allowed_struct_is_accepted(self):
        schema = parse_source("typedef struct { uint8_t a[2147483647]; } s_t;")
        assert schema.size_of("s_t") == 2147483647

    def test_size_overflow_accumulates_across_fields(self):
        fields = " ".join(f"uint8_t f{i}[2000000000];" for i in range(2))
        with pytest.raises(UnsupportedTypeError, match="byte limit"):
            parse_source(f"typedef struct {{ {fields} }} s_t;").size_of("s_t")


class TestDeferredRejections:
    """Field types are only checked when a size is actually asked for, so a
    header holding one unpackable struct is still usable for the rest."""

    def test_unknown_field_type(self):
        schema = parse_source("typedef struct { missing_t a; } s_t;")
        with pytest.raises(UnknownTypeError, match="missing_t"):
            schema.size_of("s_t")

    def test_union_cannot_be_packed(self):
        schema = parse_source(
            dedent(
                """
                typedef union { uint32_t a; float b; } u_t;
                typedef struct { u_t a; } s_t;
                """
            )
        )
        with pytest.raises(UnsupportedTypeError, match="union"):
            schema.size_of("s_t")

    def test_enum_field_cannot_be_packed(self):
        schema = parse_source(
            dedent(
                """
                typedef enum { A, B } e_t;
                typedef struct { e_t a; } s_t;
                """
            )
        )
        with pytest.raises(UnsupportedTypeError, match="enum"):
            schema.size_of("s_t")

    def test_forward_declared_struct(self):
        schema = parse_source(
            dedent(
                """
                typedef struct elsewhere_tag elsewhere_t;
                typedef struct { elsewhere_t a; } s_t;
                """
            )
        )
        with pytest.raises(UnsupportedTypeError, match="#include"):
            schema.size_of("s_t")

    def test_pointer_typedef_says_so(self):
        # The reason is keyed off the declared name, not the raw declarator
        # text, so `A * pa` does not get filed under ' pa'.
        schema = parse_source(
            dedent(
                """
                typedef struct { uint8_t q; } inner_t;
                typedef inner_t * pinner_t;
                typedef struct { pinner_t f; } s_t;
                """
            )
        )
        with pytest.raises(UnsupportedTypeError, match="pointers cannot be packed"):
            schema.size_of("s_t")

    def test_array_typedef_says_so(self):
        schema = parse_source(
            dedent(
                """
                typedef uint8_t buf_t[4];
                typedef struct { buf_t f; } s_t;
                """
            )
        )
        with pytest.raises(UnsupportedTypeError, match="array types"):
            schema.size_of("s_t")

    def test_a_packable_struct_beside_an_unpackable_one_still_works(self):
        schema = parse_source(
            dedent(
                """
                typedef struct { uint32_t *ptr; } bad_t;
                typedef struct { uint32_t a; } good_t;
                """
            )
        )
        assert schema.size_of("good_t") == 4
        with pytest.raises(UnsupportedTypeError, match="pointer"):
            schema.size_of("bad_t")


class TestTreeSitterCoupling:
    """packgen is pinned to a tree-sitter API that has broken before.

    Language(path, "c") plus set_language() became
    Language(tree_sitter_c.language()) plus Parser(lang) at 0.22.  The
    dependency is capped in pyproject.toml because of it; these assert
    the specific calls packgen makes, so a future break fails here with
    a clear reason rather than as a hundred confusing parse errors.
    """

    def test_the_language_and_parser_constructors_take_what_we_pass(self):
        import tree_sitter_c
        from tree_sitter import Language, Parser

        parser = Parser(Language(tree_sitter_c.language()))
        tree = parser.parse(b"typedef struct { int a; } s_t;\n")
        assert tree.root_node.type == "translation_unit"
        assert not tree.root_node.has_error

    def test_installed_tree_sitter_is_within_the_declared_cap(self):
        """Catches a lockfile or CI that resolved outside the pin."""
        from importlib.metadata import version

        import tomllib
        from packaging.requirements import Requirement
        from packaging.version import Version

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        declared = tomllib.loads(pyproject.read_text())["project"]["dependencies"]
        for raw in declared:
            requirement = Requirement(raw)
            installed = Version(version(requirement.name))
            assert requirement.specifier.contains(installed), (
                f"{requirement.name} {installed} is outside {requirement.specifier}"
            )


class TestFunctionLikeMacros:
    """A function-like macro is still a defined name.

    packgen cannot expand one, but #ifdef of it is true, and its name
    still occupies the identifier namespace the generated code writes
    into.  Collecting only object-like #defines made both of those
    wrong.
    """

    def test_ifdef_of_a_function_like_macro_is_true(self):
        """This one silently dropped the struct before it was fixed."""
        schema = parse_source(
            dedent(
                """
                #include <stdint.h>
                #define HAVE_EXT(x) x
                typedef struct { uint16_t a; } base_t;
                #ifdef HAVE_EXT
                typedef struct { uint32_t b; } ext_t;
                #endif
                """
            ).encode()
        )
        assert [s.name for s in schema] == ["base_t", "ext_t"]

    def test_ifndef_of_a_function_like_macro_is_false(self):
        schema = parse_source(
            dedent(
                """
                #include <stdint.h>
                #define HAVE_EXT(x) x
                #ifndef HAVE_EXT
                typedef struct { uint32_t b; } dead_t;
                #endif
                typedef struct { uint16_t a; } live_t;
                """
            ).encode()
        )
        assert [s.name for s in schema] == ["live_t"]

    def test_its_name_reaches_the_schema(self):
        schema = parse_source(b"#define MAX(a, b) 0\ntypedef struct { char c; } s_t;\n")
        assert "MAX" in schema.macros

    def test_cannot_be_used_as_an_array_bound(self):
        with pytest.raises(UnsupportedTypeError, match="function-like macro"):
            parse_source(
                b"#include <stdint.h>\n"
                b"#define N(x) 4\n"
                b"typedef struct { uint8_t a[N]; } s_t;\n"
            ).size_of("s_t")


class TestMacroNamesAreCollected:
    def test_object_like_defines_reach_the_schema(self):
        schema = parse_source(b"#define NCHAN 8\ntypedef struct { char c; } s_t;\n")
        assert "NCHAN" in schema.macros

    def test_a_header_with_no_macros_has_none(self):
        schema = parse_source(b"typedef struct { char c; } s_t;\n")
        assert schema.macros == frozenset()
