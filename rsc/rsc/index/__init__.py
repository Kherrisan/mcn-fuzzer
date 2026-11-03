"""RSC M2 deterministic structural index."""

from __future__ import annotations

from .build import StructuralIndexError, build_structural_index, index_summary
from .query import RSCIndex
from .validate import validate_index

__all__ = [
    "RSCIndex",
    "StructuralIndexError",
    "build_structural_index",
    "index_summary",
    "validate_index",
]
