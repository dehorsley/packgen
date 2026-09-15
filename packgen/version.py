# This file covered by GPL 3 license
# C. David Horsley 2025
"""The installed version of packgen.

Kept in its own module so the writer can stamp it into generated code
without importing the package root, which would be a cycle.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("packgen")
except PackageNotFoundError:  # pragma: no cover - not installed, e.g. a tarball
    __version__ = "unknown"

__all__ = ["__version__"]
