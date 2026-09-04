//! `scs-store` — SCS compatibility adapter over TSG.
//!
//! Provides the complete storage backend:
//! - [`KnowledgeGraph`] — CRUD, vector search, graph traversal, Graph RAG
//! - [`batch`] — typed bulk inputs for the ingestion pipeline
//! - [`ingestion_files`] — typed incremental-ingestion catalog records

pub mod batch;
pub mod graph;
pub mod ingestion_files;
pub mod observability;

// Re-export the primary types at crate root.
pub use graph::{EdgeDirection, KnowledgeGraph, TraversalDirection, VacuumResult};
