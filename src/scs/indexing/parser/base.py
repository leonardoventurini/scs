"""Language-independent contracts emitted by SCS's native parsers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from scs.graph.models import NodeType, RelationshipType


def build_embed_text(entity: "ParsedEntity") -> str:
    """Build a stable semantic representation from structural code metadata."""

    doc = f": {entity.docstring[:512]}" if entity.docstring else ""
    if entity.kind is NodeType.FILE:
        return f"file: {entity.qualified_name}{doc}"
    if entity.kind is NodeType.CLASS:
        return f"class {entity.name}{doc}"
    if entity.kind in {NodeType.FUNCTION, NodeType.METHOD}:
        return f"{entity.kind.value} {entity.qualified_name} {entity.signature}{doc}"
    if entity.kind in {NodeType.VARIABLE, NodeType.CONSTANT}:
        return f"{entity.kind.value} {entity.qualified_name}: {entity.signature}"
    if entity.kind is NodeType.IMPORT:
        return f"import {entity.name}"
    if entity.kind is NodeType.TYPE_ALIAS:
        return f"type {entity.name}: {entity.raw_text[:256]}"
    return f"{entity.kind.value} {entity.name}"


@dataclass(frozen=True, slots=True)
class ParsedEntity:
    """One code entity extracted from a source file."""

    kind: NodeType
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    signature: str = ""
    docstring: str = ""
    raw_text: str = ""
    parent_qualified_name: str | None = None
    bases: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    cyclomatic_complexity: int | None = None

    def embed_text(self) -> str:
        """Return the document text consumed by the embedding provider."""

        return build_embed_text(self)


@dataclass(frozen=True, slots=True)
class ParsedEdge:
    """A relationship whose qualified names resolve during indexing."""

    source_qualified_name: str
    target_qualified_name: str
    relationship: RelationshipType
    weight: float = 1.0


@runtime_checkable
class LanguageParser(Protocol):
    """Parse source text into repository-independent entities and edges."""

    def parse(
        self, source: str, file_path: str
    ) -> tuple[list[ParsedEntity], list[ParsedEdge]]:
        """Parse one repository-relative source path."""

        ...

    def supported_extensions(self) -> frozenset[str]:
        """Return native parser extensions including the leading dot."""

        ...
