"""Procedural anonymized examples of source identity collisions.

Provenance: synthetic minimal examples based on collision categories observed
while indexing an application repository. No repository source is copied; all
symbols, paths, selectors, values, and module names are invented here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CollisionCategory = Literal["type_value_imports", "selector_properties", "cross_kind"]


@dataclass(frozen=True, slots=True)
class OccurrenceFixture:
    path: str
    source: str
    shared_name: str
    occurrences: int


def repeated_source(extension: str, count: int) -> str:
    """Generate a removable suffix of same-kind occurrences for edit tests."""

    if extension == "ts":
        return (
            "\n".join(
                f'import {{ value{index} }} from "shared";' for index in range(count)
            )
            + "\n"
        )
    declarations = "\n".join(f"--tone: rgb({index},0,0);" for index in range(count))
    return f":root {{\n{declarations}\n}}\n"


def collision_fixture(
    category: CollisionCategory,
    *,
    same_line: bool,
    count: int = 4,
) -> OccurrenceFixture:
    """Generate representative collisions without identifying source content."""

    separator = " " if same_line else "\n"
    if category == "type_value_imports":
        statements = [
            f"import {'type ' if index % 2 == 0 else ''}"
            f'{{ Symbol{index} }} from "./synthetic-module";'
            for index in range(count)
        ]
        return OccurrenceFixture(
            "example.ts",
            separator.join(statements),
            "example.import../synthetic-module",
            count,
        )
    if category == "selector_properties":
        statements = [
            f".synthetic-{index} {{ --synthetic-tone: rgb({index},0,0); }}"
            for index in range(count)
        ]
        return OccurrenceFixture(
            "example.css",
            separator.join(statements),
            "example.--synthetic-tone",
            count,
        )
    # TypeScript's separate type/value namespaces allow these declarations.
    statements = ["type SyntheticSymbol = string;", "const SyntheticSymbol = 1;"]
    return OccurrenceFixture(
        "example.ts",
        separator.join(statements),
        "example.SyntheticSymbol",
        2,
    )
