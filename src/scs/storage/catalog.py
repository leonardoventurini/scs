"""SQLite-backed, root-to-store catalog with side-effect-free lookups."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scs.paths import validate_scs_home
from scs.storage.models import (
    StoreGeneration,
    StoreId,
    StoreState,
    canonical_repository_root,
    store_id_for_root,
    validate_store_generation,
    validate_store_id,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_stores (
    project_id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_root TEXT NOT NULL UNIQUE,
    store_id TEXT NOT NULL UNIQUE,
    active_generation TEXT,
    state TEXT NOT NULL,
    CHECK (state IN (
        'uninitialized',
        'ready_structural',
        'semantic_stale',
        'semantic_ready',
        'migrating',
        'migration_failed_recoverable'
    ))
)
"""


class CatalogError(RuntimeError):
    """Raised when catalog persistence violates its root/store invariant."""


@dataclass(frozen=True, slots=True)
class CatalogRecord:
    """The durable catalog entry for exactly one canonical repository root."""

    project_id: int
    canonical_root: str
    store_id: StoreId
    active_generation: StoreGeneration | None
    state: StoreState


class ProjectStoreCatalog:
    """Resolve and explicitly register isolated stores under one SCS home.

    ``lookup`` intentionally opens SQLite read-only and returns ``None`` when
    the catalog does not yet exist. It never creates a catalog, project
    directory, or generation directory.
    """

    _home: Path
    _database: Path

    def __init__(self, home: Path, *, migrate: bool = True) -> None:
        self._home = validate_scs_home(home)
        self._database = self._home / "catalog.db"
        if migrate and self._database.exists():
            self._migrate_schema()

    @property
    def database_path(self) -> Path:
        """Return the catalog database path without creating it."""

        return self._database

    def lookup(self, root: str | Path) -> CatalogRecord | None:
        """Find a registered root without changing persistent state."""

        canonical_root = canonical_repository_root(root)
        if not self._database.exists():
            return None
        connection = self._connect_read_only()
        try:
            row = cast(
                tuple[int, str, str, str | None, str] | None,
                connection.execute(
                    """
                    SELECT project_id, canonical_root, store_id, active_generation, state
                    FROM project_stores WHERE canonical_root = ?
                    """,
                    (canonical_root,),
                ).fetchone(),
            )
        finally:
            connection.close()
        return _record_from_row(row) if row is not None else None

    def lookup_project(self, project_id: int) -> CatalogRecord | None:
        """Resolve one positive durable project ID without changing state."""

        if project_id < 1:
            raise ValueError("project_id must be positive")
        if not self._database.exists():
            return None
        connection = self._connect_read_only()
        try:
            row = cast(
                tuple[int, str, str, str | None, str] | None,
                connection.execute(
                    """
                    SELECT project_id, canonical_root, store_id, active_generation, state
                    FROM project_stores WHERE project_id = ?
                    """,
                    (project_id,),
                ).fetchone(),
            )
        finally:
            connection.close()
        return _record_from_row(row) if row is not None else None

    def register(self, root: str | Path) -> CatalogRecord:
        """Create or return the unique catalog record for an explicit index request.

        Registration creates only the central catalog. Project-store directories
        remain absent until the caller invokes ``ProjectStorePaths.ensure``.
        """

        canonical_root = canonical_repository_root(root)
        derived_store_id = store_id_for_root(canonical_root)
        self._home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._home.chmod(0o700)
        connection = sqlite3.connect(self._database, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(_SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO project_stores (canonical_root, store_id, active_generation, state)
                VALUES (?, ?, NULL, ?)
                ON CONFLICT(canonical_root) DO NOTHING
                """,
                (canonical_root, derived_store_id, StoreState.UNINITIALIZED.value),
            )
            row = cast(
                tuple[int, str, str, str | None, str] | None,
                connection.execute(
                    """
                    SELECT project_id, canonical_root, store_id, active_generation, state
                    FROM project_stores WHERE canonical_root = ?
                    """,
                    (canonical_root,),
                ).fetchone(),
            )
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            connection.execute("ROLLBACK")
            raise CatalogError(f"Could not register project store for {canonical_root}") from exc
        finally:
            connection.close()
        if row is None:
            raise CatalogError(f"Catalog did not persist project store for {canonical_root}")
        record = _record_from_row(row)
        if record.store_id != derived_store_id:
            raise CatalogError(
                f"Catalog store identity mismatch for canonical root {canonical_root}"
            )
        return record

    def activate(
        self,
        root: str | Path,
        *,
        generation: StoreGeneration,
        state: StoreState,
    ) -> CatalogRecord:
        """Publish one verified generation after explicit store creation."""

        canonical_root = canonical_repository_root(root)
        safe_generation = validate_store_generation(generation)
        if state is StoreState.UNINITIALIZED:
            raise ValueError("an activated store cannot be uninitialized")
        connection = sqlite3.connect(self._database, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE project_stores
                SET active_generation = ?, state = ?
                WHERE canonical_root = ?
                """,
                (safe_generation, state.value, canonical_root),
            )
            if cursor.rowcount != 1:
                raise CatalogError(f"Project store is not registered: {canonical_root}")
            row = cast(
                tuple[int, str, str, str | None, str],
                connection.execute(
                    """
                    SELECT project_id, canonical_root, store_id, active_generation, state
                    FROM project_stores WHERE canonical_root = ?
                    """,
                    (canonical_root,),
                ).fetchone(),
            )
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            connection.execute("ROLLBACK")
            raise CatalogError(f"Could not activate project store for {canonical_root}") from exc
        finally:
            connection.close()
        return _record_from_row(row)

    def update_state(
        self,
        root: str | Path,
        *,
        expected_generation: StoreGeneration,
        state: StoreState,
    ) -> CatalogRecord:
        """Transition state only when the expected generation is still active.

        A queued job carries an immutable generation binding. The conditional
        update prevents an old worker from publishing readiness for a newer
        generation after a catalog cutover.
        """

        canonical_root = canonical_repository_root(root)
        generation = validate_store_generation(expected_generation)
        connection = sqlite3.connect(self._database, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE project_stores
                SET state = ?
                WHERE canonical_root = ? AND active_generation = ?
                """,
                (state.value, canonical_root, generation),
            )
            if cursor.rowcount != 1:
                raise CatalogError(
                    "project-store generation no longer matches the durable job"
                )
            row = cast(
                tuple[int, str, str, str | None, str],
                connection.execute(
                    """
                    SELECT project_id, canonical_root, store_id, active_generation, state
                    FROM project_stores WHERE canonical_root = ?
                    """,
                    (canonical_root,),
                ).fetchone(),
            )
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            connection.execute("ROLLBACK")
            raise CatalogError(f"Could not update project-store state for {canonical_root}") from exc
        finally:
            connection.close()
        return _record_from_row(row)

    def unregister(
        self,
        root: str | Path,
        *,
        expected_store_id: StoreId,
        expected_generation: StoreGeneration | None,
    ) -> bool:
        """Remove exactly the project-store binding owned by a deletion job.

        A recovered job must never unregister a replacement generation created
        for the same repository root. Returning ``False`` for an absent or
        different binding makes the durable deletion boundary idempotent.
        """

        canonical_root = canonical_repository_root(root)
        store_id = validate_store_id(expected_store_id)
        generation = (
            validate_store_generation(expected_generation)
            if expected_generation is not None
            else None
        )
        if not self._database.exists():
            return False
        connection = sqlite3.connect(self._database, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            if generation is None:
                cursor = connection.execute(
                    """
                    DELETE FROM project_stores
                    WHERE canonical_root = ?
                      AND store_id = ?
                      AND active_generation IS NULL
                    """,
                    (canonical_root, store_id),
                )
            else:
                cursor = connection.execute(
                    """
                    DELETE FROM project_stores
                    WHERE canonical_root = ?
                      AND store_id = ?
                      AND active_generation = ?
                    """,
                    (canonical_root, store_id, generation),
                )
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            connection.execute("ROLLBACK")
            raise CatalogError(
                f"Could not unregister project store for {canonical_root}"
            ) from exc
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return cursor.rowcount == 1

    def list_records(self) -> list[CatalogRecord]:
        """List registered stores without creating a catalog or project data."""

        if not self._database.exists():
            return []
        connection = self._connect_read_only()
        try:
            rows = cast(
                list[tuple[int, str, str, str | None, str]],
                connection.execute(
                    """
                    SELECT project_id, canonical_root, store_id, active_generation, state
                    FROM project_stores ORDER BY project_id
                    """
                ).fetchall(),
            )
        finally:
            connection.close()
        return [_record_from_row(row) for row in rows]

    def _migrate_schema(self) -> None:
        """Atomically add durable numeric identities to a legacy catalog."""

        connection = sqlite3.connect(self._database, isolation_level=None)
        try:
            column_rows = cast(
                list[tuple[int, str, str, int, object, int]],
                connection.execute("PRAGMA table_info(project_stores)").fetchall(),
            )
            columns = {row[1] for row in column_rows}
            if not columns or "project_id" in columns:
                return
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ALTER TABLE project_stores RENAME TO legacy_project_stores")
            connection.execute(_SCHEMA)
            connection.execute(
                """
                INSERT INTO project_stores (
                    canonical_root, store_id, active_generation, state
                )
                SELECT canonical_root, store_id, active_generation, state
                FROM legacy_project_stores
                ORDER BY canonical_root
                """
            )
            connection.execute("DROP TABLE legacy_project_stores")
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _connect_read_only(self) -> sqlite3.Connection:
        """Open the existing catalog without SQLite creating sidecar files."""

        try:
            return sqlite3.connect(self._database.as_uri() + "?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise CatalogError(f"Could not read project-store catalog {self._database}") from exc


def _record_from_row(row: tuple[int, str, str, str | None, str]) -> CatalogRecord:
    """Validate SQLite's untyped row before exposing a typed catalog record."""

    project_id, canonical_root, raw_store_id, raw_generation, raw_state = row
    try:
        if project_id < 1:
            raise ValueError("project ID must be a positive integer")
        generation = (
            validate_store_generation(StoreGeneration(raw_generation))
            if raw_generation is not None
            else None
        )
        return CatalogRecord(
            project_id=project_id,
            canonical_root=canonical_root,
            store_id=validate_store_id(StoreId(raw_store_id)),
            active_generation=generation,
            state=StoreState(raw_state),
        )
    except (TypeError, ValueError) as exc:
        raise CatalogError("Catalog contains an invalid project-store record") from exc
