# `packgen`: generate C struct packing and unpacking

One small step above casting a buffer to a struct.

This is a tool takes a file containing struct type definitions and generates
routines to pack and unpack the structures.

Assumptions made by the generator:

- structs you want to unpack are `typedef`'d
- contain no anonymous embedded structs (this assumption should be easily removed it needed)
- C99 fixed width types are used (you really should use these everywhere that might be exposed)
- no variable length data

Note, if you are designing a communication protocol, don't use this.

This includes pycparser's fake libc headers for convenience.
