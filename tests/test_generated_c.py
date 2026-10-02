"""End-to-end tests: compile the generated C and check what it actually does.

These are the tests that catch a generator bug the textual assertions miss.
They are skipped when there is no C compiler on the box.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from tests.conftest import BASE_CFLAGS, C_STANDARDS, WARNINGS, sanitizer_flags
from tests.reference import (
    exercise_all,
    expected_output,
    harness,
    leaves,
    pack,
    random_values,
)

HEADERS = Path(__file__).parent / "headers"

SCALARS = """
typedef struct {
    uint8_t  u8;
    int8_t   i8;
    char     c;
    bool     b;
    uint16_t u16;
    int16_t  i16;
    uint32_t u32;
    int32_t  i32;
    uint64_t u64;
    int64_t  i64;
    float    f;
    double   d;
} scalars_t;
"""

NESTED = (
    SCALARS
    + """
#define NCHAN 3

typedef uint32_t word_t;

typedef struct {
    scalars_t chan[NCHAN];
    word_t    grid[2][3];
    uint8_t   raw[4];
    char      name[8];
} outer_t;
"""
)


def run(binary: Path, stdin: bytes) -> tuple[str, bytes]:
    """Run the harness, returning its printed fields and re-encoded bytes."""
    result = subprocess.run([str(binary)], input=stdin, capture_output=True)
    assert result.returncode == 0, (
        f"harness exited {result.returncode}: {result.stderr!r}"
    )
    return result.stdout.decode(), result.stderr


@pytest.mark.parametrize("endian", ["big", "little"])
@pytest.mark.parametrize(
    "source, top",
    [
        pytest.param(SCALARS, "scalars_t", id="scalars"),
        pytest.param(NESTED, "outer_t", id="nested"),
    ],
)
def test_wire_format_matches_the_reference(generate, build, source, top, endian):
    """Every field lands at the right offset, in the right byte order."""
    project = generate(source, endian=endian)
    leafs = list(leaves(project.schema, top))
    values = random_values(leafs, seed=hash((top, endian)) & 0xFFFF)
    wire = pack(leafs, values, endian)

    assert len(wire) == project.schema.size_of(top)

    binary = build(project, harness(project.schema, top, leafs, "packet_unpack.h"))
    printed, re_encoded = run(binary, wire)

    assert printed == expected_output(leafs, values)
    assert re_encoded == wire


def test_real_world_header_round_trips(generate, build):
    """The header this tool was written for, end to end."""
    source = HEADERS / "dbbcpacket.h"
    project = generate(source.read_text(), endian="big", name="dbbcpacket")
    top = "dbbc3_ddc_multicast_t"
    leafs = list(leaves(project.schema, top))
    values = random_values(leafs, seed=7)
    wire = pack(leafs, values, "big")

    binary = build(project, harness(project.schema, top, leafs, "dbbcpacket_unpack.h"))
    printed, re_encoded = run(binary, wire)

    assert printed == expected_output(leafs, values)
    assert re_encoded == wire


def test_header_with_the_full_extern_c_idiom(generate, build):
    """The shape a real hand-written header has, end to end."""
    project = generate(
        dedent(
            """
            #include <stdint.h>
            #include <stdbool.h>

            #ifdef __cplusplus
            extern "C" {
            #endif

            #define NSAMPLES 4

            typedef struct {
                uint32_t seq;
                int16_t  samples[NSAMPLES];
                char     label[8];
            } s_t;

            #ifdef __cplusplus
            }
            #endif
            """
        )
    )
    leafs = list(leaves(project.schema, "s_t"))
    values = random_values(leafs, seed=11)
    wire = pack(leafs, values, "big")

    binary = build(project, harness(project.schema, "s_t", leafs, "packet_unpack.h"))
    printed, re_encoded = run(binary, wire)

    assert printed == expected_output(leafs, values)
    assert re_encoded == wire


ND_CASES = {
    "scalars": "typedef struct {{ uint16_t a{dims}; uint8_t tail; }} s_t;",
    "chars": "typedef struct {{ char a{dims}; uint8_t tail; }} s_t;",
    "structs": (
        "typedef struct {{ uint16_t v; uint8_t w; }} inner_t;\n"
        "typedef struct {{ inner_t a{dims}; uint8_t tail; }} s_t;"
    ),
    "nested_arrays": (
        "typedef struct {{ uint16_t g[2][2]; }} inner_t;\n"
        "typedef struct {{ inner_t a{dims}; uint8_t tail; }} s_t;"
    ),
}


@pytest.mark.parametrize("kind", list(ND_CASES))
@pytest.mark.parametrize("rank", [1, 2, 3, 4])
def test_arrays_of_any_rank(generate, build, rank, kind):
    """Arrays are not limited to two dimensions.

    The loop nest and the accessor are built separately, so a rank the tests
    never reach is a rank where they could silently drift apart.
    """
    dims = "".join(f"[{d}]" for d in range(2, 2 + rank))
    project = generate(ND_CASES[kind].format(dims=dims))

    leafs = list(leaves(project.schema, "s_t"))
    values = random_values(leafs, seed=rank * 10 + len(kind))
    wire = pack(leafs, values, "big")
    assert len(wire) == project.schema.size_of("s_t")

    binary = build(project, harness(project.schema, "s_t", leafs, "packet_unpack.h"))
    printed, re_encoded = run(binary, wire)

    assert printed == expected_output(leafs, values)
    assert re_encoded == wire


ND_JSON_MAIN = """
#include <assert.h>
#include <string.h>
#include "packet_json.h"

int main(void)
{
    s_t t;
    json_t *root;
    json_t *level;

    memset(&t, 0, sizeof t);
    t.a[1][2][1].v = 7;
    memcpy(t.names[1][2], "hi", 3);

    root = marshal_json_s_t(&t);
    assert(root != NULL);

    /* Three array dimensions of structs means three levels of JSON array. */
    level = json_object_get(root, "a");
    assert(json_array_size(level) == 2);
    level = json_array_get(level, 1);
    assert(json_array_size(level) == 3);
    level = json_array_get(level, 2);
    assert(json_array_size(level) == 2);
    level = json_array_get(level, 1);
    assert(json_integer_value(json_object_get(level, "v")) == 7);

    /* A 3-D char array is two levels of array with strings at the leaves. */
    level = json_object_get(root, "names");
    assert(json_array_size(level) == 2);
    level = json_array_get(level, 1);
    assert(json_array_size(level) == 3);
    assert(strcmp(json_string_value(json_array_get(level, 2)), "hi") == 0);

    json_decref(root);
    return 0;
}
"""


def test_json_arrays_of_any_rank(generate, build, jansson):
    cflags, ldflags = jansson
    project = generate(
        """
        typedef struct { uint16_t v; uint8_t w; } inner_t;
        typedef struct {
            inner_t a[2][3][2];
            char    names[2][3][5];
            uint8_t tail;
        } s_t;
        """,
        with_json=True,
    )
    binary = build(
        project,
        ND_JSON_MAIN,
        extra_cflags=tuple(cflags),
        extra_ldflags=tuple(ldflags),
    )
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("endian", ["big", "little"])
def test_every_entry_point_on_every_path(generate, build, endian):
    """Drive all of it under the sanitisers, not just the top struct.

    A sanitiser only checks what runs, and the round-trip harness only calls
    the top struct's routines -- which left the entry points of nested types
    unexecuted. This walks every struct's unmarshal and marshal through
    success, both NULL branches and the short-buffer branch, at byte patterns
    sitting on the representation boundaries.
    """
    project = generate(
        (HEADERS / "dbbcpacket.h").read_text(), endian=endian, name="dbbcpacket"
    )
    binary = build(project, exercise_all(project.schema, "dbbcpacket_unpack.h"))
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"exercise harness failed at line {result.returncode}: {result.stderr}"
    )


HYGIENE_MAIN = """
/* The generated header must compile as the very first thing in the file... */
#include "packet_unpack.h"
/* ...and including it twice must be harmless. */
#include "packet_unpack.h"

#include <stdio.h>

/* len_X has to be a compile-time constant: usable as an array bound... */
static uint8_t fixed_buffer[len_s_t];
/* ...and inside a static assertion, which callers need too. */
PACKGEN_STATIC_ASSERT(len_s_t == 8, len_s_t_should_be_eight);

int main(void)
{
    s_t s;

    if (unmarshal_s_t(&s, fixed_buffer, sizeof fixed_buffer) != (ptrdiff_t)len_s_t)
        return 1;
    puts("ok");
    return 0;
}
"""


def test_header_hygiene(generate, build):
    """Self-contained, idempotent, and its length macro is a real constant.

    A header that only compiles when something else was included first is a
    classic generated-code bug, and `len_X` is only worth being a macro if a
    caller can actually use it where a constant is required.
    """
    project = generate("typedef struct { uint32_t a; char b[4]; } s_t;")
    binary = build(project, HYGIENE_MAIN)
    assert subprocess.run([str(binary)], capture_output=True).returncode == 0


def test_two_generated_headers_coexist(generate, build, tmp_path, compiler):
    """Nothing in a generated header may collide with another one's."""
    first = generate("typedef struct { uint32_t a; } s_t;", name="packet")
    second = generate("typedef struct { uint16_t z; } o_t;", name="other")
    main = tmp_path / "both.c"
    main.write_text(
        '#include "packet_unpack.h"\n'
        '#include "other_unpack.h"\n'
        "PACKGEN_STATIC_ASSERT(len_s_t == 4, s_len);\n"
        "PACKGEN_STATIC_ASSERT(len_o_t == 2, o_len);\n"
        "int main(void) { return 0; }\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            compiler,
            *BASE_CFLAGS,
            f"-I{tmp_path}",
            str(main),
            *[str(s) for s in first.sources + second.sources],
            "-o",
            str(tmp_path / "both"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_generated_code_has_no_mutable_global_state(generate, compiler, tmp_path):
    """No globals means the routines are reentrant and thread safe."""
    project = generate("typedef struct { uint32_t a; char b[4]; } s_t;")
    obj = tmp_path / "o.o"
    subprocess.run(
        [
            compiler,
            "-std=c99",
            "-O1",
            f"-I{project.directory}",
            "-c",
            str(project.sources[0]),
            "-o",
            str(obj),
        ],
        check=True,
        capture_output=True,
    )
    symbols = subprocess.run(
        ["nm", str(obj)], capture_output=True, text=True, check=True
    ).stdout
    writable = []
    for line in symbols.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        kind, name = parts[-2], parts[-1]
        # b/B = BSS, d/D = data. Assembler-local labels (ltmp0, L_.str) are
        # section bookkeeping, not program state.
        if kind in "bBdD" and not name.startswith(("l", "L", ".")):
            writable.append(line.strip())
    assert not writable, f"generated code has mutable global state: {writable}"


CANARY_MAIN = """
#include <stdint.h>
#include <stdio.h>

int main(void)
{
    volatile int shift = 33;
    volatile int32_t v = 1;

    printf("%d\\n", (int)(v << shift));
    return 0;
}
"""


def test_the_sanitisers_are_actually_enabled(generate, build, compiler):
    """A guard against the suite going quietly inert.

    Every other test here asserts that sanitised code does *not* trap. If the
    sanitiser flags stopped taking effect they would all still pass, so this
    one compiles deliberate undefined behaviour the same way and insists it
    is caught.
    """
    if not sanitizer_flags(compiler):
        pytest.skip(f"{compiler} has no usable sanitisers")
    project = generate("typedef struct { uint8_t a; } s_t;")
    binary = build(project, CANARY_MAIN)
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode != 0, (
        "undefined behaviour went undetected - the sanitiser flags are not "
        "reaching the compiler, so the other end-to-end tests prove nothing"
    )
    assert "shift exponent" in result.stderr


SEMANTICS_MAIN = """
#include <assert.h>
#include <string.h>
#include "packet_unpack.h"

int main(void)
{
    s_t t;
    uint8_t in[len_s_t], out[len_s_t];
    uint32_t fbits = 0x7FA00001u;             /* signalling NaN */
    uint64_t dbits = 0x7FF0000000000000ull;   /* +infinity */
    int i;

    memset(in, 0, sizeof in);
    in[0] = 0x05;                             /* a bool byte that is not 0 or 1 */
    for (i = 0; i < 4; i++) in[1 + i] = (uint8_t)(fbits >> (8 * (3 - i)));
    for (i = 0; i < 8; i++) in[5 + i] = (uint8_t)(dbits >> (8 * (7 - i)));

    assert(unmarshal_s_t(&t, in, sizeof in) == (ptrdiff_t)len_s_t);
    assert(marshal_s_t(&t, out, sizeof out) == (ptrdiff_t)len_s_t);

    /* A bool is normalised on the way in, so it does not round trip byte
       for byte.  Everything else must. */
    assert(t.b == true);
    assert(out[0] == 0x01);
    assert(memcmp(in + 1, out + 1, 4) == 0);   /* NaN payload survives */
    assert(memcmp(in + 5, out + 5, 8) == 0);   /* infinity survives */
    return 0;
}
"""


def test_bool_normalisation_and_non_finite_reals(generate, build):
    """The two places where unmarshal then marshal is not the identity.

    Non-canonical bool bytes are normalised; NaN and infinity are copied bit
    for bit and must not be disturbed.
    """
    project = generate("typedef struct { bool b; float f; double d; } s_t;")
    binary = build(project, SEMANTICS_MAIN)
    assert subprocess.run([str(binary)], capture_output=True).returncode == 0


BOUNDS_MAIN = """
#include <assert.h>
#include <string.h>
#include "packet_unpack.h"

int main(void)
{
    s_t t;
    uint8_t buf[len_s_t + 8];

    memset(&t, 0, sizeof t);
    memset(buf, 0x5a, sizeof buf);

    /* A buffer one byte short must be refused, not read past. */
    assert(unmarshal_s_t(&t, buf, len_s_t - 1) == -1);
    assert(marshal_s_t(&t, buf, len_s_t - 1) == -1);
    assert(unmarshal_s_t(&t, buf, 0) == -1);

    /* NULL arguments must be refused too. */
    assert(unmarshal_s_t(NULL, buf, sizeof buf) == -1);
    assert(unmarshal_s_t(&t, NULL, sizeof buf) == -1);
    assert(marshal_s_t(NULL, buf, sizeof buf) == -1);
    assert(marshal_s_t(&t, NULL, sizeof buf) == -1);

    /* An oversized buffer is fine, and only len_s_t bytes are touched. */
    assert(unmarshal_s_t(&t, buf, sizeof buf) == (ptrdiff_t)len_s_t);
    memset(buf, 0, sizeof buf);
    assert(marshal_s_t(&t, buf, sizeof buf) == (ptrdiff_t)len_s_t);
    for (size_t i = len_s_t; i < sizeof buf; i++) assert(buf[i] == 0);

    return 0;
}
"""


def test_bounds_and_null_checks(generate, build):
    project = generate(
        """
        typedef struct { uint32_t a; char b[5]; } inner_t;
        typedef struct { inner_t a[2]; uint64_t b; } s_t;
        """
    )
    binary = build(project, BOUNDS_MAIN)
    assert subprocess.run([str(binary)], capture_output=True).returncode == 0


NESTED_SHORT_MAIN = """
#include <assert.h>
#include <string.h>
#include "packet_unpack.h"

int main(void)
{
    outer_t t;
    uint8_t buf[len_outer_t];

    memset(&t, 0, sizeof t);
    memset(buf, 0, sizeof buf);

    /* The nested call gets a shrinking budget; it must never see more than
       what is actually left in the buffer. */
    assert(unmarshal_outer_t(&t, buf, len_outer_t) == (ptrdiff_t)len_outer_t);
    assert(unmarshal_outer_t(&t, buf, len_outer_t - 1) == -1);
    return 0;
}
"""


def test_nested_length_budget(generate, build):
    project = generate(
        """
        typedef struct { uint32_t a; } inner_t;
        typedef struct { inner_t a[4]; uint8_t b; } outer_t;
        """
    )
    binary = build(project, NESTED_SHORT_MAIN)
    assert subprocess.run([str(binary)], capture_output=True).returncode == 0


@pytest.mark.parametrize("standard", C_STANDARDS)
def test_compiles_under_every_c_standard(generate, compiler, standard):
    """-pedantic is the part that matters: it holds the output to ISO C."""
    project = generate(
        NESTED + "\ntypedef struct { outer_t o; char s[4]; } top_t;\n",
        with_json=False,
    )
    result = subprocess.run(
        [
            compiler,
            f"-std={standard}",
            "-pedantic",
            *WARNINGS,
            "-fsyntax-only",
            f"-I{project.directory}",
            *[str(s) for s in project.sources],
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_header_uses_no_posix_only_types(generate):
    """ssize_t lives in <sys/types.h>, which MSVC does not ship."""
    project = generate("typedef struct { uint32_t a; } s_t;")
    for path in [project.directory / "packet_unpack.h", *project.sources]:
        text = path.read_text()
        assert "ssize_t" not in text, path.name
        assert "sys/types.h" not in text, path.name


def test_generated_code_compiles_as_cpp(generate, compiler, tmp_path):
    """The `extern "C"` wrapper means the headers work from C++ too."""
    project = generate(
        """
        typedef struct { uint32_t a; char b[5]; } inner_t;
        typedef struct { inner_t a[2]; double d; } s_t;
        """
    )
    main = project.directory / "main.cc"
    main.write_text(
        '#include "packet_unpack.h"\n'
        "int main() { s_t t; uint8_t b[len_s_t];"
        " return marshal_s_t(&t, b, sizeof b) > 0 ? 0 : 1; }\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            compiler,
            "-x",
            "c++",
            "-std=c++11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-fsyntax-only",
            f"-I{project.directory}",
            str(main),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


JSON_MAIN = """
#include <assert.h>
#include <string.h>
#include "packet_json.h"
#include "packet_unpack.h"

int main(void)
{
    s_t t;
    json_t *root;
    json_t *value;
    char *text;

    memset(&t, 0, sizeof t);
    t.count = 42;
    t.big = 18446744073709551615ull;
    t.flag = true;
    t.ratio = 0.5;
    memcpy(t.name, "hi", 3);
    t.samples[0] = 1;
    t.samples[1] = 2;
    t.samples[2] = 3;
    t.inner.a = 7;

    root = marshal_json_s_t(&t);
    assert(root != NULL);

    assert(json_integer_value(json_object_get(root, "count")) == 42);
    assert(json_is_true(json_object_get(root, "flag")));
    assert(json_real_value(json_object_get(root, "ratio")) == 0.5);

    /* uint64_t goes out as a string so the top bit survives. */
    value = json_object_get(root, "big");
    assert(json_is_string(value));
    assert(strcmp(json_string_value(value), "18446744073709551615") == 0);

    /* A fixed-size char field stops at its NUL, not at its declared size. */
    value = json_object_get(root, "name");
    assert(strcmp(json_string_value(value), "hi") == 0);

    value = json_object_get(root, "samples");
    assert(json_array_size(value) == 3);
    assert(json_integer_value(json_array_get(value, 2)) == 3);

    value = json_object_get(root, "inner");
    assert(json_integer_value(json_object_get(value, "a")) == 7);

    text = json_dumps(root, JSON_COMPACT);
    assert(text != NULL);
    free(text);
    json_decref(root);

    assert(marshal_json_s_t(NULL) == NULL);
    return 0;
}
"""


def test_json_output(generate, build, jansson):
    cflags, ldflags = jansson
    project = generate(
        """
        typedef struct { uint32_t a; } inner_t;
        typedef struct {
            uint32_t count;
            uint64_t big;
            bool     flag;
            double   ratio;
            char     name[8];
            uint16_t samples[3];
            inner_t  inner;
        } s_t;
        """,
        with_json=True,
    )
    binary = build(
        project,
        JSON_MAIN,
        extra_cflags=tuple(cflags),
        extra_ldflags=tuple(ldflags),
    )
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


JSON_NESTED_ARRAY_MAIN = """
#include <assert.h>
#include <string.h>
#include "packet_json.h"

int main(void)
{
    s_t t;
    json_t *root;
    json_t *outer;

    memset(&t, 0, sizeof t);
    for (int i = 0; i < 2; i++)
        for (int j = 0; j < 3; j++)
            t.grid[i][j] = (uint16_t)(i * 10 + j);

    root = marshal_json_s_t(&t);
    assert(root != NULL);

    outer = json_object_get(root, "grid");
    assert(json_array_size(outer) == 2);
    for (int i = 0; i < 2; i++) {
        json_t *row = json_array_get(outer, (size_t)i);
        assert(json_array_size(row) == 3);
        for (int j = 0; j < 3; j++)
            assert(json_integer_value(json_array_get(row, (size_t)j)) == i * 10 + j);
    }
    json_decref(root);
    return 0;
}
"""


def test_json_nested_arrays(generate, build, jansson):
    cflags, ldflags = jansson
    project = generate("typedef struct { uint16_t grid[2][3]; } s_t;", with_json=True)
    binary = build(
        project,
        JSON_NESTED_ARRAY_MAIN,
        extra_cflags=tuple(cflags),
        extra_ldflags=tuple(ldflags),
    )
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


JSON_UNREPRESENTABLE_MAIN = r"""
#include <assert.h>
#include <math.h>
#include <string.h>
#include "packet_json.h"

static const char *text(json_t *root, const char *key)
{
    json_t *v = json_object_get(root, key);
    assert(json_is_string(v));
    return json_string_value(v);
}

int main(void)
{
    s_t t;
    json_t *root;

    memset(&t, 0, sizeof t);
    t.nan = NAN;
    t.inf = INFINITY;
    t.ninf = -INFINITY;
    t.finite = 1.5;
    memcpy(t.latin1, "\xb0" "C", 2);         /* Latin-1 degree sign */
    memcpy(t.utf8, "\xc2\xb0" "C", 3);      /* the same, in UTF-8 */
    memcpy(t.cut, "ab\xe2\x82", 4);         /* a sequence cut short */
    memcpy(t.overlong, "\xc0\xaf", 2);      /* overlong '/' */
    memcpy(t.surrogate, "\xed\xa0\x80", 3); /* U+D800 */

    root = marshal_json_s_t(&t);
    assert(root != NULL);

    assert(strcmp(text(root, "nan"), "NaN") == 0);
    assert(strcmp(text(root, "inf"), "Infinity") == 0);
    assert(strcmp(text(root, "ninf"), "-Infinity") == 0);
    assert(json_real_value(json_object_get(root, "finite")) == 1.5);

    assert(strcmp(text(root, "latin1"), "\xef\xbf\xbd" "C") == 0);
    assert(strcmp(text(root, "utf8"), "\xc2\xb0" "C") == 0);
    assert(strcmp(text(root, "cut"), "ab\xef\xbf\xbd\xef\xbf\xbd") == 0);
    assert(strcmp(text(root, "overlong"), "\xef\xbf\xbd\xef\xbf\xbd") == 0);
    assert(strcmp(text(root, "surrogate"),
                  "\xef\xbf\xbd\xef\xbf\xbd\xef\xbf\xbd") == 0);

    json_decref(root);
    return 0;
}
"""


def test_json_renders_what_jansson_would_refuse(generate, build, jansson):
    # jansson returns NULL for NaN, infinity and invalid UTF-8, which the
    # generated code would take for an allocation failure and drop the
    # whole struct.  A NaN sentinel or one stray byte off the wire must not
    # make the JSON disappear.
    cflags, ldflags = jansson
    project = generate(
        """
        typedef struct {
            float nan;
            double inf;
            double ninf;
            double finite;
            char latin1[4];
            char utf8[4];
            char cut[4];
            char overlong[4];
            char surrogate[4];
        } s_t;
        """,
        with_json=True,
    )
    binary = build(
        project,
        JSON_UNREPRESENTABLE_MAIN,
        extra_cflags=tuple(cflags),
        extra_ldflags=tuple(ldflags),
    )
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_real_world_json_compiles(generate, build, jansson):
    cflags, ldflags = jansson
    project = generate(
        (HEADERS / "dbbcpacket.h").read_text(), name="dbbcpacket", with_json=True
    )
    main = dedent(
        """
        #include <assert.h>
        #include <string.h>
        #include "dbbcpacket_json.h"

        int main(void)
        {
            dbbc3_ddc_multicast_t t;
            json_t *root;
            memset(&t, 0, sizeof t);
            memcpy(t.version, "v1", 3);
            root = marshal_json_dbbc3_ddc_multicast_t(&t);
            assert(root != NULL);
            assert(json_array_size(json_object_get(root, "bbc")) == 128);
            json_decref(root);
            return 0;
        }
        """
    )
    binary = build(
        project, main, extra_cflags=tuple(cflags), extra_ldflags=tuple(ldflags)
    )
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
