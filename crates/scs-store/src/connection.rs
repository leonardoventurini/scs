//! Thread-safe SQLite connection pool.
//!
//! Uses r2d2 for connection pooling, replacing the Python implementation's
//! thread-local pattern. Each connection is configured with:
//! - WAL journal mode for concurrent read/write performance
//! - 5s busy timeout for brief lock contention
//! - Foreign keys for CASCADE delete propagation
//! - mmap/cache tuning for large knowledge-graph workloads

use std::path::Path;

use r2d2::Pool;
use r2d2_sqlite::SqliteConnectionManager;
use rusqlite::Connection;

use scs_core::error::{SCSError, SCSResult};
use scs_core::SCSConfig;

/// Initialize PRAGMAs on a new SQLite connection.
///
/// Called by r2d2_sqlite's `SqliteConnectionManager::with_init` for
/// every connection created by the pool.
fn init_connection(conn: &mut Connection) -> Result<(), rusqlite::Error> {
    // Wait up to 5 seconds on lock contention before raising SQLITE_BUSY.
    conn.execute_batch("PRAGMA busy_timeout = 5000;")?;
    // Enable WAL mode — allows concurrent reads while writing.
    conn.execute_batch("PRAGMA journal_mode = WAL;")?;
    // Enforce foreign key constraints for CASCADE delete on edges.
    conn.execute_batch("PRAGMA foreign_keys = ON;")?;
    // 64 MB page cache — the default 2 MB causes excessive disk I/O
    // on large knowledge graphs (277K+ nodes, 1 GB+ database).
    conn.execute_batch("PRAGMA cache_size = -64000;")?;
    // 256 MB memory-mapped I/O — accelerates both read and write paths
    // by letting the OS page cache serve hot pages without read() syscalls.
    conn.execute_batch("PRAGMA mmap_size = 268435456;")?;
    Ok(())
}

/// r2d2 connection pool wrapping SQLite.
///
/// Provides thread-safe access to the database with automatic
/// connection recycling and PRAGMA configuration.
pub type ConnectionPool = Pool<SqliteConnectionManager>;

/// Create a connection pool for the given configuration.
///
/// Ensures the database directory exists and configures each connection
/// with the required PRAGMAs.
pub fn create_pool(config: &SCSConfig) -> SCSResult<ConnectionPool> {
    // Ensure parent directory exists.
    if let Some(parent) = config.db_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    let manager = SqliteConnectionManager::file(&config.db_path).with_init(init_connection);

    let pool = Pool::builder()
        .max_size(config.pool_size)
        .build(manager)
        .map_err(|e| SCSError::Pool(e.to_string()))?;

    Ok(pool)
}

/// Create an in-memory connection pool for testing.
///
/// Uses a file-based database at the given path (not `:memory:`) so
/// multiple connections in the pool share the same database state.
pub fn create_test_pool(db_path: &Path) -> SCSResult<ConnectionPool> {
    let config = SCSConfig {
        db_path: db_path.to_path_buf(),
        index_path: db_path.with_extension("usearch"),
        embedding_dim: 768,
        pool_size: 2,
    };
    create_pool(&config)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pool_creates_and_connects() {
        let dir = tempfile::tempdir().unwrap();
        let pool = create_test_pool(&dir.path().join("test.db")).unwrap();
        let conn = pool.get().unwrap();
        let version: String = conn
            .query_row("SELECT sqlite_version()", [], |row| row.get(0))
            .unwrap();
        assert!(!version.is_empty());
    }

    #[test]
    fn pool_connections_have_pragmas() {
        let dir = tempfile::tempdir().unwrap();
        let pool = create_test_pool(&dir.path().join("test.db")).unwrap();
        let conn = pool.get().unwrap();

        // Verify WAL mode.
        let journal: String = conn
            .query_row("PRAGMA journal_mode", [], |row| row.get(0))
            .unwrap();
        assert_eq!(journal, "wal");

        // Verify foreign keys are enabled.
        let fk: i32 = conn
            .query_row("PRAGMA foreign_keys", [], |row| row.get(0))
            .unwrap();
        assert_eq!(fk, 1);
    }
}
