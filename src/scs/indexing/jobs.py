"""Durable repository ingestion job queue."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

logger = logging.getLogger(__name__)

IngestionJobMode = Literal["files", "full", "force_full", "cleanup", "drop_index"]
IngestionJobStatus = Literal[
    "queued",
    "running",
    "retrying",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
]

ACTIVE_QUEUE_STATUSES = ("queued", "retrying")
SQLITE_DATABASE_FAMILY_SUFFIXES = ("", "-wal", "-shm")
SQLITE_CORRUPTION_MESSAGE_FRAGMENTS = (
    "database disk image is malformed",
    "file is not a database",
)
SQLITE_CORRUPTION_ERROR_CODES = tuple(
    code
    for code in (
        getattr(sqlite3, "SQLITE_CORRUPT", None),
        getattr(sqlite3, "SQLITE_NOTADB", None),
    )
    if code is not None
)


@dataclass(frozen=True)
class IngestionJob:
    """One durable ingestion job row."""

    id: str
    repo_path: str
    store_id: str | None
    store_generation: str | None
    mode: IngestionJobMode
    reason: str
    payload: dict[str, object]
    status: IngestionJobStatus
    phase: str
    current: int
    total: int
    message: str
    attempts: int
    max_attempts: int
    lease_owner: str | None
    lease_expires_at: str | None
    error: str | None
    result: dict[str, object] | None
    created_at: str
    updated_at: str
    finished_at: str | None


@dataclass(frozen=True)
class DatabaseQuarantine:
    """Corrupt SQLite files moved aside so a durable queue can be rebuilt."""

    original_path: Path
    moved_paths: tuple[Path, ...]
    reason: str


def utc_now() -> str:
    """Return a SQLite-friendly UTC timestamp."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_after(seconds: float) -> str:
    return (
        (datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=seconds))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _normalize_payload(
    mode: IngestionJobMode, payload: dict[str, object] | None
) -> dict[str, object]:
    normalized = dict(payload or {})
    if mode == "files":
        file_paths = cast(Iterable[object], normalized.get("file_paths", []))
        normalized["file_paths"] = sorted({str(path) for path in file_paths})
        deleted_paths = cast(Iterable[object], normalized.get("deleted_paths", []))
        normalized["deleted_paths"] = sorted({str(path) for path in deleted_paths})
    return normalized


def _mode_rank(mode: str) -> int:
    return {
        "cleanup": 0,
        "files": 1,
        "full": 2,
        "force_full": 3,
        "drop_index": 4,
    }.get(mode, 0)


def _merge_payload(
    existing_mode: IngestionJobMode,
    existing_payload: dict[str, object],
    new_mode: IngestionJobMode,
    new_payload: dict[str, object],
) -> tuple[IngestionJobMode, dict[str, object]]:
    """Merge a queued job with a new request for the same repository."""
    if existing_mode == "drop_index" or new_mode == "drop_index":
        return "drop_index", {}

    winner = (
        new_mode if _mode_rank(new_mode) > _mode_rank(existing_mode) else existing_mode
    )
    if winner in {"full", "force_full", "cleanup"}:
        return winner, {}

    file_paths = set(cast(Iterable[str], existing_payload.get("file_paths", [])))
    file_paths.update(cast(Iterable[str], new_payload.get("file_paths", [])))
    deleted_paths = set(cast(Iterable[str], existing_payload.get("deleted_paths", [])))
    deleted_paths.update(cast(Iterable[str], new_payload.get("deleted_paths", [])))

    return "files", {
        "file_paths": sorted(file_paths),
        "deleted_paths": sorted(deleted_paths),
    }


def _is_recoverable_sqlite_corruption(error: sqlite3.DatabaseError) -> bool:
    sqlite_error_code = getattr(error, "sqlite_errorcode", None)
    if sqlite_error_code in SQLITE_CORRUPTION_ERROR_CODES:
        return True
    message = str(error).lower()
    return any(fragment in message for fragment in SQLITE_CORRUPTION_MESSAGE_FRAGMENTS)


def _database_file_family(db_path: Path) -> tuple[Path, ...]:
    return tuple(
        Path(f"{db_path}{suffix}") for suffix in SQLITE_DATABASE_FAMILY_SUFFIXES
    )


def _unique_quarantine_path(source_path: Path, timestamp: str) -> Path:
    destination_path = source_path.with_name(f"{source_path.name}.corrupt-{timestamp}")
    if not destination_path.exists():
        return destination_path

    collision_index = 1
    while True:
        candidate_path = source_path.with_name(
            f"{source_path.name}.corrupt-{timestamp}-{collision_index}",
        )
        if not candidate_path.exists():
            return candidate_path
        collision_index += 1


def _row_object(row: sqlite3.Row, key: str) -> object:
    """Contain sqlite3's untyped row access at the persistence boundary."""

    return cast(object, row[key])


def _row_str(row: sqlite3.Row, key: str) -> str:
    return cast(str, _row_object(row, key))


def _row_optional_str(row: sqlite3.Row, key: str) -> str | None:
    return cast(str | None, _row_object(row, key))


def _row_int(row: sqlite3.Row, key: str) -> int:
    return int(cast(int | str, _row_object(row, key)))


def _row_mode(row: sqlite3.Row, key: str = "mode") -> IngestionJobMode:
    return cast(IngestionJobMode, _row_str(row, key))


def _row_status(row: sqlite3.Row, key: str = "status") -> IngestionJobStatus:
    return cast(IngestionJobStatus, _row_str(row, key))


class IngestionJobStore:
    """SQLite-backed durable ingestion queue.

    Connections are opened per operation so the store can be used from async
    handlers and worker threads without sharing sqlite3 connection objects.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path: Path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._init_schema()
        except sqlite3.DatabaseError as exc:
            if not _is_recoverable_sqlite_corruption(exc):
                raise
            quarantine = self._quarantine_corrupt_database(exc)
            logger.warning(
                "Quarantined corrupt ingestion job database at %s after SQLite error %r; "
                "moved files: %s",
                quarantine.original_path,
                quarantine.reason,
                [str(path) for path in quarantine.moved_paths],
            )
            self._init_schema()

    def _quarantine_corrupt_database(
        self, error: sqlite3.DatabaseError
    ) -> DatabaseQuarantine:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        moved_paths: list[Path] = []
        for source_path in _database_file_family(self.db_path):
            if not source_path.exists():
                continue
            destination_path = _unique_quarantine_path(source_path, timestamp)
            source_path.replace(destination_path)
            moved_paths.append(destination_path)
        return DatabaseQuarantine(
            original_path=self.db_path,
            moved_paths=tuple(moved_paths),
            reason=str(error),
        )

    def enqueue(
        self,
        *,
        repo_path: str,
        store_id: str | None = None,
        store_generation: str | None = None,
        mode: IngestionJobMode,
        reason: str,
        payload: dict[str, object] | None = None,
        max_attempts: int = 3,
    ) -> IngestionJob:
        normalized_payload = _normalize_payload(mode, payload)
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = cast(
                sqlite3.Row | None,
                conn.execute(
                    """
                SELECT * FROM ingestion_jobs
                WHERE repo_path = ? AND status IN ('queued', 'retrying')
                ORDER BY created_at ASC
                LIMIT 1
                """,
                    (repo_path,),
                ).fetchone(),
            )
            if existing is not None:
                existing_payload = _loads(_row_str(existing, "payload_json"))
                merged_mode, merged_payload = _merge_payload(
                    _row_mode(existing),
                    existing_payload,
                    mode,
                    normalized_payload,
                )
                conn.execute(
                    """
                    UPDATE ingestion_jobs
                    SET mode = ?, reason = ?, payload_json = ?, updated_at = ?,
                        status = 'queued', error = NULL
                    WHERE id = ?
                    """,
                    (
                        merged_mode,
                        reason or _row_str(existing, "reason"),
                        json.dumps(merged_payload, sort_keys=True),
                        now,
                        _row_str(existing, "id"),
                    ),
                )
                conn.commit()
                return self._get_locked(conn, _row_str(existing, "id"))

            job_id = f"ingest_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """
                INSERT INTO ingestion_jobs (
                    id, repo_path, store_id, store_generation, mode, reason, payload_json, status, phase,
                    current, total, message, attempts, max_attempts,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', 0, 0, '', 0, ?, ?, ?)
                """,
                (
                    job_id,
                    repo_path,
                    store_id,
                    store_generation,
                    mode,
                    reason,
                    json.dumps(normalized_payload, sort_keys=True),
                    max_attempts,
                    now,
                    now,
                ),
            )
            conn.commit()
            return self._get_locked(conn, job_id)

    def claim_next(
        self, *, lease_owner: str, lease_seconds: float = 300
    ) -> IngestionJob | None:
        now = utc_now()
        lease_expires_at = _utc_after(lease_seconds)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = cast(
                sqlite3.Row | None,
                conn.execute(
                    """
                    SELECT candidate.*
                FROM ingestion_jobs AS candidate
                WHERE candidate.status IN ('queued', 'retrying')
                  AND NOT EXISTS (
                    SELECT 1
                    FROM ingestion_jobs AS running
                    WHERE COALESCE(running.store_id, running.repo_path)
                        = COALESCE(candidate.store_id, candidate.repo_path)
                      AND running.status IN ('running', 'cancelling')
                  )
                ORDER BY
                  CASE candidate.mode WHEN 'cleanup' THEN 1 ELSE 0 END,
                  candidate.created_at ASC
                LIMIT 1
                """
                ).fetchone(),
            )
            if row is None:
                conn.rollback()
                return None

            conn.execute(
                """
                UPDATE ingestion_jobs
                SET status = 'running', phase = 'running',
                    lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (lease_owner, lease_expires_at, now, _row_str(row, "id")),
            )
            conn.commit()
            return self._get_locked(conn, _row_str(row, "id"))

    def heartbeat(
        self, job_id: str, *, lease_owner: str, lease_seconds: float = 300
    ) -> None:
        now = utc_now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE ingestion_jobs
                SET lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('running', 'cancelling')
                """,
                (lease_owner, _utc_after(lease_seconds), now, job_id),
            )

    def update_progress(
        self,
        job_id: str,
        *,
        phase: str,
        current: int,
        total: int,
        message: str = "",
    ) -> None:
        now = utc_now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE ingestion_jobs
                SET phase = ?, current = ?, total = ?, message = ?, updated_at = ?
                WHERE id = ? AND status IN ('running', 'cancelling')
                """,
                (phase, max(0, current), max(0, total), message, now, job_id),
            )

    def complete(
        self, job_id: str, *, result: dict[str, object] | None = None
    ) -> IngestionJob:
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = cast(
                sqlite3.Row | None,
                conn.execute(
                    "SELECT status FROM ingestion_jobs WHERE id = ?", (job_id,)
                ).fetchone(),
            )
            if row is None:
                raise KeyError(job_id)
            status = "cancelled" if _row_status(row) == "cancelling" else "completed"
            conn.execute(
                """
                UPDATE ingestion_jobs
                SET status = ?, phase = ?, current = total,
                    message = '', lease_owner = NULL, lease_expires_at = NULL,
                    error = NULL, result_json = ?, updated_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    status,
                    json.dumps(result or {}, sort_keys=True),
                    now,
                    now,
                    job_id,
                ),
            )
            conn.commit()
            return self._get_locked(conn, job_id)

    def fail_or_retry(self, job_id: str, *, error: str) -> IngestionJob:
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = cast(
                sqlite3.Row | None,
                conn.execute(
                    "SELECT * FROM ingestion_jobs WHERE id = ?",
                    (job_id,),
                ).fetchone(),
            )
            if row is None:
                raise KeyError(job_id)

            attempts = _row_int(row, "attempts") + 1
            status = "failed" if attempts >= _row_int(row, "max_attempts") else "queued"
            if status == "queued":
                queued = cast(
                    sqlite3.Row | None,
                    conn.execute(
                        """
                    SELECT *
                    FROM ingestion_jobs
                    WHERE repo_path = ? AND status IN ('queued', 'retrying') AND id != ?
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                        (_row_str(row, "repo_path"), job_id),
                    ).fetchone(),
                )
                if queued is not None:
                    merged_mode, merged_payload = _merge_payload(
                        _row_mode(queued),
                        _loads(_row_str(queued, "payload_json")),
                        _row_mode(row),
                        _loads(_row_str(row, "payload_json")),
                    )
                    conn.execute(
                        """
                        UPDATE ingestion_jobs
                        SET mode = ?, payload_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            merged_mode,
                            json.dumps(merged_payload, sort_keys=True),
                            now,
                            _row_str(queued, "id"),
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE ingestion_jobs
                        SET status = 'cancelled', phase = 'cancelled',
                            attempts = ?, lease_owner = NULL, lease_expires_at = NULL,
                            error = ?, updated_at = ?, finished_at = ?
                        WHERE id = ?
                        """,
                        (
                            attempts,
                            f"{error}; merged into queued follow-up job",
                            now,
                            now,
                            job_id,
                        ),
                    )
                    conn.commit()
                    return self._get_locked(conn, _row_str(queued, "id"))

            finished_at = now if status == "failed" else None
            conn.execute(
                """
                UPDATE ingestion_jobs
                SET status = ?, phase = ?, attempts = ?, message = '',
                    lease_owner = NULL, lease_expires_at = NULL, error = ?,
                    updated_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, status, attempts, error, now, finished_at, job_id),
            )
            conn.commit()
            return self._get_locked(conn, job_id)

    def request_cancel(self, job_id: str) -> IngestionJob:
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = cast(
                sqlite3.Row | None,
                conn.execute(
                    "SELECT status FROM ingestion_jobs WHERE id = ?", (job_id,)
                ).fetchone(),
            )
            if row is None:
                raise KeyError(job_id)
            status = _row_status(row)
            if status in ACTIVE_QUEUE_STATUSES:
                conn.execute(
                    """
                    UPDATE ingestion_jobs
                    SET status = 'cancelled', phase = 'cancelled',
                        updated_at = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (now, now, job_id),
                )
            elif status == "running":
                conn.execute(
                    """
                    UPDATE ingestion_jobs
                    SET status = 'cancelling', phase = 'cancelling', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, job_id),
                )
            conn.commit()
            return self._get_locked(conn, job_id)

    def mark_cancelled(self, job_id: str) -> IngestionJob:
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE ingestion_jobs
                SET status = 'cancelled', phase = 'cancelled',
                    lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (now, now, job_id),
            )
            conn.commit()
            return self._get_locked(conn, job_id)

    def reclaim_stale_running(
        self,
        *,
        lease_owner: str | None = None,
        reclaim_other_owners: bool = False,
    ) -> list[IngestionJob]:
        now = utc_now()
        reclaimed: list[IngestionJob] = []
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            owner_clause = ""
            params: list[object] = [now]
            if reclaim_other_owners and lease_owner:
                owner_clause = " OR lease_owner IS NULL OR lease_owner != ?"
                params.append(lease_owner)
            stale_rows = cast(
                list[sqlite3.Row],
                conn.execute(
                    f"""
                SELECT *
                FROM ingestion_jobs
                WHERE status IN ('running', 'cancelling')
                  AND lease_expires_at IS NOT NULL
                  AND (lease_expires_at < ?{owner_clause})
                ORDER BY created_at ASC
                """,
                    params,
                ).fetchall(),
            )

            for row in stale_rows:
                owner_mismatch = bool(
                    reclaim_other_owners
                    and lease_owner
                    and (
                        _row_optional_str(row, "lease_owner") is None
                        or _row_optional_str(row, "lease_owner") != lease_owner
                    )
                )
                queued = cast(
                    sqlite3.Row | None,
                    conn.execute(
                        """
                    SELECT *
                    FROM ingestion_jobs
                    WHERE repo_path = ? AND status IN ('queued', 'retrying')
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                        (_row_str(row, "repo_path"),),
                    ).fetchone(),
                )
                if queued is not None:
                    merged_mode, merged_payload = _merge_payload(
                        _row_mode(queued),
                        _loads(_row_str(queued, "payload_json")),
                        _row_mode(row),
                        _loads(_row_str(row, "payload_json")),
                    )
                    conn.execute(
                        """
                        UPDATE ingestion_jobs
                        SET mode = ?, payload_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            merged_mode,
                            json.dumps(merged_payload, sort_keys=True),
                            now,
                            _row_str(queued, "id"),
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE ingestion_jobs
                        SET status = 'cancelled', phase = 'cancelled',
                            lease_owner = NULL, lease_expires_at = NULL,
                            error = 'Reclaimed into queued follow-up job',
                            updated_at = ?, finished_at = ?
                        WHERE id = ?
                        """,
                        (now, now, _row_str(row, "id")),
                    )
                    queued_row = cast(
                        sqlite3.Row,
                        conn.execute(
                            "SELECT * FROM ingestion_jobs WHERE id = ?",
                            (_row_str(queued, "id"),),
                        ).fetchone(),
                    )
                    reclaimed.append(self._row_to_job(queued_row))
                    continue

                attempts = _row_int(row, "attempts")
                if not owner_mismatch:
                    attempts += 1
                status = (
                    "failed" if attempts >= _row_int(row, "max_attempts") else "queued"
                )
                finished_at = now if status == "failed" else None
                error = (
                    "Reclaimed from previous worker process"
                    if owner_mismatch
                    else "Lease expired while job was running"
                )
                conn.execute(
                    """
                    UPDATE ingestion_jobs
                    SET status = ?, phase = ?, attempts = ?,
                        lease_owner = NULL, lease_expires_at = NULL,
                        error = ?, updated_at = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        status,
                        attempts,
                        error,
                        now,
                        finished_at,
                        _row_str(row, "id"),
                    ),
                )
                reclaimed_row = cast(
                    sqlite3.Row,
                    conn.execute(
                        "SELECT * FROM ingestion_jobs WHERE id = ?",
                        (_row_str(row, "id"),),
                    ).fetchone(),
                )
                reclaimed.append(self._row_to_job(reclaimed_row))

            conn.commit()
        return reclaimed

    def get(self, job_id: str) -> IngestionJob | None:
        with closing(self._connect()) as conn, conn:
            row = cast(
                sqlite3.Row | None,
                conn.execute(
                    "SELECT * FROM ingestion_jobs WHERE id = ?", (job_id,)
                ).fetchone(),
            )
        return self._row_to_job(row) if row is not None else None

    def list_recent(
        self,
        *,
        repo_path: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[IngestionJob]:
        clauses: list[str] = []
        params: list[object] = []
        if repo_path:
            clauses.append("repo_path = ?")
            params.append(repo_path)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 200)))
        with closing(self._connect()) as conn, conn:
            rows = cast(
                list[sqlite3.Row],
                conn.execute(
                    f"""
                SELECT *
                FROM ingestion_jobs
                {where}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                    params,
                ).fetchall(),
            )
        return [self._row_to_job(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ingestion_jobs (
                    id TEXT PRIMARY KEY,
                    repo_path TEXT NOT NULL,
                    store_id TEXT,
                    store_generation TEXT,
                    mode TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT '',
                    current INTEGER NOT NULL DEFAULT 0,
                    total INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    error TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_status_created
                    ON ingestion_jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_repo_updated
                    ON ingestion_jobs(repo_path, updated_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ingestion_jobs_one_queued_per_repo
                    ON ingestion_jobs(repo_path)
                    WHERE status IN ('queued', 'retrying');
                """
            )
            schema_rows = cast(
                list[sqlite3.Row],
                conn.execute("PRAGMA table_info(ingestion_jobs)").fetchall(),
            )
            columns = {_row_str(row, "name") for row in schema_rows}
            if "store_id" not in columns:
                conn.execute("ALTER TABLE ingestion_jobs ADD COLUMN store_id TEXT")
            if "store_generation" not in columns:
                conn.execute(
                    "ALTER TABLE ingestion_jobs ADD COLUMN store_generation TEXT"
                )

    def _get_locked(self, conn: sqlite3.Connection, job_id: str) -> IngestionJob:
        row = cast(
            sqlite3.Row | None,
            conn.execute(
                "SELECT * FROM ingestion_jobs WHERE id = ?", (job_id,)
            ).fetchone(),
        )
        if row is None:
            raise KeyError(job_id)
        return self._row_to_job(row)

    def _row_to_job(self, row: sqlite3.Row) -> IngestionJob:
        return IngestionJob(
            id=_row_str(row, "id"),
            repo_path=_row_str(row, "repo_path"),
            store_id=_row_optional_str(row, "store_id"),
            store_generation=_row_optional_str(row, "store_generation"),
            mode=_row_mode(row),
            reason=_row_str(row, "reason"),
            payload=_loads(_row_str(row, "payload_json")),
            status=_row_status(row),
            phase=_row_str(row, "phase"),
            current=_row_int(row, "current"),
            total=_row_int(row, "total"),
            message=_row_str(row, "message"),
            attempts=_row_int(row, "attempts"),
            max_attempts=_row_int(row, "max_attempts"),
            lease_owner=_row_optional_str(row, "lease_owner"),
            lease_expires_at=_row_optional_str(row, "lease_expires_at"),
            error=_row_optional_str(row, "error"),
            result=(
                _loads(_row_str(row, "result_json"))
                if _row_optional_str(row, "result_json")
                else None
            ),
            created_at=_row_str(row, "created_at"),
            updated_at=_row_str(row, "updated_at"),
            finished_at=_row_optional_str(row, "finished_at"),
        )


def _loads(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    try:
        data = cast(object, json.loads(value))
    except json.JSONDecodeError:
        return {}
    return cast(dict[str, object], data) if isinstance(data, dict) else {}


def job_to_dict(job: IngestionJob) -> dict[str, object]:
    """Serialize an ingestion job for wire/API responses."""
    return {
        "id": job.id,
        "repo_path": job.repo_path,
        "store_id": job.store_id,
        "store_generation": job.store_generation,
        "mode": job.mode,
        "reason": job.reason,
        "payload": job.payload,
        "status": job.status,
        "phase": job.phase,
        "current": job.current,
        "total": job.total,
        "message": job.message,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "lease_owner": job.lease_owner,
        "lease_expires_at": job.lease_expires_at,
        "error": job.error,
        "result": job.result,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "finished_at": job.finished_at,
    }
