# How packgen's output is verified

Generated code is only as trustworthy as the evidence behind it. This
document covers what is tested, what is proved, and what is not covered.

```
uv run pytest
```

runs everything below except the CBMC proofs, which have their own tools.
C tests are skipped if there is no working C compiler; JSON tests are
skipped if jansson is not installed.

## Compiling under strict flags

The end-to-end tests compile the generated code with

```
-std=c99 -pedantic -Wall -Wextra -Wconversion -Wsign-conversion -Wshadow -Werror
```

and again for c11 and c17. `-pedantic` is what holds the output to ISO C:
without it a compiler quietly accepts its own extensions.

## Differential testing against a reference

Each generated binary's wire format is checked against an independent
implementation in Python (`tests/reference.py`). A field emitted in the
wrong order or the wrong byte order shows up as a mismatch, instead of
cancelling itself out in a round trip.

`tests/test_fuzz.py` does the same on randomly generated headers, across
both byte orders. A failure prints the seed, which reproduces the header
exactly. The committed suite runs a few seeds to stay quick;
`tools/fuzz_sweep.py --cases 600` runs a wider sweep, and currently passes
clean.

## Sanitisers and coverage

Everything runs under ASan and UBSan, with the checks the default
`undefined` group leaves out (`integer`, `implicit-conversion`,
`local-bounds`, `nullability`) and `-fno-sanitize-recover=all`, so the
first violation is fatal instead of a logged warning.

A sanitiser only checks code that actually executes, so one test drives
*every* struct's `unmarshal` and `marshal` through success, both `NULL`
branches and the short-buffer branch, at byte patterns sitting on the
representation boundaries. `tools/check_c_coverage.py` confirms that
reaches 100% region, function, line and branch coverage of the generated
`.c` for both byte orders.

A further test compiles deliberate undefined behaviour and asserts it *is*
caught, so the suite cannot go quietly inert if the sanitiser flags ever
stop reaching the compiler.

## Checking the parse against the compiler

Everything above validates the *emitter*, and none of it can see a parser
bug. `tests/reference.py` looks independent, but it walks the same
`Schema` the parser produced. Reverse every struct's field order in the
parser and the generated C packs fields backwards, the reference expects
them backwards, and they agree: every differential case and every CBMC
proof still passes, because the round trip is still exact.

`tests/layout_oracle.py` closes that gap by asking the only authority on
what a C header means. A probe program compiled against the *original*
header reports `offsetof` and `sizeof` for every field packgen claims, and
the result is checked against the schema:

- offsets must increase in packgen's field order, and fields must not
  overlap (catches reordering)
- each field's byte span must equal packgen's element size times its
  element count (catches a wrong array bound or a misresolved type)
- every gap must be smaller than the struct's alignment, so it is padding
  and not a field packgen failed to see
- the in-memory struct must be at least the packed size

A nested struct's element size comes from the compiler, not from
packgen's own arithmetic, so a parser bug cannot cancel itself out.

Four tests deliberately corrupt the schema (reordering fields, widening an
array, narrowing a type, dropping a field) and assert the compiler
contradicts each one, so the check cannot quietly stop working.

## Proving the pack and unpack routines

`tools/cbmc_verify.py` uses [CBMC][] to prove, for *every* input, three
properties of each struct's generated code:

| property | claim |
| --- | --- |
| `roundtrip` | `marshal(unmarshal(b)) == b` for every byte sequence `b`. For a struct holding a `bool`, which normalises on the first pass, it proves idempotence instead: `f(f(b)) == f(b)` where `f = marshal ∘ unmarshal`. |
| `no-leak` | all `len_X` output bytes are written, so none of the caller's memory can leak onto the wire |
| `contract` | for **every** `n`: `n >= len_X` returns `len_X` and touches nothing past it; `n < len_X` returns `-1` and writes nothing at all, to the buffer or to the struct |

It also checks array bounds, pointer validity, and signed and unsigned
arithmetic overflow over the same inputs.

```
python tools/cbmc_verify.py tests/headers/dbbcpacket.h
python tools/cbmc_verify.py --random 20 --endian little
```

`no-leak` is about leaking information, not memory. It marshals the same
struct into two buffers pre-filled with different bytes and requires the
results to be equal. Any byte marshal failed to write would keep its fill
and the two would differ. Leaking struct padding or stale buffer contents
onto the wire is a well-worn CVE class elsewhere.

`contract` is the only one that catches a write occurring *before* the
length check. It is also the one that does not scale: it copies and
compares the whole struct twice over a symbolic length, and the 6208 byte
DBBC3 packet does not finish in 400 seconds. The entry-point guard is
structurally identical for every struct, so proving it for the small and
medium ones covers the pattern. Larger structs are reported as skipped,
not silently dropped, and `--max-contract-bytes` moves the line. On that
basis the whole DBBC3 header proves in about 80 seconds.

Bounded model checking is normally incomplete, because a bug past the
unwind limit is invisible. packgen escapes that: every loop it emits has
a constant trip count fixed by the struct layout, never by the data, so
`--unwinding-assertions` passes and the result is a proof, not an
approximation.

Two things these proofs deliberately do not do:

- `--conversion-check` is off. It asks whether a conversion preserves its
  value, and packing is deliberately lossy: `(uint8_t)(x >> 8)` is a byte
  extraction, well defined by C99 6.3.1.3p2 but not value preserving.
- A round trip is blind to a *symmetric* error. Byte-swapping a field in
  both directions leaves it self-consistent, and CBMC reports success.
  The differential test against `tests/reference.py` catches exactly
  that, which is why both exist: the proof covers all inputs for one
  implementation, and the reference covers sampled inputs against an
  independent one.

## Proving the JSON error paths

Every `marshal_json_X` has a `NULL`-return path for every allocation it
makes, and no ordinary test reaches them, because jansson does not fail on
demand. `tools/cbmc_json.py` replaces jansson with a model whose
allocators fail *nondeterministically*, so CBMC explores every combination
of which allocations succeed and which fail, and tracks ownership well
enough to tell a leak from a clean unwind:

- nothing allocated is still live when `marshal_json_X` returns `NULL`
- nothing is live after the caller's `json_decref` on success
- `json_decref` is never handed an already-freed object

The model reflects two parts of jansson's contract: the `*_set_new`
functions steal the reference even when they fail, which is why the
generated error paths only drop `root`; and ownership nests, so freeing
is transitive.

Injecting each classic mistake confirms the model is not vacuous: a
forgotten `json_decref(root)`, a doubled one, and a `json_decref` of a
reference jansson already stole are all caught.

The input bytes are unconstrained, so char fields reach the UTF-8 repair
path, and the bounds and pointer checks cover it.

This proof is expensive, because failure interleavings multiply. Budget
roughly a minute per struct and keep the header small:

```
python tools/cbmc_json.py --random 2
```

## What is not covered

- **Big-endian and mixed-endian hosts.** The decode is byte-wise and so
  should be host-agnostic, and `float`/`double` move through `memcpy` of
  an integer of the same width, which is correct on any host that stores
  integers and reals in the same byte order. That is every mainstream
  platform, but it is reasoning, not a test result: all testing so far is
  on little-endian AArch64.
- **MSVC.** CI builds with GCC and Clang on Linux and Clang on macOS.
  Nothing has been built with MSVC, though the output avoids POSIX for
  its sake.
- **Leak-freedom of the UTF-8 repair buffer.** The JSON proof tracks
  jansson objects, not plain `malloc`. The buffer is freed on the one path
  that allocates it, but a run with `--memory-leak-check` was too slow to
  finish.

[CBMC]: https://www.cprover.org/cbmc/
