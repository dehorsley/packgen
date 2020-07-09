#!/usr/bin/env python3
# This file covered by GPL 3 license
# C. David Horsley 2020
import sys
import os.path
from pycparser import c_parser, c_ast, c_generator, parse_file


class LengthGenerator(c_ast.NodeVisitor):
    def __init__(self, filename, output=None):
        super().__init__()
        self.filename = filename
        if output is None:
            self.output = sys.stdout
        else:
            self.output = output

        inside_typedef = False
        self.constants = {}
        self.stack = []

    def push(self, value):
        self.stack.append(value)

    def pop(self):
        val = self.stack.pop()
        return val

    def print(self, *args, **kwargs):
        print(*args, file=self.output, **kwargs)

    def add(self):
        v1 = self.pop()
        v2 = self.pop()
        self.push(v1 + v2)

    def mul(self):
        v1 = self.pop()
        v2 = self.pop()
        self.push(v1 * v2)

    def visit_Typedef(self, node):
        if node.coord.file != self.filename:
            return

        self.print(f"const size_t len_{node.name} = ", end="")

        self.push(0)
        self.inside_typedef = True
        for c in node.type.type.decls:
            self.visit(c)

        value = self.pop()
        self.inside_typedef = False

        self.print(f"{value};")
        self.constants[f"len_{node.name}"] = value

    def visit_ArrayDecl(self, node):
        if not self.inside_typedef:
            return
        self.push(int(node.dim.value))
        self.push(0)
        for c in node:
            self.visit(c)
        self.mul()
        self.add()

    def visit_TypeDecl(self, node):
        if not self.inside_typedef:
            return
        type = node.type.names[-1]
        name = node.declname

        if type in {"uint8_t", "int8_t", "char", "bool"}:
            self.push(1)

        elif type == "uint16_t":
            self.push(2)

        elif type in {"uint32_t", "float"}:
            self.push(4)

        elif type in {"uint64_t", "double"}:
            self.push(8)

        else:
            self.push(self.constants[f"len_{type}"])

        self.add()


class UnmarshalGenerator(c_ast.NodeVisitor):
    endian = "little"

    def __init__(self, filename, endian=None, output=None, header=None):
        super().__init__()
        self.filename = filename
        self.array = False
        self.inside_typedef = False

        if endian:
            self.endian = endian

        if output is None:
            self.output = sys.stdout
        else:
            self.output = output

        if header is None:
            self.header = sys.stdout
        else:
            self.header = header

        self.print("#include <stdint.h>")
        self.print("#include <sys/types.h>")
        self.print("")
        self.print(f'#include "{filename}"')

        l = LengthGenerator(filename=args.filename, output=self.output)
        l.visit(ast)

    def printheader(self, *args, **kwargs):
        print(*args, file=self.header, **kwargs)

    def print(self, *args, **kwargs):
        print(*args, file=self.output, **kwargs)

    def visit_Typedef(self, node):
        if node.coord.file != self.filename:
            return

        self.print("")
        self.printheader(
            f"ssize_t unmarshal_{node.name}({node.name} *t, uint8_t *data, size_t n);"
        )
        self.print(
            f"ssize_t unmarshal_{node.name}({node.name} *t, uint8_t *data, size_t n)"
        )
        self.print("{")
        self.print("ssize_t ret; uint8_t *p=data;")
        self.print(f"if (n < len_{node.name}) return -1;")

        self.inside_typedef = True
        for c in node.type.type.decls:
            self.visit(c)
        self.inside_typedef = False

        self.print("return (p-data);}")

    def visit_ArrayDecl(self, node):
        if not self.inside_typedef:
            return
        self.print(f"for (int i=0; i < {node.dim.value}; i++)" "{")
        self.array = True
        self.generic_visit(node)
        self.array = False
        self.print("}")

    def visit_TypeDecl(self, node):
        if not self.inside_typedef:
            return
        type = node.type.names[-1]
        name = node.declname
        if self.array:
            name += "[i]"

        if type in {"uint8_t", "int8_t", "char"}:
            self.gen_unmarshal_uint8(name)

        elif type == "uint16_t":
            self.gen_unmarshal_uint16(name)

        elif type == "uint32_t":
            self.gen_unmarshal_uint32(name)

        elif type == "uint32_t":
            self.gen_unmarshal_uint32(name)

        else:
            self.print(f"ret = unmarshal_{type}(&t->{name}, p, n);")
            self.print("p += ret; n -= ret;")

    def gen_unmarshal_uint8(self, name):
        self.print(f"t->{name} = *p++; n--;")

    def gen_unmarshal_uint16(self, name):
        if self.endian == "little":
            self.print(f"t->{name} = (p[0]<<0) | (p[1]<<8); p+=2; n-=2;")
        else:
            self.print(f"t->{name} = (p[1]<<0) | (p[0]<<8); p+=2; n-=2;")

    def gen_unmarshal_uint32(self, name):
        if self.endian == "little":
            self.print(
                f"t->{name} = (p[0]<<0) | (p[1]<<8) | (p[2]<<16) | (p[3]<<24); p+=4; n-=4;"
            )
        else:
            self.print(
                f"t->{name} = (p[3]<<0) | (p[2]<<8) | (p[1]<<16) | (p[0]<<24); p+=4; n-=4;"
            )


INT_TYPES = {
    "uint8_t",
    "int8_t",
    "char",
    "uint16_t",
    "int16_t",
    "uint32_t",
    "int32_t",
    "uint64_t",
    "int64_t",
}

BOOL_TYPES = {"bool"}

REAL_TYPES = {"float", "double"}


class JsonMarshalGenerator(c_ast.NodeVisitor):
    def __init__(self, filename, output=None, header=None):
        super().__init__()
        self.filename = filename
        self.array = False
        self.inside_typedef = False
        self.pointerlevel = 0

        if output is None:
            self.output = sys.stdout
        else:
            self.output = output

        if header is None:
            self.header = sys.stdout
        else:
            self.header = header

        self.print("#include <jansson.h>")
        self.print("")
        self.print(f'#include "{filename}"')

    def printheader(self, *args, **kwargs):
        print(*args, file=self.header, **kwargs)

    def print(self, *args, **kwargs):
        print(*args, file=self.output, **kwargs)

    def visit_PtrDecl(self, node):
        self.pointerlevel += 1
        self.generic_visit(node)
        self.pointerlevel -= 1

    def visit_Typedef(self, node):
        if node.coord.file != self.filename:
            return

        self.print("")
        self.printheader(f"json_t *marshal_json_{node.name}(const {node.name} *t);")
        self.print(f"json_t *marshal_json_{node.name}(const {node.name} *t)")
        self.print("{")
        self.print("json_t *root = json_object(); json_t *a; json_t *ret;")
        self.print("if (root == NULL) return NULL;")

        self.inside_typedef = True
        for c in node.type.type.decls:
            self.visit(c)
        self.inside_typedef = False

        self.print("return root;}")

    def visit_ArrayDecl(self, node):
        if not self.inside_typedef:
            return
        # handle strings as special case.
        if node.type.type.names[-1] == "char":
            self.print(
                f"ret = json_stringn(t->{node.type.declname}, {node.dim.value});"
            )
            self.print("if (ret == NULL) return NULL;")
            self.print(f'json_object_set_new(root, "{node.type.declname}", ret);')
            return

        self.array = True
        self.print("")
        self.print("a = json_array();")
        self.print("if (a == NULL) return NULL;")
        self.print(f"for (int i=0; i < {node.dim.value}; i++)" "{")
        for c in node:
            self.visit(c)
        self.array = False
        self.print("}")
        self.print(f'json_object_set_new(root, "{node.type.declname}", a);')
        self.print("")

    def visit_TypeDecl(self, node):
        if not self.inside_typedef:
            return
        type_ = node.type.names[-1]
        name = node.declname

        if self.array:
            name += "[i]"

        if type_ == "char" and self.pointerlevel == 1:
            self.print(f"ret = json_string(t->{name});")
        elif type_ == "char" and self.pointerlevel > 1:
            raise TypeError("can't handle string arrays")
        elif type_ in INT_TYPES:
            self.print(f"ret = json_integer(t->{name});")
        elif type_ in BOOL_TYPES:
            self.print(f"ret = json_boolean(t->{name});")
        elif type_ in REAL_TYPES:
            self.print(f"ret = json_real(t->{name});")
        else:
            self.print(f"ret = marshal_json_{type_}(&t->{name});")

        self.print("if (ret == NULL) return NULL;")

        if self.array:
            self.print("json_array_append_new(a, ret);")
        else:
            self.print(f'json_object_set_new(root, "{name}", ret);')


if __name__ == "__main__":
    import sys
    import argparse
    from pathlib import Path

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

    base = os.path.splitext(args.filename)[0]

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
