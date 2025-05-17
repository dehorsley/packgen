from io import StringIO
from pathlib import Path
from textwrap import dedent

import pytest

from packgen import LengthGenerator, parse_file_with_fake_libc

basic_types = [
    ("char", 1),
    ("uint8_t", 1),
    ("uint16_t", 2),
    ("uint32_t", 4),
    ("uint64_t", 8),
    ("int8_t", 1),
    ("int16_t", 2),
    ("int32_t", 4),
    ("int64_t", 8),
    ("float", 4),
    ("double", 8),
]


def _length_generator(c_code: str, tmp_path: Path) -> str:
    """
    Helper function to generate the length of a C struct.
    """

    c_file = tmp_path / "test.c"
    c_file.write_text(c_code, encoding="utf-8")

    # Parse the C code
    ast = parse_file_with_fake_libc(str(c_file))

    # Create a LengthGenerator instance
    buf = StringIO()
    generator = LengthGenerator(str(c_file), output=buf)

    # Visit the AST nodes
    generator.visit(ast)

    # Compare the output with the expected output
    return buf.getvalue()


@pytest.mark.parametrize(
    "base_type, expected_size",
    basic_types,
)
def test_base_type_size(tmp_path, base_type, expected_size):
    """
    Test the size of base types.
    """
    # Create a simple C file with a typedef and an array declaration
    c_code = f"""
    #include <stdint.h>

    typedef struct {{
        {base_type} b;
    }} my_struct;

    """

    assert (
        _length_generator(c_code, tmp_path)
        == f"const size_t len_my_struct = {expected_size};\n"
    )


@pytest.mark.parametrize(
    "base_type, expected_size",
    basic_types,
)
def test_array_type_size(tmp_path, base_type, expected_size):
    """
    Test the size of array types.
    """
    # Create a simple C file with a typedef and an array declaration
    c_code = f"""
    #include <stdint.h>

    typedef struct {{
        {base_type} b[10];
    }} my_struct;

    """

    assert (
        _length_generator(c_code, tmp_path)
        == f"const size_t len_my_struct = {expected_size * 10};\n"
    )


@pytest.mark.parametrize(
    "c_code, expected_output",
    [
        (
            """
            #include <stdint.h>

            typedef struct {
                uint32_t a;
                char b[5];
            } my_struct;

            """,
            "const size_t len_my_struct = 9;\n",
        ),
        (
            """
            #include <stdint.h>

            typedef struct {
                uint32_t a;
                char b[5];
            } my_struct;

            typedef struct {
                uint32_t a;
                char b[5];
            } my_struct_2;

            """,
            dedent(
                """\
            const size_t len_my_struct = 9;
            const size_t len_my_struct_2 = 9;
            """
            ),
        ),
        (
            """
            #include <stdint.h>

            typedef struct {
                uint32_t a;
                char b[5];
            } my_struct;

            typedef struct {
                my_struct a;
            } my_struct_2;
            """,
            dedent(
                """\
            const size_t len_my_struct = 9;
            const size_t len_my_struct_2 = 9;
            """
            ),
        ),
        (
            """
            #include <stdint.h>

            typedef struct {
                uint32_t a;
                char b[5];
            } my_struct;

            typedef struct {
                my_struct a[10];
            } my_struct_2;
            """,
            dedent(
                """\
            const size_t len_my_struct = 9;
            const size_t len_my_struct_2 = 90;
            """
            ),
        ),
    ],
)
def test_length_generator(tmp_path, c_code, expected_output):
    """
    Test various C struct sizes.
    """

    assert _length_generator(c_code, tmp_path) == expected_output
