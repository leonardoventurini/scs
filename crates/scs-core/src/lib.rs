//! `scs-core` — Core types, enums, and configuration for the SCS knowledge graph engine.
//!
//! This crate contains zero heavy dependencies and provides the foundational
//! types shared across all other SCS crates:
//!
//! - [`NodeType`] and [`RelationshipType`] — enum discriminators matching the Python implementation
//! - [`Node`], [`Edge`], [`SearchResult`], [`TraversalResult`] — data models
//! - [`SCSError`] — unified error type
//! - [`SCSConfig`] — configuration with sensible defaults

pub mod config;
pub mod error;
pub mod models;
pub mod node_types;

// Re-export the most commonly used types at crate root for convenience.
pub use config::SCSConfig;
pub use error::{SCSError, SCSResult};
pub use models::{Edge, GraphRagResult, Node, SearchResult, TraversalResult};
pub use node_types::{NodeType, RelationshipType};
