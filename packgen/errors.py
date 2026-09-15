# This file covered by GPL 3 license
# C. David Horsley 2025
"""Exceptions raised by packgen."""

from __future__ import annotations


class PackgenError(Exception):
    """Base class for every error packgen raises."""


class ParseError(PackgenError):
    """The input could not be parsed as C."""


class UnsupportedTypeError(PackgenError):
    """A declaration uses a construct packgen deliberately does not support."""


class UnknownTypeError(PackgenError):
    """A field refers to a type that was not defined in the input."""
