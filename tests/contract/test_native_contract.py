"""Cross-language contracts for the private SCS native extension."""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

from scs.graph.models import NodeType, RelationshipType

_RUST_FFI_PATH = (
    Path(__file__).resolve().parents[2]
    / "crates"
    / "scs-python"
    / "src"
    / "python.rs"
)


def _find_matching_brace(source: str, open_index: int) -> int:
    depth = 0
    in_string = False
    in_line_comment = False
    escaped = False

    for index in range(open_index, len(source)):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""

        if in_line_comment:
            if char == "\n":
                in_line_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == "/" and next_char == "/":
            in_line_comment = True
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index

    raise AssertionError("unclosed Rust function body")


def _rust_functions(source: str) -> dict[str, str]:
    functions: dict[str, str] = {}
    for match in re.finditer(r"(?m)^\s*fn\s+([A-Za-z0-9_]+)\s*\(", source):
        open_index = source.find("{", match.end())
        if open_index == -1:
            continue
        close_index = _find_matching_brace(source, open_index)
        functions[match.group(1)] = source[open_index : close_index + 1]
    return functions


def test_native_module_is_named_scs() -> None:
    """The extension imports only through the private SCS runtime name."""

    native = importlib.import_module("scs._scs_native")
    assert native.__name__ == "scs._scs_native"


def test_python_rust_enum_parity() -> None:
    """Rust and Python reject the same non-code graph vocabulary."""

    native = importlib.import_module("scs._scs_native")
    assert native.node_type_values() == [value.value for value in NodeType]
    assert native.relationship_type_values() == [value.value for value in RelationshipType]


def test_native_rejects_non_code_graph_discriminators(tmp_path: Path) -> None:
    """The public native boundary cannot persist retired graph domains."""

    native = importlib.import_module("scs._scs_native")
    graph = native.KnowledgeGraph(str(tmp_path / "index.db"), 4)

    try:
        graph.upsert_node("retired", "correction", "retired")
    except ValueError as error:
        assert "invalid node type" in str(error)
    else:
        raise AssertionError("non-code node type was accepted")

    graph.upsert_node("source", "function", "source")
    graph.upsert_node("target", "function", "target")
    try:
        graph.upsert_edge("source", "target", "related_to")
    except ValueError as error:
        assert "invalid relationship type" in str(error)
    else:
        raise AssertionError("non-code relationship type was accepted")


def test_native_dimension_error_preserves_prior_node_and_vector(tmp_path: Path) -> None:
    """Cross-language errors cannot partially overwrite durable graph state."""

    native = importlib.import_module("scs._scs_native")
    database = tmp_path / "index.db"
    graph = native.KnowledgeGraph(str(database), 4)
    graph.upsert_node(
        "stable",
        "function",
        "original",
        "def original(): pass",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )
    graph.flush_vector_index()

    with pytest.raises(
        RuntimeError, match="embedding dimension mismatch: expected 4, got 3"
    ):
        graph.upsert_node(
            "stable",
            "function",
            "replacement",
            "def replacement(): pass",
            embedding=[0.0, 1.0, 0.0],
        )

    node = json.loads(graph.get_node("stable"))
    assert node["name"] == "original"
    assert node["content"] == "def original(): pass"
    assert graph.count_embeddings() == 1
    results = json.loads(graph.search_by_vector([1.0, 0.0, 0.0, 0.0]))
    assert results[0]["node"]["id"] == "stable"
    assert results[0]["distance"] < 1e-6
    del graph

    reopened = native.KnowledgeGraph(str(database), 4)
    assert json.loads(reopened.get_node("stable"))["name"] == "original"
    assert reopened.count_embeddings() == 1
    reopened_results = json.loads(
        reopened.search_by_vector([1.0, 0.0, 0.0, 0.0])
    )
    assert reopened_results[0]["node"]["id"] == "stable"
    assert reopened_results[0]["distance"] < 1e-6


def test_rust_ffi_storage_and_parser_calls_release_the_gil() -> None:
    """Every potentially growing native operation yields the Python GIL."""

    source = _RUST_FFI_PATH.read_text()
    violations: list[str] = []
    for name, body in _rust_functions(source).items():
        calls_native_work = any(
            marker in body
            for marker in ("self.inner.", "scs_store::", "RustKnowledgeGraph::", "parser.parse(")
        )
        if calls_native_work and "allow_threads" not in body:
            violations.append(name)

    assert violations == []
