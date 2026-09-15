"""An independent Python implementation of the wire format.

The generated C is checked against this rather than against itself, so a
field emitted in the wrong order or the wrong byte order shows up as a
mismatch instead of cancelling out in a round trip.
"""

from __future__ import annotations

import math
import random
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import product

from packgen.model import BOOL_TYPES, Schema, Struct

#: Resolved type -> struct module format code.
FORMAT = {
    "char": "B",
    "uint8_t": "B",
    "int8_t": "b",
    "uint16_t": "H",
    "int16_t": "h",
    "uint32_t": "I",
    "int32_t": "i",
    "uint64_t": "Q",
    "int64_t": "q",
    "float": "f",
    "double": "d",
    "bool": "B",
    "_Bool": "B",
}

#: Resolved type -> the printf conversion the C harness uses.
PRINTF = {
    "char": '"%u", (unsigned)(unsigned char)',
    "uint8_t": '"%" PRIu8, ',
    "int8_t": '"%" PRId8, ',
    "uint16_t": '"%" PRIu16, ',
    "int16_t": '"%" PRId16, ',
    "uint32_t": '"%" PRIu32, ',
    "int32_t": '"%" PRId32, ',
    "uint64_t": '"%" PRIu64, ',
    "int64_t": '"%" PRId64, ',
    "bool": '"%d", (int)',
    "_Bool": '"%d", (int)',
}


@dataclass(frozen=True)
class Leaf:
    """One scalar in the packed stream."""

    accessor: str
    type: str


def leaves(schema: Schema, struct_: Struct | str, prefix: str = "t.") -> Iterator[Leaf]:
    """Every scalar of a struct, in the order it appears on the wire."""
    if isinstance(struct_, str):
        struct_ = schema[struct_]
    for field in struct_.fields:
        type_ = schema.resolve(field.type)
        indices = product(*(range(d) for d in field.dims)) if field.is_array else [()]
        for index in indices:
            accessor = prefix + field.name + "".join(f"[{i}]" for i in index)
            if schema.is_struct(type_):
                yield from leaves(schema, type_, prefix=accessor + ".")
            else:
                yield Leaf(accessor=accessor, type=type_)


def random_values(leafs: list[Leaf], seed: int = 0) -> list[float | int]:
    """A deterministic value for each leaf, within its type's range."""
    rng = random.Random(seed)
    values: list[float | int] = []
    for leaf in leafs:
        if leaf.type in BOOL_TYPES:
            values.append(rng.randint(0, 1))
        elif leaf.type in ("float", "double"):
            size = 4 if leaf.type == "float" else 8
            code = "f" if leaf.type == "float" else "d"
            while True:
                raw = rng.getrandbits(size * 8).to_bytes(size, "little")
                value = struct.unpack("<" + code, raw)[0]
                # Non-finite values survive the round trip but make the
                # expected output awkward to spell; finite ones are enough.
                if math.isfinite(value):
                    values.append(value)
                    break
        else:
            code = FORMAT[leaf.type]
            bits = struct.calcsize(code) * 8
            if code.islower():  # signed
                values.append(rng.randint(-(2 ** (bits - 1)), 2 ** (bits - 1) - 1))
            else:
                values.append(rng.randint(0, 2**bits - 1))
    return values


def pack(leafs: list[Leaf], values: list[float | int], endian: str) -> bytes:
    """The wire bytes for a set of leaf values."""
    prefix = ">" if endian == "big" else "<"
    codes = "".join(FORMAT[leaf.type] for leaf in leafs)
    return struct.pack(prefix + codes, *values)


def expected_output(leafs: list[Leaf], values: list[float | int]) -> str:
    """What the C harness should print, one leaf per line."""
    lines = []
    for leaf, value in zip(leafs, values, strict=True):
        if leaf.type == "float":
            (bits,) = struct.unpack("<I", struct.pack("<f", value))
            lines.append(f"{bits:08x}")
        elif leaf.type == "double":
            (bits,) = struct.unpack("<Q", struct.pack("<d", value))
            lines.append(f"{bits:016x}")
        elif leaf.type in BOOL_TYPES:
            lines.append(str(int(bool(value))))
        else:
            lines.append(str(value))
    return "".join(line + "\n" for line in lines)


def exercise_all(schema: Schema, generated_header: str) -> str:
    """A ``main.c`` that drives every entry point down every path.

    The round-trip harness only calls the top struct's routines, which leaves
    the entry points of nested types unexecuted -- and a sanitiser only checks
    what runs.  This walks all of them, success and both failure branches, at
    byte patterns chosen to sit on the representation boundaries where a sign
    or overflow bug would show up.
    """
    body: list[str] = []
    for definition in schema:
        name = definition.name
        if schema.size_of(definition) == 0:  # an empty struct is not C99 anyway
            continue
        body += [
            "    {",
            f"        {name} v;",
            f"        uint8_t buf[len_{name}];",
            f"        uint8_t out[len_{name}];",
            "        size_t k, i;",
            "",
            "        for (k = 0; k < sizeof PATTERNS; k++) {",
            "            memset(buf, PATTERNS[k], sizeof buf);",
            f"            if (unmarshal_{name}(&v, buf, sizeof buf)",
            f"                != (ptrdiff_t)len_{name}) return __LINE__;",
            f"            if (marshal_{name}(&v, out, sizeof out)",
            f"                != (ptrdiff_t)len_{name}) return __LINE__;",
            "        }",
            "        for (i = 0; i < sizeof buf; i++) buf[i] = (uint8_t)(i * 31 + 7);",
            f"        if (unmarshal_{name}(&v, buf, sizeof buf) "
            f"!= (ptrdiff_t)len_{name}) return __LINE__;",
            f"        if (marshal_{name}(&v, out, sizeof out) "
            f"!= (ptrdiff_t)len_{name}) return __LINE__;",
            "",
            "        /* Both sides of the NULL check, and the length check. */",
            f"        if (unmarshal_{name}(NULL, buf, sizeof buf) != -1)",
            "            return __LINE__;",
            f"        if (unmarshal_{name}(&v, NULL, sizeof buf) != -1)",
            "            return __LINE__;",
            f"        if (marshal_{name}(NULL, out, sizeof out) != -1)",
            "            return __LINE__;",
            f"        if (marshal_{name}(&v, NULL, sizeof out) != -1)",
            "            return __LINE__;",
            f"        if (unmarshal_{name}(&v, buf, len_{name} - 1) != -1)",
            "            return __LINE__;",
            f"        if (marshal_{name}(&v, out, len_{name} - 1) != -1)",
            "            return __LINE__;",
            "    }",
        ]

    return "\n".join(
        [
            "#include <stdint.h>",
            "#include <stdio.h>",
            "#include <string.h>",
            f'#include "{generated_header}"',
            "",
            "static const uint8_t PATTERNS[] = "
            "{ 0x00, 0xFF, 0x80, 0x7F, 0x01, 0xAA, 0x55 };",
            "",
            "int main(void)",
            "{",
            *body,
            '    puts("ok");',
            "    return 0;",
            "}",
            "",
        ]
    )


def harness(schema: Schema, name: str, leafs: list[Leaf], generated_header: str) -> str:
    """A ``main.c`` that unmarshals stdin and prints every field it decoded."""
    body = []
    for leaf in leafs:
        accessor = leaf.accessor
        if leaf.type in ("float", "double"):
            bits = "uint32_t" if leaf.type == "float" else "uint64_t"
            spec = "PRIx32" if leaf.type == "float" else "PRIx64"
            width = "08" if leaf.type == "float" else "016"
            body.append("    {")
            body.append(f"        {bits} raw;")
            body.append(f"        memcpy(&raw, &{accessor}, sizeof raw);")
            body.append(f'        printf("%{width}" {spec} "\\n", raw);')
            body.append("    }")
        else:
            conversion = PRINTF[leaf.type]
            body.append(f"    printf({conversion}{accessor});")
            body.append("    putchar('\\n');")

    return "\n".join(
        [
            "#include <inttypes.h>",
            "#include <stdio.h>",
            "#include <stdlib.h>",
            "#include <string.h>",
            f'#include "{generated_header}"',
            "",
            "int main(void)",
            "{",
            f"    {name} t;",
            f"    uint8_t buf[len_{name} ? len_{name} : 1];",
            "    ptrdiff_t used;",
            "",
            "    memset(&t, 0, sizeof t);",
            f"    if (fread(buf, 1, len_{name}, stdin) != len_{name}) return 2;",
            f"    used = unmarshal_{name}(&t, buf, len_{name});",
            f"    if (used != (ptrdiff_t)len_{name}) return 3;",
            "",
            *body,
            "",
            "    /* Re-encode and hand the bytes back for comparison. */",
            "    memset(buf, 0, sizeof buf);",
            f"    if (marshal_{name}(&t, buf, len_{name})",
            f"        != (ptrdiff_t)len_{name}) return 4;",
            f"    if (fwrite(buf, 1, len_{name}, stderr) != len_{name}) return 5;",
            "    return 0;",
            "}",
            "",
        ]
    )
