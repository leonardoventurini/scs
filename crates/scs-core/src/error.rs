//! Error types for the SCS knowledge graph engine.
//!
//! All fallible operations return `SCSError`, which wraps underlying
//! errors (SQLite, I/O, JSON) with context about what operation failed.

use thiserror::Error;

/// Unified error type for all SCS operations.
#[derive(Error, Debug)]
pub enum SCSError {
    /// SQLite operation failed (query, insert, schema creation, etc.).
    ///
    /// Holds a stringified error — `scs-core` doesn't depend on rusqlite
    /// by default. Crates that enable the `rusqlite` feature get an automatic
    /// `From<rusqlite::Error>` conversion so `?` propagation works as before.
    #[error("database error: {0}")]
    Database(String),

    /// r2d2 connection pool error (timeout, exhaustion).
    #[error("connection pool error: {0}")]
    Pool(String),

    /// JSON serialization or deserialization failed on metadata blobs.
    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),

    /// Filesystem I/O error during file discovery or reading.
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    /// A requested entity was not found in the graph.
    #[error("not found: {0}")]
    NotFound(String),

    /// Schema migration failed — database may be in an inconsistent state.
    #[error("migration error: {0}")]
    Migration(String),

    /// Invalid configuration (bad path, unsupported dimension, etc.).
    #[error("config error: {0}")]
    Config(String),

    /// Tree-sitter parsing failed on a source file.
    #[error("parse error: {0}")]
    Parse(String),

    /// Embedding dimension mismatch between vector and schema.
    #[error("embedding dimension mismatch: expected {expected}, got {actual}")]
    DimensionMismatch { expected: usize, actual: usize },

    /// Storage engine error (USearch, vector index, etc.).
    #[error("storage error: {0}")]
    Storage(String),
}

/// Convenience alias used throughout the crate.
pub type SCSResult<T> = Result<T, SCSError>;

/// Auto-convert `rusqlite::Error` → `SCSError::Database` when the
/// `rusqlite` feature is enabled, restoring seamless `?` propagation
/// for crates that depend on both scs-core and rusqlite.
#[cfg(feature = "rusqlite")]
impl From<rusqlite::Error> for SCSError {
    fn from(e: rusqlite::Error) -> Self {
        SCSError::Database(e.to_string())
    }
}
