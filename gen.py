from pycparser import c_parser, c_ast, c_generator, parse_file

class Visitor(c_ast.NodeVisitor):
    def __init__(self, filename):
        super().__init__()
        self.filename = filename
        print("#include <stdint.h>")
        print("#include <sys/types.h>")
        print("")
        print(f'#include "{FILE}"')
        print("")

    def visit_Typedef(self, node):
        if node.coord.file != FILE:
            return

        gen_unmarshal(node)

def gen_unmarshal(t):
    print("")
    print(f"size_t unmarshal_{t.name}({t.name} *t, uint8_t *data, size_t n)")
    print("{")
    print("ssize_t ret; uint8_t *p=data;")
    # print(dir(t.type.type.decls))
    for d in t.type.type.decls:
        print("")
        if d.type.__class__ == c_ast.ArrayDecl:
            # TODO: check if it's a char array
            gen_unmarshal_array(d)
            
        else:
            gen_unmarshal_type(d.name, d.type.type.names[0])
    print("return (p-data);}")

def gen_unmarshal_uint8(name):
    print("if (n < 1) return -1;")
    print(f"t->{name} = p++; n--;")

ENDIAN = "little"
def gen_unmarshal_uint16(name):
    print("if (n < 2) return -1;")
    if ENDIAN=="little":
        print(f"t->{name} = (data[0]<<0) | (data[1]<<8); data+=2; n-=2;")
    else:
        print(f"t->{name} = (data[1]<<0) | (data[0]<<8); data+=2; n-=2;")

def gen_unmarshal_uint32(name):
    print("if (n < 4) return -1;")
    if ENDIAN=="little":
        print(f"t->{name} = (data[0]<<0) | (data[1]<<8) | (data[2]<<16) | (data[3]<<24); data+=4; n-=4;")
    else:
        print(f"t->{name} = (data[3]<<0) | (data[2]<<8) | (data[1]<<16) | (data[0]<<24); data+=4; n-=4;")

def gen_unmarshal_type(name, type):
    if type in {'uint8_t', 'int8_t', 'char'}:
        gen_unmarshal_uint8(name)

    elif type == 'uint16_t':
        gen_unmarshal_uint16(name)
    
    elif type == 'uint32_t':
        gen_unmarshal_uint32(name)

    elif type == 'uint32_t':
        gen_unmarshal_uint32(name)

    else:
        print(f"ret = unmarshal_{type}(&t->{name}, p, n);")
        print("""if (ret < 0) return -1;
p += ret; n -= ret;""")

def gen_unmarshal_array(d):
        print(f"for (int i=0; i < {d.type.dim.value}; i++)" "{")
        gen_unmarshal_type(type=d.type.type.type.names[0], name=d.name + "[i]")
        print("}")
    


FILE = "packet.h"
ast = parse_file(FILE, use_cpp=True, cpp_path="gcc",
        cpp_args=['-E', r'-I/Users/dhorsley/src/pycparser/utils/fake_libc_include'])
v = Visitor(FILE)
v.visit(ast)
# ast.show()
