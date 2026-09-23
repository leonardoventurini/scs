"""Read saved project indexes without starting the indexing daemon."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast

from scs.storage.catalog import ProjectStoreCatalog
from scs.storage.models import StoreState
from scs.storage.paths import ProjectStorePaths

INGESTION_NAMESPACE = "scs.ingested-files"


def list_saved_projects(home: Path) -> dict[str, object]:
    """Return a catalog-ordered snapshot of committed project index data."""

    catalog = ProjectStoreCatalog(home, migrate=False)
    projects: list[dict[str, object]] = []

    for record in catalog.list_records():
        file_count = 0
        last_indexed: str | None = None
        saved_index_exists = False

        if record.active_generation is not None:
            paths = ProjectStorePaths.resolve(
                home, record.store_id, record.active_generation
            )
            if paths.database.exists():
                saved_index_exists = True
                with closing(
                    sqlite3.connect(paths.database.as_uri() + "?mode=ro", uri=True)
                ) as connection:
                    row = cast(
                        tuple[int, str | None],
                        connection.execute(
                            """
                            SELECT COUNT(*), MAX(json_extract(value, '$.indexed_at'))
                            FROM catalog
                            WHERE namespace = ?
                              AND json_extract(value, '$.repo_path') = ?
                            """,
                            (INGESTION_NAMESPACE, record.canonical_root),
                        ).fetchone(),
                    )
                    file_count, last_indexed = row

        indexed = saved_index_exists and (
            record.state is StoreState.SEMANTIC_READY or file_count > 0
        )
        projects.append(
            {
                "id": record.project_id,
                "repo_path": record.canonical_root,
                "state": "indexed" if indexed else "unindexed",
                "file_count": file_count,
                "last_indexed": last_indexed or None,
                "active_job_id": None,
            }
        )

    return {"projects": projects}
