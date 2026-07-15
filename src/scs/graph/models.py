"""Code-only graph models shared by indexing and native persistence."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class NodeType(StrEnum):
    """Allowed code and source-control node discriminators."""

    FILE = "file"
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"
    CONSTANT = "constant"
    IMPORT = "import"
    TYPE_ALIAS = "type_alias"
    COMMIT = "commit"
    CONTRIBUTOR = "contributor"


class RelationshipType(StrEnum):
    """Allowed structural and source-control relationships."""

    CONTAINS = "contains"
    CALLS = "calls"
    IMPORTS = "imports"
    INHERITS = "inherits"
    IMPLEMENTS = "implements"
    REFERENCES = "references"
    AUTHORED_BY = "authored_by"
    MODIFIES = "modifies"


class Node(BaseModel):
    """A repository-derived graph node."""

    id: str
    type: NodeType
    name: str
    content: str = ""
    metadata: dict[str, object] = Field(default_factory=dict)
    repo_id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Edge(BaseModel):
    """A directed relationship between repository-derived nodes."""

    id: str
    source_id: str
    target_id: str
    relationship: RelationshipType
    weight: float = 1.0
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: datetime | None = None


class SearchResult(BaseModel):
    """A semantic vector match where lower distance is more similar."""

    node: Node
    distance: float
