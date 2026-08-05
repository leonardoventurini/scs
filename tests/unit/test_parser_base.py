from __future__ import annotations

import pytest

from scs.graph.models import NodeType
from scs.indexing.parser.base import ParsedEntity


@pytest.mark.parametrize(
    ("entity", "expected"),
    [
        (
            ParsedEntity(NodeType.FILE, "main.py", "src.main", 0, 1),
            "file: src.main",
        ),
        (
            ParsedEntity(
                NodeType.CLASS,
                "Service",
                "src.Service",
                1,
                5,
                docstring="Owns requests.",
            ),
            "class Service: Owns requests.",
        ),
        (
            ParsedEntity(
                NodeType.FUNCTION,
                "run",
                "src.run",
                2,
                3,
                signature="(value: int) -> bool",
                docstring="Execute work.",
            ),
            "function src.run (value: int) -> bool: Execute work.",
        ),
        (
            ParsedEntity(
                NodeType.METHOD,
                "start",
                "src.Service.start",
                2,
                3,
                signature="()",
            ),
            "method src.Service.start ()",
        ),
        (
            ParsedEntity(
                NodeType.VARIABLE,
                "value",
                "src.value",
                1,
                1,
                signature="str",
            ),
            "variable src.value: str",
        ),
        (
            ParsedEntity(
                NodeType.CONSTANT,
                "LIMIT",
                "src.LIMIT",
                1,
                1,
                signature="int",
            ),
            "constant src.LIMIT: int",
        ),
        (
            ParsedEntity(NodeType.IMPORT, "pathlib", "src.pathlib", 1, 1),
            "import pathlib",
        ),
        (
            ParsedEntity(
                NodeType.TYPE_ALIAS,
                "Identifier",
                "src.Identifier",
                1,
                1,
                raw_text="Identifier = str | int",
            ),
            "type Identifier: Identifier = str | int",
        ),
        (
            ParsedEntity(NodeType.MODULE, "src", "src", 1, 1),
            "module src",
        ),
    ],
)
def test_embed_text_contract(entity: ParsedEntity, expected: str) -> None:
    assert entity.embed_text() == expected


def test_embed_text_bounds_provider_input_without_splitting_unicode() -> None:
    docstring = "á" * 600
    raw_text = "型" * 300

    function = ParsedEntity(
        NodeType.FUNCTION,
        "run",
        "src.run",
        1,
        1,
        docstring=docstring,
    )
    alias = ParsedEntity(
        NodeType.TYPE_ALIAS,
        "Payload",
        "src.Payload",
        1,
        1,
        raw_text=raw_text,
    )

    assert function.embed_text() == f"function src.run : {'á' * 512}"
    assert alias.embed_text() == f"type Payload: {'型' * 256}"
