//! Configuration for the SCS knowledge graph.
//!
//! All settings have sensible defaults matching the Python implementation.
//! Override via `SCSConfig::builder()` for testing or alternate deployments.

use std::path::{Path, PathBuf};

/// Derive the USearch index path from a database path.
///
/// Replaces the `.db` extension with `.usearch`, keeping the index file
/// co-located with the database for easy discovery and backup.
fn derive_index_path(db_path: &Path) -> PathBuf {
    db_path.with_extension("usearch")
}

/// Configuration for a SCS knowledge graph instance.
///
/// Controls database location, vector index location, embedding dimensions,
/// and connection pool sizing. Defaults match the Python SCS implementation
/// for backward compatibility.
#[derive(Debug, Clone)]
pub struct SCSConfig {
    /// Path to the SQLite database file.
    /// Default: `~/.scs/index.db`
    pub db_path: PathBuf,

    /// Path to the USearch HNSW index file.
    /// Default: same directory as `db_path`, named `index.usearch`.
    pub index_path: PathBuf,

    /// Embedding vector dimension for the vector index.
    /// Must match the dimension of vectors passed to search/upsert.
    /// Default: 768 (Nomic Embed Text v1.5 output dimension).
    pub embedding_dim: usize,

    /// Maximum number of connections in the r2d2 pool.
    /// Default: 4 (sufficient for most single-app workloads).
    pub pool_size: u32,
}

impl Default for SCSConfig {
    fn default() -> Self {
        let db_path = default_db_path();
        let index_path = derive_index_path(&db_path);
        Self {
            db_path,
            index_path,
            embedding_dim: 768,
            pool_size: 4,
        }
    }
}

impl SCSConfig {
    /// Create a new config with the given database path, using defaults
    /// for all other settings. The index path is derived automatically.
    pub fn with_db_path(db_path: impl Into<PathBuf>) -> Self {
        let db_path = db_path.into();
        let index_path = derive_index_path(&db_path);
        Self {
            db_path,
            index_path,
            ..Default::default()
        }
    }

    /// Create a config suitable for testing — uses the given temp directory
    /// for the database and index files.
    pub fn for_testing(dir: &Path) -> Self {
        let db_path = dir.join("test_index.db");
        let index_path = derive_index_path(&db_path);
        Self {
            db_path,
            index_path,
            embedding_dim: 768,
            pool_size: 2,
        }
    }
}

/// Default database path under the independent SCS data root.
///
/// Reads `SCS_HOME` when set, otherwise falls back to `~/.scs`.
fn default_db_path() -> PathBuf {
    if let Ok(scs_home) = std::env::var("SCS_HOME") {
        return PathBuf::from(scs_home).join("index.db");
    }
    if let Some(home) = dirs_path() {
        home.join(".scs").join("index.db")
    } else {
        PathBuf::from("index.db")
    }
}

/// Platform-aware home directory resolution.
fn dirs_path() -> Option<PathBuf> {
    std::env::var("HOME").ok().map(PathBuf::from)
}
