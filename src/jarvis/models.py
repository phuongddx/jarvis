"""Plain dataclasses for the nav-tool result shapes.

Trimmed from an internal reference implementation's pydantic `models.py` (that file also
carries FastAPI/registration/stats/blast-radius models jarvis doesn't
need). Dataclasses instead of pydantic here: MCP tool functions in
`server.py` return dicts built from these directly (`dataclasses.asdict`),
and jarvis has no HTTP layer that would benefit from pydantic validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Freshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Position:
    line: int
    character: int


@dataclass(frozen=True)
class Range:
    start: Position
    end: Position


@dataclass(frozen=True)
class Location:
    path: str
    range: Range
    # Additive (Task 5, spec TSI-05 "Additive result fields"): every
    # declaration/definition entry now carries its provider. Defaults keep
    # every pre-existing SCIP-only call site unchanged.
    source: str = "scip"
    positionEncoding: str | None = None
    carried_from_sha: str | None = None


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    displayName: str | None = None
    kind: str | None = None


@dataclass(frozen=True)
class DocumentSymbolEntry:
    symbol: str
    displayName: str | None
    kind: str | None
    range: Range
    # Additive (Task 5): `source`/`positionEncoding`/`qualifiedName`/
    # `parentSymbol` are always present; `selectionRange` is populated only
    # for syntax-served outline entries -- `range` keeps its existing SCIP
    # semantics (the enclosing/definition range) unchanged either way.
    source: str = "scip"
    selectionRange: Range | None = None
    positionEncoding: str | None = None
    qualifiedName: str | None = None
    parentSymbol: str | None = None


@dataclass(frozen=True)
class Coverage:
    """Per-file syntax coverage disclosed alongside a syntax-served
    response (spec TSI-05 "Additive result fields"). Never attached to a
    response served entirely by SCIP."""

    state: str  # complete | partial | unsupported | not-indexed
    reason: str | None
    parsed: int
    partial: int
    failed: int
    skipped: int
    unsupported: int


@dataclass(frozen=True)
class CallHierarchyEntry:
    symbol: SymbolInfo
    location: Location


@dataclass(frozen=True)
class TypeHierarchyEntry:
    symbol: SymbolInfo
    location: Location
