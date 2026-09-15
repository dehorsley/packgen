"""Generate random but valid headers, for differential testing.

Hand-written cases only cover what someone thought to write down.  These are
fed through the generator and compared against the Python reference in
``tests.reference``, which was written independently of the C emitter.
"""

from __future__ import annotations

import random

SCALARS = [
    "uint8_t",
    "int8_t",
    "char",
    "bool",
    "uint16_t",
    "int16_t",
    "uint32_t",
    "int32_t",
    "uint64_t",
    "int64_t",
    "float",
    "double",
]


def random_header(
    seed: int, *, structs: int = 4, max_fields: int = 6, max_bytes: int = 4096
) -> tuple[str, str]:
    """Return ``(header_text, name_of_top_struct)``.

    Structs are emitted in dependency order, so a field may refer to any
    struct already defined -- which is the only thing C allows anyway.
    """
    rng = random.Random(seed)
    defined: list[tuple[str, int]] = []  # (name, packed size)
    lines = ["#include <stdbool.h>", "#include <stdint.h>", ""]

    for index in range(structs):
        name = f"s{index}_t"
        fields: list[str] = []
        size = 0
        for slot in range(rng.randint(1, max_fields)):
            field = f"f{slot}"
            # Prefer scalars; nest into an earlier struct sometimes.
            if defined and rng.random() < 0.3:
                type_name, unit = defined[rng.randrange(len(defined))]
            else:
                type_name = rng.choice(SCALARS)
                unit = {
                    "uint8_t": 1,
                    "int8_t": 1,
                    "char": 1,
                    "bool": 1,
                    "uint16_t": 2,
                    "int16_t": 2,
                    "uint32_t": 4,
                    "int32_t": 4,
                    "float": 4,
                    "uint64_t": 8,
                    "int64_t": 8,
                    "double": 8,
                }[type_name]

            # Arrays of any rank are supported, so generate up to four
            # dimensions -- the loop nesting and the accessor both have to
            # stay in step past the two dimensions a hand-written test covers.
            roll = rng.random()
            if roll < 0.5:
                dims: tuple[int, ...] = ()
            elif roll < 0.8:
                dims = (rng.randint(1, 4),)
            else:
                rank = rng.randint(2, 4)
                dims = tuple(rng.randint(1, 3) for _ in range(rank))

            count = 1
            for extent in dims:
                count *= extent
            if size + unit * count > max_bytes:
                continue
            size += unit * count

            suffix = "".join(f"[{d}]" for d in dims)
            fields.append(f"    {type_name} {field}{suffix};")

        if not fields:  # an empty struct is a GNU extension, not C99
            fields.append("    uint8_t f0;")
            size = 1

        lines.append("typedef struct {")
        lines.extend(fields)
        lines.append(f"}} {name};")
        lines.append("")
        defined.append((name, size))

    return "\n".join(lines), defined[-1][0]
