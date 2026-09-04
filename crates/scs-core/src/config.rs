//! Configuration for the SCS knowledge graph.
//!
//! All settings have sensible defaults matching the Python implementation.
//! Override via `SCSConfig::builder()` for testing or alternate deployments.

use std::path::{Path, PathBuf};

/// Derive the legacy USearch sidecar path from a database path.
///
/// SCS retains this path in its public configuration so a TSG cutover can
/// preserve the old sidecar for rollback. TSG owns its active accelerator.
fn derive_index_path(db_path: &Path) -> PathBuf {
    db_path.with_extension("usearch")
}

/// Configuration for a SCS knowledge graph instance.
///
/// Controls the TSG database location and embedding dimensions. The legacy
/// sidecar and pool settings remain for source compatibility and rollback.
#[derive(Debug, Clone)]
pub struct SCSConfig {
    /// Path to the authoritative TSG database file.
    /// Default: `~/.scs/index.db`
    pub db_path: PathBuf,

    /// Path to the legacy USearch HNSW sidecar, retained for rollback backup.
    /// Default: same directory as `db_path`, named `index.usearch`.
    pub index_path: PathBuf,

    /// Embedding vector dimension for the vector index.
    /// Must match the dimension of vectors passed to search/upsert.
    /// Default: 768 (Nomic Embed Text v1.5 output dimension).
    pub embedding_dim: usize,

    /// Legacy connection-pool setting retained for API compatibility.
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
