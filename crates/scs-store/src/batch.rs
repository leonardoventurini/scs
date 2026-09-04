//! Typed batch inputs retained by the SCS compatibility boundary.

use std::collections::HashMap;

use scs_core::node_types::NodeType;

/// Input for a batch node upsert operation.
#[derive(Debug, Clone, serde::Deserialize)]
pub struct BatchNode {
    pub id: String,
    #[serde(rename = "type")]
    pub node_type: NodeType,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub content: String,
    #[serde(default)]
    pub metadata: HashMap<String, serde_json::Value>,
    #[serde(default)]
    pub repo_id: Option<i64>,
}

/// Input for a batch edge upsert operation.
#[derive(Debug, Clone, serde::Deserialize)]
pub struct BatchEdge {
    pub source_id: String,
    pub target_id: String,
    pub relationship: String,
    #[serde(default = "default_weight")]
    pub weight: f64,
    #[serde(default)]
    pub metadata: HashMap<String, serde_json::Value>,
}

fn default_weight() -> f64 {
    1.0
}
