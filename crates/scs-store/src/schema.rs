//! DDL constants and direct schema initialization.
//!
//! The knowledge graph lives in a single SQLite database. This module
//! defines the SQL statements that create the current schema. All DDL is
//! idempotent (`IF NOT EXISTS`) so startup can always apply it directly.

use rusqlite::Connection;

use scs_core::error::SCSResult;

/// Core nodes table — vertices of the knowledge graph.
pub const DDL_NODES: &str = "
CREATE TABLE IF NOT EXISTS nodes (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL CHECK(type IN (
        'file', 'module', 'class', 'function', 'method', 'variable',
        'constant', 'import', 'type_alias', 'commit', 'contributor'
    )),
    name       TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    metadata   TEXT NOT NULL DEFAULT '{}',
    repo_id    INTEGER REFERENCES repos(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
";

/// Indexes on nodes for filtering, case-insensitive lookup, and stable recency order.
pub const DDL_NODES_INDEXES: &[&str] = &[
    "CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);",
    "CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name COLLATE NOCASE);",
    "CREATE INDEX IF NOT EXISTS idx_nodes_repo_id ON nodes(repo_id);",
    "CREATE INDEX IF NOT EXISTS idx_nodes_updated_at_id ON nodes(updated_at DESC, id DESC);",
    "CREATE INDEX IF NOT EXISTS idx_nodes_type_updated_at_id ON nodes(type, updated_at DESC, id DESC);",
    "CREATE INDEX IF NOT EXISTS idx_nodes_repo_updated_at_id ON nodes(repo_id, updated_at DESC, id DESC);",
    "CREATE INDEX IF NOT EXISTS idx_nodes_repo_type_updated_at_id ON nodes(repo_id, type, updated_at DESC, id DESC);",
];

/// Remove schema objects owned by capabilities that SCS no longer exposes.
const LEGACY_SCHEMA_CLEANUP: &str = "
DROP INDEX IF EXISTS idx_nodes_summarizable_repo_id_id;
";

/// Normalized repository table — stores repo paths once, referenced by FK.
///
/// Integer PK enables fast B-tree comparisons in repo-scoped search queries
/// and avoids repeating 60-byte path strings per node. `UNIQUE(path)` ensures
/// `INSERT OR IGNORE` + `SELECT` is idempotent for `get_or_create_repo`.
pub const DDL_REPOS: &str = "
CREATE TABLE IF NOT EXISTS repos (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE
);
";

/// Directed edges table — relationships between nodes.
pub const DDL_EDGES: &str = "
CREATE TABLE IF NOT EXISTS edges (
    id           TEXT PRIMARY KEY,
    source_id    TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_id    TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    relationship TEXT NOT NULL CHECK(relationship IN (
        'contains', 'calls', 'imports', 'inherits', 'implements',
        'references', 'authored_by', 'modifies'
    )),
    weight       REAL NOT NULL DEFAULT 1.0,
    metadata     TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
";

/// Indexes on edges for efficient traversal in both directions.
pub const DDL_EDGES_INDEXES: &[&str] = &[
    "CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);",
    "CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);",
    "CREATE INDEX IF NOT EXISTS idx_edges_source_rel ON edges(source_id, relationship);",
    "CREATE INDEX IF NOT EXISTS idx_edges_target_rel ON edges(target_id, relationship);",
];

/// File ingestion tracking table — records which files have been indexed
/// and their content hashes for incremental change detection.
pub const DDL_INGESTED_FILES: &str = "
CREATE TABLE IF NOT EXISTS ingested_files (
    id           TEXT PRIMARY KEY,
    repo_path    TEXT NOT NULL,
    rel_path     TEXT NOT NULL,
    language     TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    byte_size    INTEGER NOT NULL,
    indexed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(repo_path, rel_path)
);
";

/// Returns all DDL statements in order for initial schema creation.
///
pub fn all_ddl() -> Vec<String> {
    let mut stmts = Vec::with_capacity(15);
    stmts.push(DDL_REPOS.to_string());
    stmts.push(DDL_NODES.to_string());
    for idx in DDL_NODES_INDEXES {
        stmts.push(idx.to_string());
    }
    stmts.push(DDL_EDGES.to_string());
    for idx in DDL_EDGES_INDEXES {
        stmts.push(idx.to_string());
    }
    stmts.push(DDL_INGESTED_FILES.to_string());
    stmts
}

/// Ensure the current schema exists before the graph serves requests.
pub fn initialize_schema(conn: &Connection) -> SCSResult<()> {
    conn.execute_batch(
        "DROP TABLE IF EXISTS community_assignments;
         DROP TABLE IF EXISTS communities;",
    )?;
    conn.execute_batch(LEGACY_SCHEMA_CLEANUP)?;
    for ddl in all_ddl() {
        conn.execute_batch(&ddl)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::connection::create_test_pool;

    #[test]
    fn all_ddl_returns_correct_count() {
        // repos + nodes + 7 indexes + edges + 4 indexes + ingested_files
        // = 15
        assert_eq!(all_ddl().len(), 15);
    }

    #[test]
    fn ddl_matches_current_table_names() {
        assert!(DDL_REPOS.contains("repos"));
        assert!(DDL_NODES.contains("nodes"));
        assert!(DDL_EDGES.contains("edges"));
        assert!(DDL_INGESTED_FILES.contains("ingested_files"));
    }

    #[test]
    fn initialize_schema_is_idempotent_and_removes_retired_indexes() {
        let dir = tempfile::tempdir().unwrap();
        let pool = create_test_pool(&dir.path().join("test.db")).unwrap();
        let conn = pool.get().unwrap();

        initialize_schema(&conn).unwrap();
        let historical_node_metadata =
            r#"{"file_path":"src/main.py","summary":"Legacy node summary"}"#;
        let historical_edge_metadata = r#"{"summary":"Legacy edge summary"}"#;
        conn.execute(
            "INSERT INTO nodes (id, type, name, metadata) VALUES ('node-a', 'file', 'a', ?1)",
            [historical_node_metadata],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO nodes (id, type, name) VALUES ('node-b', 'function', 'b')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO edges (id, source_id, target_id, relationship, metadata)
             VALUES ('edge-a-b', 'node-a', 'node-b', 'contains', ?1)",
            [historical_edge_metadata],
        )
        .unwrap();
        conn.execute_batch("CREATE INDEX idx_nodes_summarizable_repo_id_id ON nodes(repo_id, id);")
            .unwrap();
        initialize_schema(&conn).unwrap();

        let index_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master
             WHERE type = 'index'
               AND name = 'idx_nodes_summarizable_repo_id_id'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(index_count, 0);

        let active_index_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master
                 WHERE type = 'index'
                   AND name = 'idx_nodes_repo_type_updated_at_id'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(active_index_count, 1);

        let preserved_node_metadata: String = conn
            .query_row(
                "SELECT metadata FROM nodes WHERE id = 'node-a'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let preserved_edge_metadata: String = conn
            .query_row(
                "SELECT metadata FROM edges WHERE id = 'edge-a-b'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(preserved_node_metadata, historical_node_metadata);
        assert_eq!(preserved_edge_metadata, historical_edge_metadata);
    }
}
