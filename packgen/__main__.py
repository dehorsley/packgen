import argparse
from pathlib import Path

from pycparser import parse_file

from packgen import (
    JsonMarshalGenerator,
    UnmarshalGenerator,
)


def main():
    parser = argparse.ArgumentParser(
        description="Generate unpacking routines for C struct"
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
    args = parser.parse_args()

    ast = parse_file(
        args.filename,
        use_cpp=True,
        cpp_path="gcc",
        cpp_args=[
            "-E",
            "-I%s" % (Path(__file__).resolve().parent / "fake_libc_include"),
        ],
    )

    base = Path(args.filename).stem

    with open(base + "_unpack.h", "w+") as unpack_h, open(
        base + "_unpack.c", "w+"
    ) as unpack_c, open(base + "_json.h", "w+") as json_h, open(
        base + "_json.c", "w+"
    ) as json_c:

        v = UnmarshalGenerator(
            filename=args.filename,
            endian=args.endian,
            output=unpack_c,
            header=unpack_h,
        )
        v.visit(ast)

        v = JsonMarshalGenerator(filename=args.filename, output=json_c, header=json_h)
        v.visit(ast)


if __name__ == "__main__":
    main()
