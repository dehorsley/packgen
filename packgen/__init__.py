# This file covered by GPL 3 license
# C. David Horsley 2025
"""Generate C struct packing and unpacking routines from a header file."""

from __future__ import annotations

from packgen.errors import (
    PackgenError,
    ParseError,
    UnknownTypeError,
    UnsupportedTypeError,
)
from packgen.generators import GeneratedPair, json, lengths, pack
from packgen.model import Field, Schema, Struct
from packgen.parser import parse_header, parse_source

__all__ = [
    "Field",
    "GeneratedPair",
    "PackgenError",
    "ParseError",
    "Schema",
    "Struct",
    "UnknownTypeError",
    "UnsupportedTypeError",
    "json",
    "lengths",
    "pack",
    "parse_header",
    "parse_source",
]
