# This file covered by GPL 3 license
# C. David Horsley 2025
"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from packgen.errors import PackgenError
from packgen.generators import json as json_gen
from packgen.generators import pack as pack_gen
from packgen.parser import parse_header


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="packgen",
        description="Generate packing and unpacking routines for C structs",
    )
    parser.add_argument(
        "filename", metavar="filename", help="the header file containing the typedefs"
    )
    parser.add_argument(
        "--little",
        dest="endian",
        action="store_const",
        const="little",
        default="big",
        help="generate little endian pack/unpack routines (default big endian)",
    )
    parser.add_argument(
        "--big",
        dest="endian",
        action="store_const",
        const="big",
        help="generate big endian pack/unpack routines (the default)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="where to write the generated files (default: alongside the header)",
    )
    parser.add_argument(
        "--no-json",
        dest="json",
        action="store_false",
        help="skip the jansson-based JSON marshallers",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    header_path = Path(args.filename)
    out_dir = args.output_dir or header_path.parent
    base = header_path.stem

    try:
        schema = parse_header(header_path)
    except PackgenError as exc:
        print(f"packgen: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"packgen: {exc}", file=sys.stderr)
        return 1

    if not schema:
        print(f"packgen: no typedef'd structs found in {header_path}", file=sys.stderr)
        return 1

    unpack_header = f"{base}_unpack.h"
    json_header = f"{base}_json.h"

    outputs: dict[str, str] = {}
    try:
        unpack = pack_gen.generate(
            schema,
            source_header=header_path.name,
            generated_header=unpack_header,
            endian=args.endian,
        )
        outputs[unpack_header] = unpack.header
        outputs[f"{base}_unpack.c"] = unpack.source
    except PackgenError as exc:
        print(f"packgen: {exc}", file=sys.stderr)
        return 1

    if args.json:
        try:
            marshal = json_gen.generate(
                schema,
                source_header=header_path.name,
                generated_header=json_header,
            )
        except PackgenError as exc:
            # The pack routines generated fine; only the JSON ones did not.
            # Say so, because --no-json is then a real way forward.
            print(f"packgen: JSON marshalling: {exc}", file=sys.stderr)
            print(
                "packgen: the pack/unpack routines were fine; "
                "re-run with --no-json to generate just those",
                file=sys.stderr,
            )
            return 1
        outputs[json_header] = marshal.header
        outputs[f"{base}_json.c"] = marshal.source

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, content in outputs.items():
        (out_dir / name).write_text(content, encoding="utf-8")
        print(out_dir / name)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
