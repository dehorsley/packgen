# `packgen`: generate C struct packing and unpacking

One small step above casting a buffer to a struct.

This is a tool takes a file containing struct type definitions and generates
routines to pack and unpack the structures.

Assumptions made by the generator:

- structs you want to unpack are `typedef`'d
- contain no anonymous embedded structs (this assumption should be easily removed if needed)
- C99 fixed width types are used (you really should use these everywhere that might be exposed)
- no variable length data

If you are designing a communication protocol, don't use this. Use something
like protobuf, msgpack, or json


This includes pycparser's fake libc headers for convenience. Those are covered under their own license.

The main program licensed under GPL 3. 
