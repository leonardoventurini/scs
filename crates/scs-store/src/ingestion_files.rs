//! SCS ingestion catalog types stored through TSG.

use serde::Serialize;

/// Per-repository ingestion statistics.
#[derive(Debug, Clone, serde::Serialize)]
pub struct IngestionStats {
    pub file_count: i64,
    pub last_indexed: String,
}

/// One durable acknowledgement of successfully indexed source content.
#[derive(Debug, Clone, serde::Deserialize, serde::Serialize)]
pub struct IngestedFileRecord {
    pub file_id: String,
    pub repo_path: String,
    pub rel_path: String,
    pub language: String,
    pub content_hash: String,
    pub byte_size: i64,
}

/// Counts returned after dropping a repository scope.
#[derive(Debug, Clone, Serialize)]
pub struct DeleteRepoResult {
    pub files_removed: i64,
    pub nodes_removed: i64,
    pub embeddings_removed: i64,
}
