//! Forward-only SQLite schema migrations for the SCS graph store.
//!
//! SQLite's `user_version` and `schema_migrations` ledger advance together in
//! one `BEGIN IMMEDIATE` transaction. The ledger is durable evidence of every
//! migration that ran; startup never repairs schema by dropping objects.

use rusqlite::Connection;

use scs_core::error::{SCSError, SCSResult};

/// The newest SQLite schema supported by this binary.
pub const CURRENT_SCHEMA_VERSION: i64 = 2;

/// Durable, forward-only record of applied SQLite migrations.
pub const DDL_SCHEMA_MIGRATIONS: &str = "
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY CHECK(version > 0),
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
";

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

/// Normalized repository table — stores repo paths once, referenced by FK.
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

/// File ingestion tracking table — records which files have been indexed.
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

/// SQLite is the authoritative embedding store; the USearch sidecar is derived.
///
/// `payload_f32` is canonical little-endian IEEE-754 f32 data. The length
/// check makes malformed vectors impossible to commit. `vector_key` is fixed
/// width hexadecimal text because SQLite INTEGER cannot represent all u64 keys.
pub const DDL_EMBEDDING_RECORDS: &str = "
CREATE TABLE IF NOT EXISTS embedding_records (
    node_id              TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    vector_key           TEXT NOT NULL UNIQUE CHECK(length(vector_key) = 16),
    provider_id          TEXT NOT NULL,
    model_id             TEXT NOT NULL,
    dimension            INTEGER NOT NULL CHECK(dimension > 0),
    content_digest       TEXT NOT NULL,
    payload_encoding     TEXT NOT NULL DEFAULT 'f32le' CHECK(payload_encoding = 'f32le'),
    payload_f32          BLOB NOT NULL CHECK(length(payload_f32) = dimension * 4),
    payload_digest       TEXT NOT NULL,
    vector_generation    INTEGER NOT NULL CHECK(vector_generation >= 0),
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
";

/// Indexes used by provider/generation parity checks and sidecar rebuilds.
pub const DDL_EMBEDDING_RECORDS_INDEXES: &[&str] = &[
    "CREATE INDEX IF NOT EXISTS idx_embedding_records_generation ON embedding_records(vector_generation);",
    "CREATE INDEX IF NOT EXISTS idx_embedding_records_provider_model ON embedding_records(provider_id, model_id);",
];

/// Returns the DDL needed by a fresh database at the current version.
pub fn all_ddl() -> Vec<String> {
    let mut stmts = Vec::with_capacity(19);
    stmts.push(DDL_SCHEMA_MIGRATIONS.to_string());
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
    stmts.push(DDL_EMBEDDING_RECORDS.to_string());
    for idx in DDL_EMBEDDING_RECORDS_INDEXES {
        stmts.push(idx.to_string());
    }
    stmts
}

/// Ensure every migration up to [`CURRENT_SCHEMA_VERSION`] is applied.
///
/// Version-zero databases predate the ledger. Migration one intentionally uses
/// idempotent creation statements to adopt them without deleting their data.
pub fn initialize_schema(conn: &Connection) -> SCSResult<()> {
    conn.execute_batch("PRAGMA foreign_keys = ON; BEGIN IMMEDIATE;")?;
    match apply_pending_migrations(conn) {
        Ok(()) => conn.execute_batch("COMMIT;").map_err(Into::into),
        Err(error) => {
            if let Err(rollback_error) = conn.execute_batch("ROLLBACK;") {
                return Err(SCSError::Migration(format!(
                    "migration failed ({error}); rollback also failed ({rollback_error})"
                )));
            }
            Err(error)
        }
    }
}

/// Apply the ordered migration list while an immediate transaction is held.
fn apply_pending_migrations(conn: &Connection) -> SCSResult<()> {
    let user_version = read_user_version(conn)?;
    if user_version > CURRENT_SCHEMA_VERSION {
        return Err(SCSError::Migration(format!(
            "database schema version {user_version} is newer than supported version {CURRENT_SCHEMA_VERSION}"
        )));
    }
    for version in (user_version + 1)..=CURRENT_SCHEMA_VERSION {
        apply_migration(conn, version)?;
        conn.execute(
            "INSERT INTO schema_migrations (version) VALUES (?1)",
            [version],
        )?;
        conn.execute_batch(&format!("PRAGMA user_version = {version};"))?;
    }
    validate_migration_ledger(conn, CURRENT_SCHEMA_VERSION)
}

/// Apply one forward-only migration. New migrations are append-only match arms.
fn apply_migration(conn: &Connection, version: i64) -> SCSResult<()> {
    match version {
        1 => {
            conn.execute_batch(DDL_SCHEMA_MIGRATIONS)?;
            conn.execute_batch(DDL_REPOS)?;
            conn.execute_batch(DDL_NODES)?;
            for ddl in DDL_NODES_INDEXES {
                conn.execute_batch(ddl)?;
            }
            conn.execute_batch(DDL_EDGES)?;
            for ddl in DDL_EDGES_INDEXES {
                conn.execute_batch(ddl)?;
            }
            conn.execute_batch(DDL_INGESTED_FILES)?;
        }
        2 => {
            conn.execute_batch(DDL_EMBEDDING_RECORDS)?;
            for ddl in DDL_EMBEDDING_RECORDS_INDEXES {
                conn.execute_batch(ddl)?;
            }
        }
        _ => {
            return Err(SCSError::Migration(format!(
                "no migration is defined for schema version {version}"
            )))
        }
    }
    Ok(())
}

/// Read SQLite's application-managed schema version.
fn read_user_version(conn: &Connection) -> SCSResult<i64> {
    Ok(conn.query_row("PRAGMA user_version", [], |row| row.get(0))?)
}

/// Reject a tampered or partial migration ledger before serving requests.
fn validate_migration_ledger(conn: &Connection, expected_version: i64) -> SCSResult<()> {
    let versions = conn
        .prepare("SELECT version FROM schema_migrations ORDER BY version")?
        .query_map([], |row| row.get::<_, i64>(0))?
        .collect::<Result<Vec<_>, _>>()?;
    let expected: Vec<i64> = (1..=expected_version).collect();
    if versions != expected {
        return Err(SCSError::Migration(format!(
            "schema migration ledger {versions:?} does not match expected {expected:?}"
        )));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::connection::create_test_pool;

    #[test]
    fn all_ddl_contains_current_schema_objects() {
        assert_eq!(all_ddl().len(), 20);
        assert!(DDL_SCHEMA_MIGRATIONS.contains("schema_migrations"));
        assert!(DDL_EMBEDDING_RECORDS.contains("embedding_records"));
        assert!(DDL_NODES_INDEXES
            .iter()
            .any(|ddl| ddl.contains("idx_nodes_repo_qualified_name")));
    }

    #[test]
    fn initialize_schema_records_every_migration_and_is_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        let pool = create_test_pool(&dir.path().join("test.db")).unwrap();
        let conn = pool.get().unwrap();
        initialize_schema(&conn).unwrap();
        initialize_schema(&conn).unwrap();
        assert_eq!(read_user_version(&conn).unwrap(), CURRENT_SCHEMA_VERSION);
        let versions = conn
            .prepare("SELECT version FROM schema_migrations ORDER BY version")
            .unwrap()
            .query_map([], |row| row.get::<_, i64>(0))
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        let qualified_name_index: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' AND name = 'idx_nodes_repo_qualified_name'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(qualified_name_index, 1);
        assert_eq!(versions, vec![1, 2, 3]);
    }

    #[test]
    fn migration_adopts_legacy_schema_without_dropping_data_or_tables() {
        let dir = tempfile::tempdir().unwrap();
        let pool = create_test_pool(&dir.path().join("test.db")).unwrap();
        let conn = pool.get().unwrap();
        conn.execute_batch(DDL_REPOS).unwrap();
        conn.execute_batch(DDL_NODES).unwrap();
        conn.execute(
            "INSERT INTO nodes (id, type, name) VALUES ('node-a', 'file', 'a')",
            [],
        )
        .unwrap();
        conn.execute_batch("CREATE TABLE communities (id TEXT PRIMARY KEY); INSERT INTO communities VALUES ('legacy');").unwrap();
        initialize_schema(&conn).unwrap();
        let node_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM nodes", [], |row| row.get(0))
            .unwrap();
        let community: String = conn
            .query_row("SELECT id FROM communities", [], |row| row.get(0))
            .unwrap();
        assert_eq!(node_count, 1);
        assert_eq!(community, "legacy");
    }

    #[test]
    fn embedding_records_enforce_payload_shape_and_vector_key_uniqueness() {
        let dir = tempfile::tempdir().unwrap();
        let pool = create_test_pool(&dir.path().join("test.db")).unwrap();
        let conn = pool.get().unwrap();
        initialize_schema(&conn).unwrap();
        conn.execute(
            "INSERT INTO nodes (id, type, name) VALUES ('node-a', 'file', 'a')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO nodes (id, type, name) VALUES ('node-b', 'file', 'b')",
            [],
        )
        .unwrap();
        let invalid_payload = conn.execute("INSERT INTO embedding_records (node_id, vector_key, provider_id, model_id, dimension, content_digest, payload_f32, payload_digest, vector_generation) VALUES ('node-a', '0000000000000001', 'omlx', 'model', 2, 'content', X'00000000', 'payload', 0)", []);
        assert!(invalid_payload.is_err());
        conn.execute("INSERT INTO embedding_records (node_id, vector_key, provider_id, model_id, dimension, content_digest, payload_f32, payload_digest, vector_generation) VALUES ('node-a', '0000000000000001', 'omlx', 'model', 1, 'content', X'00000000', 'payload', 0)", []).unwrap();
        let duplicate_key = conn.execute("INSERT INTO embedding_records (node_id, vector_key, provider_id, model_id, dimension, content_digest, payload_f32, payload_digest, vector_generation) VALUES ('node-b', '0000000000000001', 'omlx', 'model', 1, 'content', X'00000000', 'payload', 0)", []);
        assert!(duplicate_key.is_err());
    }

    #[test]
    fn newer_database_version_is_rejected_without_mutation() {
        let dir = tempfile::tempdir().unwrap();
        let pool = create_test_pool(&dir.path().join("test.db")).unwrap();
        let conn = pool.get().unwrap();
        conn.execute_batch("PRAGMA user_version = 999;").unwrap();
        let error = initialize_schema(&conn).unwrap_err();
        assert!(error.to_string().contains("newer than supported"));
        assert_eq!(read_user_version(&conn).unwrap(), 999);
    }
}
