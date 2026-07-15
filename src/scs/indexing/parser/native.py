"""Lazy import boundary for the standalone SCS tree-sitter extension."""

from __future__ import annotations

import importlib
import json
from typing import Protocol, cast

from scs.graph.models import NodeType, RelationshipType
from scs.indexing.parser.base import ParsedEdge, ParsedEntity


class _NativeParserModule(Protocol):
    def parse_file(self, source: str, file_path: str) -> str | None: ...
    def parse_file_supported_extensions(self) -> list[str]: ...


class NativeParser:
    """Delegate syntax extraction to ``_scs_native`` without product coupling."""

    def __init__(self, module: _NativeParserModule | None = None) -> None:
        self._module = module

    def _native(self) -> _NativeParserModule:
        if self._module is None:
            self._module = cast(_NativeParserModule, importlib.import_module("_scs_native"))
        return self._module

    def supported_extensions(self) -> frozenset[str]:
        """Return the native parser registry as the source of truth."""

        return frozenset(self._native().parse_file_supported_extensions())

    def parse(self, source: str, file_path: str) -> tuple[list[ParsedEntity], list[ParsedEdge]]:
        """Deserialize one native parser result into strict code-only types."""

        result = self._native().parse_file(source, file_path)
        if result is None:
            return [], []
        payload = json.loads(result)
        entities = [
            ParsedEntity(
                kind=NodeType(item["kind"]),
                name=item["name"],
                qualified_name=item["qualified_name"],
                start_line=item["start_line"],
                end_line=item["end_line"],
                signature=item.get("signature", ""),
                docstring=item.get("docstring", ""),
                raw_text=item.get("raw_text", ""),
                parent_qualified_name=item.get("parent_qualified_name"),
                bases=item.get("bases", []),
                imports=item.get("imports", []),
                calls=item.get("calls", []),
                cyclomatic_complexity=item.get("cyclomatic_complexity"),
            )
            for item in payload["entities"]
        ]
        edges = [
            ParsedEdge(
                source_qualified_name=item["source_qualified_name"],
                target_qualified_name=item["target_qualified_name"],
                relationship=RelationshipType(item["relationship"]),
                weight=float(item.get("weight", 1.0)),
            )
            for item in payload["edges"]
        ]
        return entities, edges
