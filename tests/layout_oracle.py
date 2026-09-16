"""Check packgen's parse against the C compiler's own view of the header.

Every other test in this suite validates the *emitter*.  The differential
tests in reference.py look independent, but they walk the same Schema
packgen produced, so a parser bug produces a matching wrong reference and
matching wrong C that agree with each other perfectly.  Reversing every
struct's field order in the parser passes 60 differential cases and every
CBMC proof.

This closes that hole by asking the only authority on what a C header
means -- a C compiler.  A probe program built against the *original*
header reports offsetof, sizeof and alignof for every field packgen
claims, and the results are checked against the Schema:

* offsets must increase in packgen's field order, and fields must not
  overlap -- catches reordering;
* each field's byte span must match packgen's element size times its
  element count -- catches a wrong array bound or a misresolved type;
* every gap must be smaller than the struct's alignment, so it is
  padding and not a field packgen failed to see.  Padding before a field
  is always less than that field's alignment, which in turn divides the
  struct's, so this is a sound bound -- and a portable one, where asking
  for a field's own alignment would need the __typeof__ extension;
* the in-memory struct must be at least the packed size.

The probe is built as C11 so it can use _Alignof.  That is a test-only
requirement and says nothing about the C99 output packgen generates.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from packgen.errors import PackgenError
from packgen.model import Schema, Struct


@dataclass(frozen=True)
class Measured:
    """What the compiler says about one struct."""

    size: int
    align: int
    offsets: dict[str, int]
    sizes: dict[str, int]


def packable(schema: Schema, struct: Struct) -> bool:
    """Whether packgen would generate code for this struct at all."""
    try:
        schema.size_of(struct)
    except PackgenError:
        return False
    return all(not f.is_pointer for f in struct.fields)


def probe_source(schema: Schema, header_name: str) -> str:
    """A C program printing the real layout of every struct packgen found."""
    lines = [
        "#include <stdalign.h>",
        "#include <stddef.h>",
        "#include <stdio.h>",
        f'#include "{header_name}"',
        "",
        "int main(void)",
        "{",
    ]
    for struct in schema:
        if not packable(schema, struct):
            continue
        name = struct.name
        lines.append(
            f'    printf("S {name} %zu %zu\\n", sizeof({name}), _Alignof({name}));'
        )
        for field in struct.fields:
            lines.append(
                f'    printf("F {name} {field.name} %zu %zu\\n", '
                f"offsetof({name}, {field.name}), "
                f"sizeof(((({name} *)0)->{field.name})));"
            )
    lines += ["    return 0;", "}", ""]
    return "\n".join(lines)


def measure(
    compiler: str, header: Path, schema: Schema, work: Path
) -> dict[str, Measured]:
    """Compile and run the probe, returning the compiler's layout."""
    source = work / "layout_probe.c"
    source.write_text(probe_source(schema, header.name), encoding="utf-8")
    binary = work / "layout_probe"
    build = subprocess.run(
        [compiler, "-std=c11", f"-I{header.parent}", str(source), "-o", str(binary)],
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        raise AssertionError(f"layout probe did not compile:\n{build.stderr}")

    run = subprocess.run([str(binary)], capture_output=True, text=True)
    if run.returncode != 0:
        raise AssertionError(f"layout probe did not run:\n{run.stderr}")

    sizes: dict[str, int] = {}
    aligns: dict[str, int] = {}
    fields: dict[str, dict[str, tuple[int, int]]] = {}
    for line in run.stdout.splitlines():
        parts = line.split()
        if parts[0] == "S":
            _, name, size, align = parts
            sizes[name] = int(size)
            aligns[name] = int(align)
            fields.setdefault(name, {})
        else:
            _, struct_name, field_name, offset, size = parts
            fields[struct_name][field_name] = (int(offset), int(size))

    return {
        name: Measured(
            size=sizes[name],
            align=aligns[name],
            offsets={f: v[0] for f, v in fields[name].items()},
            sizes={f: v[1] for f, v in fields[name].items()},
        )
        for name in sizes
    }


def _element_size(schema: Schema, type_: str, measured: dict[str, Measured]) -> int:
    """In-memory size of one element of `type_`.

    A nested struct's in-memory size includes padding, so it has to come
    from the compiler rather than from packgen's packed arithmetic --
    using packgen's number here would let a parser bug cancel itself out.
    """
    resolved = schema.resolve(type_)
    if schema.is_struct(resolved):
        return measured[resolved].size
    return schema.size_of_type(resolved)


def discrepancies(schema: Schema, measured: dict[str, Measured]) -> list[str]:
    """Every way the compiler's layout contradicts packgen's Schema."""
    problems: list[str] = []

    for struct in schema:
        if not packable(schema, struct) or struct.name not in measured:
            continue
        real = measured[struct.name]
        previous_end = 0
        previous_name = "the start of the struct"

        for field in struct.fields:
            offset = real.offsets[field.name]
            size = real.sizes[field.name]

            if offset < previous_end:
                problems.append(
                    f"{struct.name}.{field.name} starts at {offset}, which "
                    f"overlaps {previous_name} ending at {previous_end}; "
                    f"packgen has it after"
                )

            gap = offset - previous_end
            if gap >= real.align:
                problems.append(
                    f"{struct.name}: {gap} unexplained bytes between "
                    f"{previous_name} and {field.name} -- too many to be "
                    f"padding for alignment {real.align}, so packgen may "
                    f"have missed a field"
                )

            expected = _element_size(schema, field.type, measured) * max(field.count, 1)
            if size != expected:
                problems.append(
                    f"{struct.name}.{field.name} is {size} bytes, but packgen "
                    f"expects {expected} ({field.type}"
                    f"{''.join(f'[{d}]' for d in field.dims)})"
                )

            previous_end = offset + size
            previous_name = field.name

        trailing = real.size - previous_end
        if trailing >= real.align:
            problems.append(
                f"{struct.name}: {trailing} unexplained bytes after "
                f"{previous_name} -- too many to be tail padding for "
                f"alignment {real.align}, so packgen may have missed a field"
            )

        packed = schema.size_of(struct)
        if real.size < packed:
            problems.append(
                f"{struct.name} is {real.size} bytes in memory but packgen "
                f"packs it into {packed}"
            )

    return problems


def check(compiler: str, header: Path, schema: Schema, work: Path) -> list[str]:
    return discrepancies(schema, measure(compiler, header, schema, work))
