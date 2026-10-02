# Design notes on the generated code

Why the generated C looks the way it does. None of this is needed to use
packgen.

## Shape of the routines

Each `unmarshal_X` and `marshal_X` is a thin checked wrapper around a
`static` core that threads a pointer and takes no length. The wrapper has
already proved the buffer holds `len_X` bytes, and `len_X` counts every
nested field, so the core cannot run off the end and a nested struct needs
no second bounds check. Bounds and `NULL` checks happen once, at the
public entry point.

The cores are named `unmarshal_X_core` and `marshal_X_core`. They cannot
simply be called `unmarshal_X`, because that name belongs to the public
entry point in the same translation unit and C has no overloading.
Helpers that could clash with a standard or POSIX name keep a `packgen_`
prefix (`packgen_strnlen` exists because glibc declares `strnlen` in
`<string.h>`).

## Decoding

The byte-at-a-time shift-and-or decode is deliberate. It is endian-agnostic
standard C, and both GCC and Clang recognise the pattern and emit a single
wide load plus a byte swap.

Every signed or floating-point value is decoded the same way: its bytes
are assembled into the unsigned integer of the same width, and that is
`memcpy`'d into the field. The decode has no implementation-defined
behaviour.

- **Signed integers.** A cast back from unsigned, such as
  `(int32_t)(uint32_t)x`, is implementation-defined when `x` exceeds
  `INT32_MAX` (C99 6.3.1.3p3). Every compiler wraps it modulo 2ᴺ, but the
  standard does not require that. Copying the bits is fully defined,
  because C99 7.18.1.1 requires `intN_t` to be two's complement with no
  padding bits. On a machine where that is impossible, `intN_t` does not
  exist and the code fails to compile, instead of decoding wrongly. A
  signed byte (`int8_t`, or `char` where it is signed) is copied for the
  same reason.
- **`float` and `double`.** Copying is the only portable way to
  reinterpret bits: not a union, and not a pointer cast. The generated
  code asserts that they are 4 and 8 bytes.

The `memcpy` costs nothing in an optimised build. On the DBBC3 header,
with an added struct holding every signed type, the object code at `-O2`
was byte-for-byte identical to the cast version under both Clang and
GCC 16. At `-O0` it costs about four instructions per field.

Encoding needs none of this. The conversion from signed to unsigned is
defined as reduction modulo 2ᴺ (C99 6.3.1.3p2).

## Portability choices

- The return type is `ptrdiff_t` from `<stddef.h>`, not the more idiomatic
  `ssize_t`. That one is POSIX and does not exist on MSVC.
- `_Static_assert` is used where the compiler has it, with a C89-style
  `typedef char x[cond ? 1 : -1]` fallback, so the assertions survive on
  older toolchains.
- The JSON NUL scan is spelled out, not a call to `strnlen`, which glibc
  hides under `-std=c11`.

## Inlining

A nested struct is decoded by calling the shared `static` core for its
type, and whether that call survives is left to the compiler. That was
measured, not assumed. packgen's output against three alternatives, on the
DBBC3 packet code, clang 21 at `-O2` on arm64, best of seven runs:

| variant | calls left | `__TEXT` bytes | 6208 B packet | 92 B nested struct |
| --- | --- | --- | --- | --- |
| shared cores (what packgen emits) | 22 | 5408 | 482 ns | 5.0–6.3 ns |
| `static inline` on the cores | 18 | 5932 | **518 ns** | 5.1–5.5 ns |
| `always_inline` on the cores | 0 | 6228 | 482 ns | 4.5–4.6 ns |
| nesting emitted inline in the source | 0 | 6256 | 481 ns | 4.5–4.9 ns |

- **The compiler already inlines what matters.** For a small nested
  struct it needs no encouragement: a header of 1000 one-byte nested
  structs compiled to byte-identical code in every variant.
- **Annotating `inline` makes things worse.** It bought partial inlining,
  which was bigger *and* 7.6% slower on the real packet, on every run. So
  packgen does not emit it.
- **Forcing the calls open is not worth it.** On a 6 KB packet it changes
  nothing, for 15% more code. The only gain is about 0.5 ns per message
  on a mid-size struct with several nested structs, in a tight
  cache-resident loop, which is invisible next to the socket read that
  delivers a real packet.

packgen used to have a `--flatten` flag that emitted the nesting inline.
It was removed: it made no difference on the header this tool exists for,
and on a deeply branching type graph it multiplied the generated code 16×.
