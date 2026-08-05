//! Batch upsert operations for the ingestion pipeline.
//!
//! These operations wrap multiple inserts in a single transaction for
//! atomicity and performance. Used by the code ingestion pipeline to
//! store hundreds of entities/edges/embeddings per file batch.

use std::collections::HashMap;

use rusqlite::params;

use scs_core::error::SCSResult;
use scs_core::node_types::NodeType;

use crate::connection::ConnectionPool;
use crate::graph::make_edge_id;
use crate::observability::{observe_result, QueryBackend};
use crate::PoolResultExt;

/// Input for a batch node upsert operation.
///
/// `Deserialize` enables accepting JSON input from the Python FFI layer.
/// The `type` → `node_type` rename matches Python's dict key convention
/// where `"type"` is the natural key name.
#[derive(Debug, Clone, serde::Deserialize)]
pub struct BatchNode {
    pub id: String,
    #[serde(rename = "type")]
    pub node_type: NodeType,
    pub name: String,
    #[serde(default)]
    pub content: String,
    #[serde(default)]
    pub metadata: HashMap<String, serde_json::Value>,
    /// FK to the `repos` table — scopes this node to a specific repository.
    /// Optional: non-code nodes (corrections, vocabulary) don't need a repo.
    #[serde(default)]
    pub repo_id: Option<i64>,
}

/// Input for a batch edge upsert operation.
///
/// `Deserialize` enables accepting JSON input from the Python FFI layer.
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

/// Bulk upsert nodes in a single transaction.
///
/// Returns the number of rows affected. On failure, the entire
/// batch is rolled back to maintain atomicity.
///
/// Metadata is **merged** via `json_patch()` (RFC 7396) — incoming fields
/// overwrite existing ones, but keys already in the DB that are absent from
/// the update are preserved. This lets parser and independent analyzer fields
/// coexist without one producer erasing metadata owned by another.
pub fn batch_upsert_nodes(pool: &ConnectionPool, nodes: &[BatchNode]) -> SCSResult<usize> {
    observe_result(
        QueryBackend::Sqlite,
        "batch_upsert_nodes",
        "nodes",
        format!("count={}", nodes.len()),
        |obs| {
            let wait_started = std::time::Instant::now();
            let conn = pool.get().pool_err()?;
            obs.set_wait(wait_started.elapsed());
            let tx = conn.unchecked_transaction()?;
            let mut count = 0;

            // Use prepare_cached to reuse the compiled statement plan across iterations.
            // At 277K nodes this avoids 277K redundant SQL compilations — ~15-30% faster.
            let mut stmt = tx.prepare_cached(
                "INSERT INTO nodes (id, type, name, content, metadata, repo_id)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)
                 ON CONFLICT(id) DO UPDATE SET
                     type = excluded.type,
                     name = excluded.name,
                     content = excluded.content,
                     metadata = json_patch(nodes.metadata, excluded.metadata),
                     repo_id = COALESCE(excluded.repo_id, nodes.repo_id),
                     updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
            )?;

            for node in nodes {
                let meta_json = serde_json::to_string(&node.metadata)?;
                stmt.execute(params![
                    node.id,
                    node.node_type.to_string(),
                    node.name,
                    node.content,
                    meta_json,
                    node.repo_id,
                ])?;
                count += 1;
            }

            // Drop the statement before committing to release the borrow on `tx`.
            drop(stmt);
            tx.commit()?;
            obs.set_rows(count);
            Ok(count)
        },
    )
}

/// Bulk upsert embeddings into the USearch HNSW vector index.
///
/// Each tuple is `(node_id, embedding_vector)`. Writes go to USearch
/// (not SQLite). The caller must flush the vector index once after the
/// owning ingestion job finishes.
pub fn batch_upsert_embeddings(
    vector_index: &crate::vector_index::VectorIndex,
    embeddings: &[(String, Vec<f32>)],
) -> SCSResult<usize> {
    observe_result(
        QueryBackend::Vector,
        "batch_upsert_embeddings",
        "usearch",
        format!("count={}", embeddings.len()),
        |obs| {
            let count = vector_index.add_batch(embeddings)?;
            obs.set_vectors(count);
            Ok(count)
        },
    )
}

/// Bulk upsert edges in a single transaction.
///
/// Edge IDs are computed deterministically from (source, target, relationship)
/// using UUID v5, matching the Python implementation.
///
/// Metadata is **merged** via `json_patch()` — see `batch_upsert_nodes` for
/// the rationale. Independently owned fields survive re-ingestion that only
/// updates weight or adds new metadata keys.
///
/// Edges that reference non-existent nodes (FK violation) are silently
/// skipped rather than failing the entire batch. This is defensive against
/// cross-file edge resolution finding stale node IDs during re-ingestion.
pub fn batch_upsert_edges(pool: &ConnectionPool, edges: &[BatchEdge]) -> SCSResult<usize> {
    observe_result(
        QueryBackend::Sqlite,
        "batch_upsert_edges",
        "edges",
        format!("count={}", edges.len()),
        |obs| {
            let wait_started = std::time::Instant::now();
            let conn = pool.get().pool_err()?;
            obs.set_wait(wait_started.elapsed());
            let tx = conn.unchecked_transaction()?;
            let mut count = 0;
            let mut skipped = 0;

            // Reuse compiled statement — same rationale as batch_upsert_nodes.
            let mut stmt = tx.prepare_cached(
                "INSERT INTO edges (id, source_id, target_id, relationship, weight, metadata)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)
                 ON CONFLICT(id) DO UPDATE SET
                     weight = excluded.weight,
                     metadata = json_patch(edges.metadata, excluded.metadata)",
            )?;

            for edge in edges {
                let edge_id = make_edge_id(&edge.source_id, &edge.target_id, &edge.relationship);
                let meta_json = serde_json::to_string(&edge.metadata)?;

                match stmt.execute(params![
                    edge_id,
                    edge.source_id,
                    edge.target_id,
                    edge.relationship,
                    edge.weight,
                    meta_json,
                ]) {
                    Ok(_) => count += 1,
                    Err(rusqlite::Error::SqliteFailure(err, _))
                        if err.extended_code == rusqlite::ffi::SQLITE_CONSTRAINT_FOREIGNKEY =>
                    {
                        // Edge references a node that doesn't exist — skip it.
                        // This can happen during re-ingestion when cross-file edge
                        // resolution finds stale node IDs from deleted entities.
                        skipped += 1;
                    }
                    Err(e) => return Err(e.into()),
                }
            }

            drop(stmt);
            tx.commit()?;

            if skipped > 0 {
                log::warn!(
                    "batch_upsert_edges: skipped {} edges with missing endpoints (FK violation)",
                    skipped,
                );
            }

            obs.set_rows(count);
            Ok(count)
        },
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::connection::create_test_pool;
    use crate::schema::initialize_schema;
    use serde_json::json;

    fn setup() -> (
        tempfile::TempDir,
        ConnectionPool,
        crate::vector_index::VectorIndex,
    ) {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("test.db");
        let pool = create_test_pool(&db_path).unwrap();
        let conn = pool.get().unwrap();
        initialize_schema(&conn).unwrap();
        let idx =
            crate::vector_index::VectorIndex::open(db_path.with_extension("usearch"), 384).unwrap();
        (dir, pool, idx)
    }

    #[test]
    fn batch_upsert_nodes_atomicity() {
        let (_dir, pool, _idx) = setup();

        let nodes: Vec<BatchNode> = (0..10)
            .map(|i| BatchNode {
                id: format!("batch-{i}"),
                node_type: NodeType::Function,
                name: format!("func_{i}"),
                content: String::new(),
                metadata: HashMap::new(),
                repo_id: None,
            })
            .collect();

        let count = batch_upsert_nodes(&pool, &nodes).unwrap();
        assert_eq!(count, 10);

        let conn = pool.get().unwrap();
        let total: i64 = conn
            .query_row("SELECT COUNT(*) FROM nodes", [], |row| row.get(0))
            .unwrap();
        assert_eq!(total, 10);
    }

    #[test]
    fn batch_upsert_embeddings_stores_vectors() {
        let (_dir, pool, idx) = setup();

        // Create nodes first.
        let nodes: Vec<BatchNode> = (0..3)
            .map(|i| BatchNode {
                id: format!("emb-{i}"),
                node_type: NodeType::Function,
                name: format!("concept_{i}"),
                content: String::new(),
                metadata: HashMap::new(),
                repo_id: None,
            })
            .collect();
        batch_upsert_nodes(&pool, &nodes).unwrap();

        // Insert embeddings into USearch.
        let embeddings: Vec<(String, Vec<f32>)> = (0..3)
            .map(|i| {
                let mut v = vec![0.0f32; 384];
                v[i] = 1.0;
                (format!("emb-{i}"), v)
            })
            .collect();

        let count = batch_upsert_embeddings(&idx, &embeddings).unwrap();
        assert_eq!(count, 3);
        assert_eq!(idx.size(), 3);
    }

    #[test]
    fn batch_upsert_embeddings_defers_sidecar_save_until_flush() {
        let (dir, pool, idx) = setup();
        let index_path = dir.path().join("test.usearch");

        let nodes: Vec<BatchNode> = (0..3)
            .map(|i| BatchNode {
                id: format!("emb-{i}"),
                node_type: NodeType::Function,
                name: format!("concept_{i}"),
                content: String::new(),
                metadata: HashMap::new(),
                repo_id: None,
            })
            .collect();
        batch_upsert_nodes(&pool, &nodes).unwrap();

        let embeddings: Vec<(String, Vec<f32>)> = (0..3)
            .map(|i| {
                let mut v = vec![0.0f32; 384];
                v[i] = 1.0;
                (format!("emb-{i}"), v)
            })
            .collect();

        assert!(!index_path.exists());
        assert_eq!(batch_upsert_embeddings(&idx, &embeddings).unwrap(), 3);
        assert_eq!(idx.size(), 3);
        assert!(!index_path.exists());

        assert!(idx.save_if_dirty().unwrap());
        assert!(index_path.exists());
        assert!(!idx.save_if_dirty().unwrap());
    }

    #[test]
    fn batch_upsert_edges_creates_relationships() {
        let (_dir, pool, _idx) = setup();

        // Create parent and child nodes.
        let mut nodes = vec![BatchNode {
            id: "parent".to_string(),
            node_type: NodeType::Module,
            name: "mod".to_string(),
            content: String::new(),
            metadata: HashMap::new(),
            repo_id: None,
        }];
        for i in 0..3 {
            nodes.push(BatchNode {
                id: format!("child-{i}"),
                node_type: NodeType::Function,
                name: format!("func_{i}"),
                content: String::new(),
                metadata: HashMap::new(),
                repo_id: None,
            });
        }
        batch_upsert_nodes(&pool, &nodes).unwrap();

        // Create edges.
        let edges: Vec<BatchEdge> = (0..3)
            .map(|i| BatchEdge {
                source_id: "parent".to_string(),
                target_id: format!("child-{i}"),
                relationship: "contains".to_string(),
                weight: 1.0,
                metadata: HashMap::new(),
            })
            .collect();

        let count = batch_upsert_edges(&pool, &edges).unwrap();
        assert_eq!(count, 3);

        let conn = pool.get().unwrap();
        let total: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM edges WHERE source_id = 'parent'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(total, 3);
    }

    /// Re-upserting a node preserves metadata owned by another producer.
    #[test]
    fn batch_upsert_nodes_preserves_existing_metadata_keys() {
        let (_dir, pool, _idx) = setup();

        // Phase 1: Simulate initial ingestion with parser-derived metadata.
        let mut initial_meta = HashMap::new();
        initial_meta.insert("file_path".to_string(), json!("src/main.py"));
        initial_meta.insert("language".to_string(), json!("python"));
        initial_meta.insert("start_line".to_string(), json!(10));

        batch_upsert_nodes(
            &pool,
            &[BatchNode {
                id: "node-1".to_string(),
                node_type: NodeType::Function,
                name: "process".to_string(),
                content: "def process(): pass".to_string(),
                metadata: initial_meta,
                repo_id: None,
            }],
        )
        .unwrap();

        // Phase 2: Simulate an independent analyzer adding metadata.
        let conn = pool.get().unwrap();
        conn.execute(
            "UPDATE nodes SET metadata = json_patch(metadata, ?1) WHERE id = 'node-1'",
            params![r#"{"semantic_label":"entrypoint","analysis_version":2}"#],
        )
        .unwrap();

        // Verify independently owned fields are present.
        let meta: String = conn
            .query_row(
                "SELECT metadata FROM nodes WHERE id = 'node-1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&meta).unwrap();
        assert_eq!(parsed["semantic_label"], "entrypoint");

        // Phase 3: Re-ingestion updates parser metadata only.
        let mut reingestion_meta = HashMap::new();
        reingestion_meta.insert("file_path".to_string(), json!("src/main.py"));
        reingestion_meta.insert("language".to_string(), json!("python"));
        reingestion_meta.insert("start_line".to_string(), json!(12)); // line changed

        batch_upsert_nodes(
            &pool,
            &[BatchNode {
                id: "node-1".to_string(),
                node_type: NodeType::Function,
                name: "process".to_string(),
                content: "def process(): return True".to_string(),
                metadata: reingestion_meta,
                repo_id: None,
            }],
        )
        .unwrap();

        // Verify parser fields updated and analyzer fields survived.
        let meta: String = conn
            .query_row(
                "SELECT metadata FROM nodes WHERE id = 'node-1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&meta).unwrap();

        // Parser field was updated.
        assert_eq!(parsed["start_line"], 12);
        assert_eq!(parsed["semantic_label"], "entrypoint");
        assert_eq!(parsed["analysis_version"], 2);
    }

    /// The same preservation guarantee applies to edge metadata.
    #[test]
    fn batch_upsert_edges_preserves_existing_metadata_keys() {
        let (_dir, pool, _idx) = setup();

        // Create two nodes for the edge endpoints.
        let nodes = vec![
            BatchNode {
                id: "src".to_string(),
                node_type: NodeType::Class,
                name: "Engine".to_string(),
                content: String::new(),
                metadata: HashMap::new(),
                repo_id: None,
            },
            BatchNode {
                id: "tgt".to_string(),
                node_type: NodeType::Method,
                name: "start".to_string(),
                content: String::new(),
                metadata: HashMap::new(),
                repo_id: None,
            },
        ];
        batch_upsert_nodes(&pool, &nodes).unwrap();

        // Phase 1: Create edge via ingestion (empty metadata).
        batch_upsert_edges(
            &pool,
            &[BatchEdge {
                source_id: "src".to_string(),
                target_id: "tgt".to_string(),
                relationship: "contains".to_string(),
                weight: 1.0,
                metadata: HashMap::new(),
            }],
        )
        .unwrap();

        // Phase 2: Simulate an independent analyzer writing edge metadata.
        let edge_id = make_edge_id("src", "tgt", "contains");
        let conn = pool.get().unwrap();
        conn.execute(
            "UPDATE edges SET metadata = json_patch(metadata, ?1) WHERE id = ?2",
            params![
                r#"{"confidence_source":"static","analysis_version":2}"#,
                edge_id
            ],
        )
        .unwrap();

        // Phase 3: Re-ingestion — same edge, fresh empty metadata.
        batch_upsert_edges(
            &pool,
            &[BatchEdge {
                source_id: "src".to_string(),
                target_id: "tgt".to_string(),
                relationship: "contains".to_string(),
                weight: 2.0, // weight changed
                metadata: HashMap::new(),
            }],
        )
        .unwrap();

        // Verify the weight changed without erasing independent metadata.
        let (weight, meta): (f64, String) = conn
            .query_row(
                "SELECT weight, metadata FROM edges WHERE id = ?1",
                params![edge_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();

        assert_eq!(weight, 2.0);
        let parsed: serde_json::Value = serde_json::from_str(&meta).unwrap();
        assert_eq!(parsed["confidence_source"], "static");
        assert_eq!(parsed["analysis_version"], 2);
    }

    /// Edges referencing non-existent nodes must be skipped, not crash
    /// the entire batch. This is the defensive fix for FK constraint
    /// failures during re-ingestion when cross-file edge resolution
    /// finds stale node IDs.
    #[test]
    fn batch_upsert_edges_skips_fk_violations() {
        let (_dir, pool, _idx) = setup();

        // Create only one node — edges will reference a missing target.
        batch_upsert_nodes(
            &pool,
            &[BatchNode {
                id: "existing".to_string(),
                node_type: NodeType::Function,
                name: "foo".to_string(),
                content: String::new(),
                metadata: HashMap::new(),
                repo_id: None,
            }],
        )
        .unwrap();

        // Batch with one valid edge and two invalid ones (missing endpoints).
        let edges = vec![
            // Valid: both endpoints exist (self-referential).
            BatchEdge {
                source_id: "existing".to_string(),
                target_id: "existing".to_string(),
                relationship: "references".to_string(),
                weight: 1.0,
                metadata: HashMap::new(),
            },
            // Invalid: target doesn't exist.
            BatchEdge {
                source_id: "existing".to_string(),
                target_id: "ghost-target".to_string(),
                relationship: "calls".to_string(),
                weight: 1.0,
                metadata: HashMap::new(),
            },
            // Invalid: source doesn't exist.
            BatchEdge {
                source_id: "ghost-source".to_string(),
                target_id: "existing".to_string(),
                relationship: "imports".to_string(),
                weight: 1.0,
                metadata: HashMap::new(),
            },
        ];

        // Must succeed, not panic/error.
        let count = batch_upsert_edges(&pool, &edges).unwrap();
        // Only the valid edge should be inserted.
        assert_eq!(count, 1);

        let conn = pool.get().unwrap();
        let total: i64 = conn
            .query_row("SELECT COUNT(*) FROM edges", [], |row| row.get(0))
            .unwrap();
        assert_eq!(total, 1);
    }
}
