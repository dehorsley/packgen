from pycparser import c_parser, c_ast, c_generator, parse_file

class LengthGenerator(c_ast.NodeVisitor):
    def __init__(self, filename):
        super().__init__()
        self.filename = filename

    def visit_Typedef(self, node):
        if node.coord.file != self.filename:
            return

        print("")
        print(f"const size_t len_{node.name} = 0", end="")

        for c in node.type.type.decls:
            self.visit(c)


        print(f";")

    def visit_ArrayDecl(self, node):
        print("+", node.dim.value, "*(0")
        for c in node:
            self.visit(c)
        print(")")

    def visit_TypeDecl(self, node):
        type = node.type.names[-1]
        name = node.declname

        if type in {'uint8_t', 'int8_t', 'char'}:
            print("+1")

        elif type == 'uint16_t':
            print("+2")

        elif type == 'uint32_t':
            print("+4")

        elif type == 'uint64_t':
            print("+8")

        else:
            print(f"+len_{type}")

class UnmarshalGenerator(c_ast.NodeVisitor):
    endian = "little"

    def __init__(self, filename, endian=None):
        super().__init__()
        self.filename = filename
        self.array = False

        if endian:
            self.endian = endian

    def visit_Typedef(self, node):
        if node.coord.file != self.filename:
            return

        print("")
        print(f"size_t unmarshal_{node.name}({node.name} *t, uint8_t *data, size_t n)")
        print("{")
        print("ssize_t ret; uint8_t *p=data;")
        print(f"if (n < len_{node.name}) return -1;")

        for c in node.type.type.decls:
            self.visit(c)

        print("return (p-data);}")

    def visit_ArrayDecl(self, node):
        self.array = True
        print(f"for (int i=0; i < {node.dim.value}; i++)" "{")
        for c in node:
            self.visit(c)
        self.array = False
        print("}")

    def visit_TypeDecl(self, node):
        type = node.type.names[-1]
        name = node.declname
        if self.array:
            name += "[i]"

        if type in {'uint8_t', 'int8_t', 'char'}:
            self.gen_unmarshal_uint8(name)

        elif type == 'uint16_t':
            self.gen_unmarshal_uint16(name)

        elif type == 'uint32_t':
            self.gen_unmarshal_uint32(name)

        elif type == 'uint32_t':
            self.gen_unmarshal_uint32(name)

        else:
            print(f"ret = unmarshal_{type}(&t->{name}, p, n);")
            print("p += ret; n -= ret;")

    def gen_unmarshal_uint8(self,name):
        print(f"t->{name} = *p++; n--;")

    def gen_unmarshal_uint16(self,name):
        if ENDIAN=="little":
            print(f"t->{name} = (data[0]<<0) | (data[1]<<8); data+=2; n-=2;")
        else:
            print(f"t->{name} = (data[1]<<0) | (data[0]<<8); data+=2; n-=2;")

    def gen_unmarshal_uint32(self,name):
        if ENDIAN=="little":
            print(f"t->{name} = (data[0]<<0) | (data[1]<<8) | (data[2]<<16) | (data[3]<<24); data+=4; n-=4;")
        else:
            print(f"t->{name} = (data[3]<<0) | (data[2]<<8) | (data[1]<<16) | (data[0]<<24); data+=4; n-=4;")


FILE = "packet.h"
ENDIAN = "little"

ast = parse_file(FILE, use_cpp=True, cpp_path="gcc",
        cpp_args=['-E', r'-I/Users/dhorsley/src/pycparser/utils/fake_libc_include'])


print("#include <stdint.h>")
print("#include <sys/types.h>")
print("")
print(f'#include "{FILE}"')
print("")

l = LengthGenerator(filename=FILE)
l.visit(ast)

v = UnmarshalGenerator(filename=FILE,endian=ENDIAN)
v.visit(ast)
