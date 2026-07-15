//! Data models for the knowledge graph's public API.
//!
//! These structs define the data contracts between the `KnowledgeGraph`
//! service and its consumers (pipeline, API routes, ingestion). All graph
//! operations accept and return these types rather than raw maps or tuples.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use crate::node_types::NodeType;

/// A repository registered in the knowledge graph.
///
/// The `repos` table provides a normalized FK target for nodes, avoiding
/// repeated 60-byte repo path strings on every node row. The integer PK
/// enables fast B-tree comparisons in repo-scoped search queries.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Repo {
    pub id: i64,
    pub path: String,
}

/// A vertex in the knowledge graph.
///
/// The `metadata` JSON blob preserves parser and provider fields such as
/// signatures, documentation, source locations, and model provenance.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Node {
    pub id: String,
    #[serde(rename = "type")]
    pub node_type: NodeType,
    pub name: String,
    pub content: String,
    pub metadata: HashMap<String, serde_json::Value>,
    /// FK to the `repos` table. Provenance nodes may be populated before a
    /// repository association is resolved, so this remains optional.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub repo_id: Option<i64>,
    pub created_at: Option<String>,
    pub updated_at: Option<String>,
}

/// A directed, weighted edge between two nodes.
///
/// Relationships are restricted by the schema to [`RelationshipType`] values.
/// Weight defaults to 1.0 and can represent frequency or confidence.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Edge {
    pub id: String,
    pub source_id: String,
    pub target_id: String,
    pub relationship: String,
    pub weight: f64,
    pub metadata: HashMap<String, serde_json::Value>,
    pub created_at: Option<String>,
}

/// A node returned from vector similarity search.
///
/// Distance is the raw vector-distance score reported by the search index.
/// Lower distance = more similar.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchResult {
    pub node: Node,
    pub distance: f64,
}

/// A node discovered during graph traversal.
///
/// Depth indicates how many hops from the start node (0 = start node
/// itself). Path records the chain of node IDs from start to this node
/// for cycle detection and provenance.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TraversalResult {
    pub node: Node,
    pub depth: i32,
    pub path: Vec<String>,
}

/// Combined vector search + graph traversal result for RAG context.
///
/// Contains both the semantically similar nodes from vector search and
/// additional graph context nodes discovered via edge traversal.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GraphRagResult {
    pub similar_nodes: Vec<SearchResult>,
    pub graph_context: Vec<Node>,
}
