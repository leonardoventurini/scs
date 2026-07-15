//! `scs-store` — SQLite + USearch storage layer for the SCS knowledge graph.
//!
//! Provides the complete storage backend:
//! - [`KnowledgeGraph`] — CRUD, vector search, graph traversal, Graph RAG
//! - [`batch`] — Bulk upsert operations for the ingestion pipeline
//! - [`ingestion_files`] — File tracking for incremental code ingestion
//! - [`connection`] — r2d2 connection pool
//! - [`schema`] — DDL constants and direct schema initialization

pub mod batch;
pub mod connection;
pub mod graph;
pub mod ingestion_files;
pub mod observability;
pub mod schema;
pub mod vector_index;

// Re-export the primary types at crate root.
pub use connection::ConnectionPool;
pub use graph::{EdgeDirection, KnowledgeGraph, TraversalDirection, VacuumResult};
pub use vector_index::VectorIndex;

/// Extension trait for converting r2d2 pool errors to SCSError.
///
/// The orphan rule prevents us from implementing `From<r2d2::Error>` for
/// `SCSError` outside of `scs-core`, and we don't want to add r2d2
/// as a dependency of scs-core. This trait provides a clean alternative.
pub(crate) trait PoolResultExt<T> {
    fn pool_err(self) -> scs_core::error::SCSResult<T>;
}

impl<T> PoolResultExt<T> for Result<T, r2d2::Error> {
    fn pool_err(self) -> scs_core::error::SCSResult<T> {
        self.map_err(|e| scs_core::error::SCSError::Pool(e.to_string()))
    }
}
