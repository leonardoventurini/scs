"""Privacy-preserving durable aggregate operation metrics."""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, cast

METRICS_SCHEMA_VERSION: Final[int] = 1
DEFAULT_RETENTION_DAYS: Final[int] = 30
DEFAULT_MAX_ROWS: Final[int] = 50_000
KEY_BYTES: Final[int] = 32
ALLOWED_SEARCH_MODES: Final[frozenset[str]] = frozenset(
    {"fast", "balanced", "thorough"}
)
ALLOWED_RESULT_DETAILS: Final[frozenset[str]] = frozenset({"full", "compact"})


class AggregateMetrics:
    """Store bounded hourly counters without request or response content."""

    def __init__(
        self,
        database_path: Path,
        key_path: Path,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        if retention_days < 1 or max_rows < 1:
            raise ValueError("metrics retention and row bounds must be positive")
        self._database_path: Path = database_path
        self._key_path: Path = key_path
        self._retention_days: int = retention_days
        self._max_rows: int = max_rows
        self._lock: threading.Lock = threading.Lock()
        self._key: bytes = self._load_or_create_key()
        self._initialize_with_recovery()

    def record(
        self,
        method: str,
        params: dict[str, object],
        *,
        status: str,
        duration_ms: float,
    ) -> None:
        """Increment one allowlisted aggregate bucket and enforce bounds."""

        now = datetime.now(UTC)
        hour = now.replace(minute=0, second=0, microsecond=0).isoformat()
        repo_id = self._repo_id(params.get("repo_path"))
        search_mode = self._allowed_string(params.get("search_mode"), ALLOWED_SEARCH_MODES)
        result_detail = self._allowed_string(
            params.get("result_detail"), ALLOWED_RESULT_DETAILS
        )
        normalized_status = "error" if status == "error" else "ok"
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO hourly_metrics (
                    hour, method, repo_id, search_mode, result_detail, status,
                    call_count, total_duration_ms, max_duration_ms, error_count
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT (
                    hour, method, repo_id, search_mode, result_detail, status
                ) DO UPDATE SET
                    call_count = call_count + 1,
                    total_duration_ms = total_duration_ms + excluded.total_duration_ms,
                    max_duration_ms = MAX(max_duration_ms, excluded.max_duration_ms),
                    error_count = error_count + excluded.error_count
                """,
                (
                    hour,
                    method,
                    repo_id,
                    search_mode,
                    result_detail,
                    normalized_status,
                    max(0.0, duration_ms),
                    max(0.0, duration_ms),
                    int(normalized_status == "error"),
                ),
            )
            cutoff = (now - timedelta(days=self._retention_days)).isoformat()
            connection.execute("DELETE FROM hourly_metrics WHERE hour < ?", (cutoff,))
            connection.execute(
                """
                DELETE FROM hourly_metrics
                WHERE id IN (
                    SELECT id FROM hourly_metrics
                    ORDER BY hour ASC, id ASC
                    LIMIT MAX(0, (SELECT COUNT(*) FROM hourly_metrics) - ?)
                )
                """,
                (self._max_rows,),
            )

    def report(self, *, days: int = 7) -> dict[str, object]:
        """Return aggregate counters for a bounded recent window."""

        bounded_days = max(1, min(days, self._retention_days))
        cutoff = (datetime.now(UTC) - timedelta(days=bounded_days)).isoformat()
        with self._lock, closing(self._connect()) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT hour, method, repo_id, search_mode, result_detail, status,
                           call_count, total_duration_ms, max_duration_ms, error_count
                    FROM hourly_metrics
                    WHERE hour >= ?
                    ORDER BY hour DESC, method, repo_id
                    """,
                    (cutoff,),
                ).fetchall(),
            )
        operations = [dict(row) for row in rows]
        return {
            "days": bounded_days,
            "totals": {
                "calls": sum(cast(int, row["call_count"]) for row in rows),
                "errors": sum(cast(int, row["error_count"]) for row in rows),
            },
            "operations": operations,
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_with_recovery(self) -> None:
        self._database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self._initialize()
        except sqlite3.DatabaseError:
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            for suffix in ("", "-wal", "-shm"):
                source = Path(f"{self._database_path}{suffix}")
                if source.exists():
                    source.replace(
                        source.with_name(f"{source.name}.corrupt-{timestamp}")
                    )
            self._initialize()

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS hourly_metrics (
                    id INTEGER PRIMARY KEY,
                    hour TEXT NOT NULL,
                    method TEXT NOT NULL,
                    repo_id TEXT NOT NULL,
                    search_mode TEXT NOT NULL,
                    result_detail TEXT NOT NULL,
                    status TEXT NOT NULL,
                    call_count INTEGER NOT NULL,
                    total_duration_ms REAL NOT NULL,
                    max_duration_ms REAL NOT NULL,
                    error_count INTEGER NOT NULL,
                    UNIQUE (hour, method, repo_id, search_mode, result_detail, status)
                )
                """
            )
            connection.execute(f"PRAGMA user_version = {METRICS_SCHEMA_VERSION}")
        self._database_path.chmod(0o600)

    def _load_or_create_key(self) -> bytes:
        self._key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self._key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as key_file:
                key_file.write(os.urandom(KEY_BYTES))
        self._key_path.chmod(0o600)
        key = self._key_path.read_bytes()
        if len(key) != KEY_BYTES:
            raise ValueError("metrics key must contain exactly 32 bytes")
        return key

    def _repo_id(self, value: object) -> str:
        if not isinstance(value, str) or not value:
            return ""
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()[:16]

    @staticmethod
    def _allowed_string(value: object, allowed: frozenset[str]) -> str:
        return value if isinstance(value, str) and value in allowed else ""
