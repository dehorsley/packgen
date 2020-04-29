from pycparser import c_parser, c_ast, c_generator, parse_file

class UnmarshalGenerator(c_ast.NodeVisitor):
    endian = "little"

    def __init__(self, filename, endian=None):
        super().__init__()
        self.filename = filename
        self.array = False

        if endian:
            self.endian = endian

        print("#include <stdint.h>")
        print("#include <sys/types.h>")
        print("")
        print(f'#include "{self.filename}"')
        print("")

    def visit_Typedef(self, node):
        if node.coord.file != self.filename:
            return

        print("")
        print(f"size_t unmarshal_{node.name}({node.name} *t, uint8_t *data, size_t n)")
        print("{")
        print("ssize_t ret; uint8_t *p=data;")

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
            print("if (ret < 0) return -1; p += ret; n -= ret;")

    def gen_unmarshal_uint8(self,name):
        print("if (n < 1) return -1;")
        print(f"t->{name} = p++; n--;")

    def gen_unmarshal_uint16(self,name):
        print("if (n < 2) return -1;")
        if ENDIAN=="little":
            print(f"t->{name} = (data[0]<<0) | (data[1]<<8); data+=2; n-=2;")
        else:
            print(f"t->{name} = (data[1]<<0) | (data[0]<<8); data+=2; n-=2;")

    def gen_unmarshal_uint32(self,name):
        print("if (n < 4) return -1;")
        if ENDIAN=="little":
            print(f"t->{name} = (data[0]<<0) | (data[1]<<8) | (data[2]<<16) | (data[3]<<24); data+=4; n-=4;")
        else:
            print(f"t->{name} = (data[3]<<0) | (data[2]<<8) | (data[1]<<16) | (data[0]<<24); data+=4; n-=4;")


    # def 
    #         print("")
    #         if d.type.__class__ == c_ast.ArrayDecl:
    #             # TODO: check if it's a char array
    #             gen_unmarshal_array(d)
    #             
    #         else:
    #             gen_unmarshal_type(d.name, d.type.type.names[0])

    


FILE = "packet.h"
ENDIAN = "little"

ast = parse_file(FILE, use_cpp=True, cpp_path="gcc",
        cpp_args=['-E', r'-I/Users/dhorsley/src/pycparser/utils/fake_libc_include'])
v = UnmarshalGenerator(filename=FILE,endian=ENDIAN)
v.visit(ast)
# ast.show()
