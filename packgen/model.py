# This file covered by GPL 3 license
# C. David Horsley 2025
"""The intermediate representation packgen's generators work from.

The parser turns a header into a :class:`Schema`: an ordered collection of
:class:`Struct` definitions made of :class:`Field` values.  Everything after
parsing -- size calculation, code generation -- works from this and never
touches a parse tree.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from math import prod

from packgen.errors import UnknownTypeError, UnsupportedTypeError

#: Fixed-width integer types, mapped to their packed size in bytes.
INT_TYPES: dict[str, int] = {
    "char": 1,
    "int8_t": 1,
    "uint8_t": 1,
    "int16_t": 2,
    "uint16_t": 2,
    "int32_t": 4,
    "uint32_t": 4,
    "int64_t": 8,
    "uint64_t": 8,
}

#: Integer types that are written back sign-extended.
SIGNED_INT_TYPES = frozenset({"int8_t", "int16_t", "int32_t", "int64_t"})

#: Boolean types, packed as a single byte.
BOOL_TYPES = frozenset({"bool", "_Bool"})

#: IEEE-754 types, mapped to their packed size in bytes.
REAL_TYPES: dict[str, int] = {"float": 4, "double": 8}

#: Largest packed struct packgen will generate code for.
#:
#: The generated routines return ``ptrdiff_t``, which is only guaranteed to
#: hold 2**31 - 1 on a 32-bit target.  A header claiming more than this would
#: produce code that silently truncates there, so it is refused instead.
MAX_PACKED_SIZE = 2**31 - 1

#: Deepest chain of nested structs packgen will follow when sizing.  Real
#: packed headers nest a handful deep; a limit keeps a pathological one from
#: exhausting the Python stack.
MAX_NESTING = 64

#: Every type packgen can pack without consulting the schema.
PRIMITIVE_SIZES: dict[str, int] = {
    **INT_TYPES,
    **{name: 1 for name in BOOL_TYPES},
    **REAL_TYPES,
}


def unsigned_equivalent(type_: str) -> str:
    """The unsigned type a value of ``type_`` is shuffled through on the wire."""
    return f"uint{PRIMITIVE_SIZES[type_] * 8}_t"


@dataclass(frozen=True)
class Field:
    """A single member of a struct.

    ``dims`` holds the array extents outermost first, so ``uint8_t a[2][3]``
    is ``dims=(2, 3)``.  A scalar has no dims.  ``pointer`` is the pointer
    depth, which is only ever non-zero for types packgen cannot pack but can
    still describe (``char *`` in JSON output, for instance).
    """

    name: str
    type: str
    dims: tuple[int, ...] = ()
    pointer: int = 0

    @property
    def is_array(self) -> bool:
        return bool(self.dims)

    @property
    def is_pointer(self) -> bool:
        return self.pointer > 0

    @property
    def count(self) -> int:
        """Total number of elements, across every array dimension."""
        return prod(self.dims)


@dataclass(frozen=True)
class Struct:
    """A ``typedef``'d struct from the input header."""

    name: str
    fields: tuple[Field, ...] = ()


@dataclass
class Schema:
    """Every struct packgen found, in declaration order.

    Sizes are computed on demand and cached, so a header holding one struct
    packgen cannot pack is still usable for the structs it can.
    """

    structs: tuple[Struct, ...] = ()
    #: Typedefs that alias another type, e.g. ``typedef uint32_t word_t;``
    aliases: dict[str, str] = field(default_factory=dict)
    #: Typedef names packgen saw but cannot pack, mapped to the reason why.
    unsupported: dict[str, str] = field(default_factory=dict)
    #: Every macro name the source header defines.  Generated code is
    #: included *after* that header, so anything packgen emits at file
    #: scope that collides with one of these is a macro redefinition.
    macros: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        self._by_name = {s.name: s for s in self.structs}
        self._sizes: dict[str, int] = {}
        self._in_progress: set[str] = set()

    def __iter__(self) -> Iterator[Struct]:
        return iter(self.structs)

    def __len__(self) -> int:
        return len(self.structs)

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def __getitem__(self, name: str) -> Struct:
        try:
            return self._by_name[name]
        except KeyError:
            raise UnknownTypeError(f"no struct named {name!r} in this header") from None

    def resolve(self, type_: str) -> str:
        """Follow alias typedefs until a primitive or struct name is reached."""
        seen = {type_}
        while type_ in self.aliases:
            type_ = self.aliases[type_]
            if type_ in seen:
                raise UnsupportedTypeError(
                    f"typedef {type_!r} is defined in terms of itself"
                )
            seen.add(type_)
        return type_

    def is_struct(self, type_: str) -> bool:
        return self.resolve(type_) in self._by_name

    def is_primitive(self, type_: str) -> bool:
        return self.resolve(type_) in PRIMITIVE_SIZES

    def size_of_type(self, type_: str) -> int:
        """Packed size in bytes of a single value of ``type_``."""
        resolved = self.resolve(type_)
        if resolved in PRIMITIVE_SIZES:
            return PRIMITIVE_SIZES[resolved]
        if resolved in self._sizes:
            return self._sizes[resolved]
        if resolved in self._by_name:
            return self._compute_size(self._by_name[resolved])
        if resolved in self.unsupported:
            raise UnsupportedTypeError(
                f"cannot pack type {type_!r}: {self.unsupported[resolved]}"
            )
        raise UnknownTypeError(
            f"unknown type {type_!r}; packgen only sees types defined in the "
            f"header it is given, not ones pulled in by #include"
        )

    def size_of_field(self, field_: Field) -> int:
        if field_.is_pointer:
            raise UnsupportedTypeError(
                f"field {field_.name!r} is a pointer; packed structs cannot "
                f"contain pointers"
            )
        size = self.size_of_type(field_.type)
        return size * field_.count if field_.is_array else size

    def size_of(self, struct: Struct | str) -> int:
        """Packed size in bytes of a struct, by value or by name."""
        name = struct if isinstance(struct, str) else struct.name
        resolved = self.resolve(name)
        if resolved not in self._by_name:
            raise UnknownTypeError(f"no struct named {name!r} in this header")
        return self.size_of_type(resolved)

    def _compute_size(self, struct: Struct) -> int:
        if struct.name in self._in_progress:
            raise UnsupportedTypeError(
                f"struct {struct.name!r} contains itself, so it has no finite size"
            )
        if len(self._in_progress) >= MAX_NESTING:
            raise UnsupportedTypeError(
                f"struct {struct.name!r} is nested more than {MAX_NESTING} deep; "
                f"packgen will not generate code for a type graph this deep"
            )
        self._in_progress.add(struct.name)
        try:
            total = 0
            for field_ in struct.fields:
                try:
                    total += self.size_of_field(field_)
                except (UnknownTypeError, UnsupportedTypeError) as exc:
                    raise type(exc)(f"in struct {struct.name!r}: {exc}") from None
        finally:
            self._in_progress.discard(struct.name)
        if total > MAX_PACKED_SIZE:
            raise UnsupportedTypeError(
                f"struct {struct.name!r} packs to {total} bytes, over the "
                f"{MAX_PACKED_SIZE} byte limit; the generated routines return "
                f"ptrdiff_t, which cannot represent that on a 32-bit target"
            )
        self._sizes[struct.name] = total
        return total
