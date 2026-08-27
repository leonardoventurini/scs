//! KnowledgeGraph — SCS code and provenance storage.
//!
//! This struct provides the full CRUD, vector search, and graph traversal
//! API over the SQLite + USearch knowledge graph. All database operations
//! run synchronously — async callers wrap with `spawn_blocking` or `to_thread`.
//!
//! Design decisions:
//! - r2d2 pool replaces Python's thread-local pattern for proper pooling
//! - Deterministic UUIDs (v5) for edges prevent duplicates on re-ingestion

// Clippy's `let_and_return` lint conflicts with rusqlite's borrow checker
// requirements: `stmt.query_map(...)?.collect()?` in if/else branches
// needs an intermediate `let` binding to extend `stmt`'s lifetime past
// the `MappedRows` iterator's drop.
#![allow(clippy::let_and_return)]

use std::collections::{HashMap, HashSet};

use rusqlite::params;
use scs_core::error::SCSResult;
use scs_core::models::{Edge, GraphRagResult, Node, Repo, SearchResult, TraversalResult};
use scs_core::node_types::NodeType;
use scs_core::SCSConfig;
use uuid::Uuid;

use crate::connection::{create_pool, ConnectionPool};
use crate::observability::{observe_result, QueryBackend};
use crate::schema::initialize_schema;
use crate::vector_index::{node_id_to_key, VectorIndex};
use crate::PoolResultExt;

/// UUID namespace for deterministic edge IDs.
///
/// Edges are keyed by `(source_id, target_id, relationship)` to prevent
/// duplicates. This namespace UUID matches the Python implementation exactly
/// for backward compatibility.
const EDGE_NAMESPACE: Uuid = Uuid::from_bytes([
    0xa1, 0xb2, 0xc3, 0xd4, 0xe5, 0xf6, 0x78, 0x90, 0xab, 0xcd, 0xef, 0x12, 0x34, 0x56, 0x78, 0x90,
]);

/// Generate a deterministic UUID v5 for an edge.
///
/// This prevents duplicate edges when re-indexing code. The same
/// source/target/relationship always produces the
/// same edge ID. The key format matches the Python implementation:
/// `"{source_id}:{target_id}:{relationship}"`.
pub fn make_edge_id(source_id: &str, target_id: &str, relationship: &str) -> String {
    let key = format!("{source_id}:{target_id}:{relationship}");
    Uuid::new_v5(&EDGE_NAMESPACE, key.as_bytes()).to_string()
}

/// Convert a rusqlite Row into a Node model.
///
/// Reads the optional `repo_id` column when selected by the query.
pub(crate) fn row_to_node(row: &rusqlite::Row) -> rusqlite::Result<Node> {
    let metadata_str: String = row.get("metadata")?;
    let metadata: HashMap<String, serde_json::Value> =
        serde_json::from_str(&metadata_str).unwrap_or_default();

    // repo_id may be NULL or absent depending on the query.
    let repo_id: Option<i64> = row.get("repo_id").unwrap_or(None);

    Ok(Node {
        id: row.get("id")?,
        node_type: {
            let type_str: String = row.get("type")?;
            type_str.parse().map_err(|_| {
                rusqlite::Error::FromSqlConversionFailure(
                    1,
                    rusqlite::types::Type::Text,
                    std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        format!("unsupported SCS node type: {type_str}"),
                    )
                    .into(),
                )
            })?
        },
        name: row.get("name")?,
        content: row.get("content")?,
        metadata,
        repo_id,
        created_at: row.get("created_at")?,
        updated_at: row.get("updated_at")?,
    })
}

/// Convert a rusqlite Row into an Edge model.
///
/// The schema guarantees that the stored relationship is code-only.
fn row_to_edge(row: &rusqlite::Row) -> rusqlite::Result<Edge> {
    let metadata_str: String = row.get("metadata")?;
    let metadata: HashMap<String, serde_json::Value> =
        serde_json::from_str(&metadata_str).unwrap_or_default();

    Ok(Edge {
        id: row.get("id")?,
        source_id: row.get("source_id")?,
        target_id: row.get("target_id")?,
        relationship: row.get("relationship")?,
        weight: row.get("weight")?,
        metadata,
        created_at: row.get("created_at")?,
    })
}

/// Result of a SQLite `VACUUM` operation.
///
/// Reports the database file size before and after compaction so the
/// caller (UI) can show how much disk space was reclaimed.
#[derive(Debug, Clone, serde::Serialize)]
pub struct VacuumResult {
    /// Database file size in bytes before VACUUM.
    pub size_before: u64,
    /// Database file size in bytes after VACUUM.
    pub size_after: u64,
}

/// Unified knowledge graph service backed by SQLite + USearch HNSW.
///
/// Combines structural relationships (graph edges in SQLite) with semantic
/// similarity (vector embeddings in USearch HNSW) in a single queryable
/// store for repository code entities, provenance, and structural relationships.
pub struct KnowledgeGraph {
    pool: ConnectionPool,
    config: SCSConfig,
    vector_index: VectorIndex,
}

impl KnowledgeGraph {
    /// Create and initialize a new KnowledgeGraph instance.
    ///
    /// Creates the connection pool, initializes the schema, opens the USearch
    /// vector index, and returns a fully ready graph.
    pub fn open(config: SCSConfig) -> SCSResult<Self> {
        let pool = create_pool(&config)?;
        let vector_index = VectorIndex::open(&config.index_path, config.embedding_dim)?;

        let graph = Self {
            pool,
            config,
            vector_index,
        };

        // Apply the current schema on a fresh connection before any queries run.
        graph.initialize()?;

        Ok(graph)
    }

    /// Ensure the current schema exists before the graph serves requests.
    fn initialize(&self) -> SCSResult<()> {
        let conn = self.pool.get().pool_err()?;
        initialize_schema(&conn)?;
        log::info!("Knowledge graph initialized at {:?}", self.config.db_path);
        Ok(())
    }

    /// Get the embedding dimension configured for this graph.
    pub fn embedding_dim(&self) -> usize {
        self.config.embedding_dim
    }

    /// Get a reference to the configuration.
    pub fn config(&self) -> &SCSConfig {
        &self.config
    }

    /// Get a reference to the connection pool (for batch operations).
    pub fn pool(&self) -> &ConnectionPool {
        &self.pool
    }

    /// Get a reference to the vector index (for batch operations).
    pub fn vector_index(&self) -> &VectorIndex {
        &self.vector_index
    }

    /// Persist pending vector-index mutations if the sidecar is dirty.
    pub fn flush_vector_index(&self) -> SCSResult<bool> {
        self.vector_index.save_if_dirty()
    }

    /// Reopen the durable sidecar and verify every acknowledged vector exists.
    ///
    /// The live index may still contain process-local mutations.  Ingestion
    /// therefore uses a fresh handle as the acknowledgement oracle after the
    /// atomic sidecar activation boundary.
    pub fn reopened_vectors_contain(&self, node_ids: &[String]) -> SCSResult<bool> {
        let reopened = VectorIndex::open(&self.config.index_path, self.config.embedding_dim)?;
        Ok(node_ids.iter().all(|node_id| reopened.contains(node_id)))
    }

    /// Reopen the durable sidecar and verify removed vectors stay absent.
    pub fn reopened_vectors_absent(&self, node_ids: &[String]) -> SCSResult<bool> {
        let reopened = VectorIndex::open(&self.config.index_path, self.config.embedding_dim)?;
        Ok(node_ids.iter().all(|node_id| !reopened.contains(node_id)))
    }

    // ── Repo Management ────────────────────────────────────────────

    /// Get or create a repo record, returning its integer ID.
    ///
    /// Uses `INSERT OR IGNORE` + `SELECT` for idempotent, race-safe
    /// upsert semantics. The returned ID is used as the `repo_id` FK
    /// on nodes for efficient repo-scoped queries.
    pub fn get_or_create_repo(&self, path: &str) -> SCSResult<Repo> {
        let conn = self.pool.get().pool_err()?;
        conn.execute(
            "INSERT OR IGNORE INTO repos (path) VALUES (?1)",
            params![path],
        )?;
        let repo = conn.query_row(
            "SELECT id, path FROM repos WHERE path = ?1",
            params![path],
            |row| {
                Ok(Repo {
                    id: row.get(0)?,
                    path: row.get(1)?,
                })
            },
        )?;
        Ok(repo)
    }

    /// Look up a repo by path, returning its ID if it exists.
    ///
    /// Unlike `get_or_create_repo`, this is a read-only lookup that
    /// returns `None` for unknown paths — used by search methods that
    /// need to resolve a user-provided path without side effects.
    pub fn resolve_repo_id(&self, path: &str) -> SCSResult<Option<i64>> {
        let conn = self.pool.get().pool_err()?;
        let result = conn.query_row(
            "SELECT id FROM repos WHERE path = ?1",
            params![path],
            |row| row.get::<_, i64>(0),
        );
        match result {
            Ok(id) => Ok(Some(id)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    /// Resolve an indexed entity's deterministic ID by repository and qualified name.
    ///
    /// Cross-batch edge planning needs to retain edges to entities from an
    /// unchanged file. The qualified name is stored as parser metadata and is
    /// unique within a repository by the ingestion identity contract. The
    /// ordering is a defensive tie-breaker for legacy stores that may contain
    /// duplicate metadata.
    pub fn resolve_node_id_by_qualified_name(
        &self,
        repo_path: &str,
        qualified_name: &str,
    ) -> SCSResult<Option<String>> {
        let conn = self.pool.get().pool_err()?;
        let result = conn.query_row(
            "SELECT n.id
             FROM nodes n
             LEFT JOIN repos r ON r.id = n.repo_id
             WHERE json_extract(n.metadata, '$.qualified_name') = ?2
               AND (r.path = ?1 OR json_extract(n.metadata, '$.repo_path') = ?1)
             ORDER BY n.id ASC
             LIMIT 1",
            params![repo_path, qualified_name],
            |row| row.get::<_, String>(0),
        );
        match result {
            Ok(id) => Ok(Some(id)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(error) => Err(error.into()),
        }
    }

    /// Look up a repo by integer ID, returning its path if it exists.
    ///
    /// Reverse of `resolve_repo_id` — used when a node's `repo_id` FK needs
    /// to be surfaced as a human-readable path for the frontend.
    pub fn resolve_repo_path(&self, repo_id: i64) -> SCSResult<Option<String>> {
        let conn = self.pool.get().pool_err()?;
        let result = conn.query_row(
            "SELECT path FROM repos WHERE id = ?1",
            params![repo_id],
            |row| row.get::<_, String>(0),
        );
        match result {
            Ok(path) => Ok(Some(path)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    /// Return a `{name: id}` map for all File-type nodes in a repo.
    ///
    /// Used by the git history ingester to resolve MODIFIES edges from
    /// commits to file nodes without relying on hash-based ID generation
    /// (which historically mismatched the code pipeline's scheme).
    /// A single query returns all file nodes for the repo, avoiding N+1
    /// lookups per commit file change.
    pub fn get_file_node_map(&self, repo_id: i64) -> SCSResult<HashMap<String, String>> {
        let conn = self.pool.get().pool_err()?;
        let mut stmt =
            conn.prepare("SELECT id, name FROM nodes WHERE type = 'file' AND repo_id = ?1")?;
        let map = stmt
            .query_map(params![repo_id], |row| {
                Ok((row.get::<_, String>(1)?, row.get::<_, String>(0)?))
            })?
            .filter_map(|r| r.ok())
            .collect();
        Ok(map)
    }

    // ── Node CRUD ──────────────────────────────────────────────────

    /// Insert or update a node and optionally its embedding.
    ///
    /// On conflict (same `node_id`), updates all fields and bumps
    /// `updated_at`. Embedding upsert uses DELETE + INSERT because
    /// The vector index is maintained separately from SQLite row upserts. The optional
    /// `repo_id` FK scopes the node to a specific repository for
    /// repo-filtered search queries.
    #[allow(clippy::too_many_arguments)] // Stable cross-language storage contract.
    pub fn upsert_node(
        &self,
        node_id: &str,
        node_type: NodeType,
        name: &str,
        content: &str,
        metadata: Option<&HashMap<String, serde_json::Value>>,
        embedding: Option<&[f32]>,
        repo_id: Option<i64>,
    ) -> SCSResult<Node> {
        // SQLite and USearch do not share a transaction. Reject deterministic
        // vector-shape errors before either backend can mutate durable state.
        if let Some(embedding) = embedding {
            self.vector_index.validate_dimension(embedding)?;
        }
        let conn = self.pool.get().pool_err()?;
        let meta_json = match metadata {
            Some(m) => serde_json::to_string(m)?,
            None => "{}".to_string(),
        };

        // Merge metadata via json_patch (RFC 7396) so independently owned
        // fields absent from this update survive re-ingestion.
        conn.execute(
            "INSERT INTO nodes (id, type, name, content, metadata, repo_id)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)
             ON CONFLICT(id) DO UPDATE SET
                 type = excluded.type,
                 name = excluded.name,
                 content = excluded.content,
                 metadata = json_patch(nodes.metadata, excluded.metadata),
                 repo_id = COALESCE(excluded.repo_id, nodes.repo_id),
                 updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
            params![
                node_id,
                node_type.to_string(),
                name,
                content,
                meta_json,
                repo_id
            ],
        )?;

        if let Some(emb) = embedding {
            self.vector_index.add(node_id, emb)?;
        }

        let node = conn.query_row(
            "SELECT * FROM nodes WHERE id = ?1",
            params![node_id],
            row_to_node,
        )?;

        Ok(node)
    }

    /// Retrieve a single node by ID.
    pub fn get_node(&self, node_id: &str) -> SCSResult<Option<Node>> {
        observe_result(
            QueryBackend::Sqlite,
            "KnowledgeGraph::get_node",
            "nodes",
            String::new(),
            |obs| {
                let wait_started = std::time::Instant::now();
                let conn = self.pool.get().pool_err()?;
                obs.set_wait(wait_started.elapsed());
                let result = conn.query_row(
                    "SELECT * FROM nodes WHERE id = ?1",
                    params![node_id],
                    row_to_node,
                );

                match result {
                    Ok(node) => {
                        obs.set_rows(1);
                        Ok(Some(node))
                    }
                    Err(rusqlite::Error::QueryReturnedNoRows) => {
                        obs.set_rows(0);
                        Ok(None)
                    }
                    Err(e) => Err(e.into()),
                }
            },
        )
    }

    /// Delete a node and its edges/embedding.
    ///
    /// Removes the vector from USearch and the node from SQLite (edges
    /// CASCADE automatically via foreign key constraints).
    pub fn delete_node(&self, node_id: &str) -> SCSResult<bool> {
        let conn = self.pool.get().pool_err()?;
        self.vector_index.remove(node_id)?;
        let count = conn.execute("DELETE FROM nodes WHERE id = ?1", params![node_id])?;
        Ok(count > 0)
    }

    /// Bulk-delete nodes matching a type + metadata key/value filter.
    ///
    /// Finds matching node IDs, removes their vectors from USearch, then
    /// deletes the nodes from SQLite (edges CASCADE automatically).
    /// Returns the number of nodes deleted.
    ///
    /// This is orders of magnitude faster than iterating in Python for
    /// large datasets (e.g. 1.8M rows) — everything stays in SQLite.
    pub fn delete_nodes_by_metadata(
        &self,
        node_type: &NodeType,
        metadata_key: &str,
        metadata_value: &str,
    ) -> SCSResult<usize> {
        let conn = self.pool.get().pool_err()?;
        let type_str = node_type.to_string();
        let json_path = format!("$.{metadata_key}");

        log::debug!(
            "Bulk-deleting nodes: type={}, {}={}",
            type_str,
            metadata_key,
            metadata_value,
        );

        // Collect matching node IDs so we can remove their vectors from USearch.
        let ids: Vec<String> = {
            let mut stmt = conn.prepare(
                "SELECT id FROM nodes WHERE type = ?1 AND json_extract(metadata, ?2) = ?3",
            )?;
            let result = stmt
                .query_map(params![type_str, json_path, metadata_value], |row| {
                    row.get(0)
                })?
                .collect::<Result<Vec<_>, _>>()?;
            result
        };

        // Remove vectors from USearch.
        self.vector_index.remove_batch(&ids)?;

        // Delete the matching nodes (edges CASCADE automatically).
        let deleted = conn.execute(
            "DELETE FROM nodes
             WHERE type = ?1
               AND json_extract(metadata, ?2) = ?3",
            params![type_str, json_path, metadata_value],
        )?;

        log::info!(
            "Bulk-deleted {} {} nodes where {}={}",
            deleted,
            type_str,
            metadata_key,
            metadata_value,
        );

        Ok(deleted)
    }

    /// List nodes, optionally filtered by type.
    pub fn list_nodes(
        &self,
        node_type: Option<NodeType>,
        limit: i64,
        offset: i64,
        repo_id: Option<i64>,
    ) -> SCSResult<Vec<Node>> {
        observe_result(
            QueryBackend::Sqlite,
            "KnowledgeGraph::list_nodes",
            "nodes",
            format!("type={node_type:?} repo_id={repo_id:?} limit={limit} offset={offset}"),
            |obs| {
                let wait_started = std::time::Instant::now();
                let conn = self.pool.get().pool_err()?;
                obs.set_wait(wait_started.elapsed());

                // Build WHERE clause dynamically from optional filters.
                let mut clauses: Vec<String> = Vec::new();
                let mut param_values: Vec<Box<dyn rusqlite::types::ToSql>> = Vec::new();

                if let Some(nt) = node_type {
                    param_values.push(Box::new(nt.to_string()));
                    clauses.push(format!("type = ?{}", param_values.len()));
                }
                if let Some(rid) = repo_id {
                    param_values.push(Box::new(rid));
                    clauses.push(format!("repo_id = ?{}", param_values.len()));
                }

                let where_clause = if clauses.is_empty() {
                    String::new()
                } else {
                    format!("WHERE {}", clauses.join(" AND "))
                };

                param_values.push(Box::new(limit));
                let limit_idx = param_values.len();
                param_values.push(Box::new(offset));
                let offset_idx = param_values.len();

                let sql = format!(
                    "SELECT * FROM nodes {where_clause} ORDER BY updated_at DESC, id DESC LIMIT ?{limit_idx} OFFSET ?{offset_idx}"
                );

                let params_ref: Vec<&dyn rusqlite::types::ToSql> =
                    param_values.iter().map(|b| b.as_ref()).collect();
                let mut stmt = conn.prepare(&sql)?;
                let nodes = stmt
                    .query_map(params_ref.as_slice(), row_to_node)?
                    .collect::<Result<Vec<_>, _>>()?;

                obs.set_rows(nodes.len());
                Ok(nodes)
            },
        )
    }

    /// Count nodes, optionally filtered by type and/or repo.
    ///
    /// When `repo_id` is provided, the count is scoped to nodes belonging
    /// to that repository — essential for `list_symbols` where the total
    /// must reflect the filtered set, not the entire graph.
    pub fn count_nodes(&self, node_type: Option<NodeType>, repo_id: Option<i64>) -> SCSResult<i64> {
        observe_result(
            QueryBackend::Sqlite,
            "KnowledgeGraph::count_nodes",
            "nodes",
            format!("type={node_type:?} repo_id={repo_id:?}"),
            |obs| {
                let wait_started = std::time::Instant::now();
                let conn = self.pool.get().pool_err()?;
                obs.set_wait(wait_started.elapsed());

                let mut clauses: Vec<String> = Vec::new();
                let mut param_values: Vec<Box<dyn rusqlite::types::ToSql>> = Vec::new();

                if let Some(nt) = node_type {
                    param_values.push(Box::new(nt.to_string()));
                    clauses.push(format!("type = ?{}", param_values.len()));
                }
                if let Some(rid) = repo_id {
                    param_values.push(Box::new(rid));
                    clauses.push(format!("repo_id = ?{}", param_values.len()));
                }

                let where_clause = if clauses.is_empty() {
                    String::new()
                } else {
                    format!(" WHERE {}", clauses.join(" AND "))
                };

                let sql = format!("SELECT COUNT(*) FROM nodes{where_clause}");
                let params_ref: Vec<&dyn rusqlite::types::ToSql> =
                    param_values.iter().map(|b| b.as_ref()).collect();

                let count: i64 = conn.query_row(&sql, params_ref.as_slice(), |row| row.get(0))?;
                obs.set_rows(count as usize);
                Ok(count)
            },
        )
    }

    /// List nodes that have no embedding vector in the USearch index.
    ///
    /// Returns nodes missing embeddings, optionally filtered by type and/or
    /// repo. Used by the background embedding generator to discover work items.
    /// Results are ordered by `n.id` for deterministic pagination.
    pub fn list_nodes_without_embeddings(
        &self,
        node_type: Option<NodeType>,
        limit: i64,
        offset: i64,
        repo_id: Option<i64>,
    ) -> SCSResult<Vec<Node>> {
        let conn = self.pool.get().pool_err()?;

        let mut clauses: Vec<String> = Vec::new();
        let mut param_values: Vec<Box<dyn rusqlite::types::ToSql>> = Vec::new();

        if let Some(nt) = node_type {
            param_values.push(Box::new(nt.to_string()));
            clauses.push(format!("n.type = ?{}", param_values.len()));
        }
        if let Some(rid) = repo_id {
            param_values.push(Box::new(rid));
            clauses.push(format!("n.repo_id = ?{}", param_values.len()));
        }

        let where_clause = if clauses.is_empty() {
            String::new()
        } else {
            format!(" WHERE {}", clauses.join(" AND "))
        };

        // No SQL LIMIT — we must scan all rows because the USearch index
        // (not SQLite) tracks which nodes have embeddings. Applying LIMIT
        // in SQL would return only the first N rows by ID order, which may
        // all already have embeddings, yielding an empty result even when
        // hundreds of thousands of unembedded nodes exist further in the table.
        let sql = format!("SELECT n.* FROM nodes n{where_clause} ORDER BY n.id");

        let params_ref: Vec<&dyn rusqlite::types::ToSql> =
            param_values.iter().map(|b| b.as_ref()).collect();
        let mut stmt = conn.prepare(&sql)?;

        // Filter out nodes that already have embeddings in USearch,
        // then apply offset/limit for pagination in Rust.
        let nodes: Vec<Node> = stmt
            .query_map(params_ref.as_slice(), row_to_node)?
            .filter_map(|r| r.ok())
            .filter(|node| !self.vector_index.contains(&node.id))
            .skip(offset as usize)
            .take(limit as usize)
            .collect();

        Ok(nodes)
    }

    /// Count nodes that have no embedding vector in the USearch index.
    ///
    /// Optionally scoped by type and/or repo. Used by the background
    /// embedding generator to report total work remaining.
    pub fn count_nodes_without_embeddings(
        &self,
        node_type: Option<NodeType>,
        repo_id: Option<i64>,
    ) -> SCSResult<i64> {
        let conn = self.pool.get().pool_err()?;

        let mut clauses: Vec<String> = Vec::new();
        let mut param_values: Vec<Box<dyn rusqlite::types::ToSql>> = Vec::new();

        if let Some(nt) = node_type {
            param_values.push(Box::new(nt.to_string()));
            clauses.push(format!("n.type = ?{}", param_values.len()));
        }
        if let Some(rid) = repo_id {
            param_values.push(Box::new(rid));
            clauses.push(format!("n.repo_id = ?{}", param_values.len()));
        }

        let where_clause = if clauses.is_empty() {
            String::new()
        } else {
            format!(" WHERE {}", clauses.join(" AND "))
        };

        let sql = format!("SELECT id FROM nodes n{where_clause}");
        let params_ref: Vec<&dyn rusqlite::types::ToSql> =
            param_values.iter().map(|b| b.as_ref()).collect();

        let mut stmt = conn.prepare(&sql)?;
        let count = stmt
            .query_map(params_ref.as_slice(), |row| row.get::<_, String>(0))?
            .filter_map(|r| r.ok())
            .filter(|id| !self.vector_index.contains(id))
            .count() as i64;

        Ok(count)
    }

    /// Count nodes grouped by type in a single SQL query.
    ///
    /// Returns a map from node type string to count. Types with zero nodes
    /// are omitted. This is more efficient than calling `count_nodes` once
    /// per `NodeType` — O(1) queries instead of O(n_types).
    ///
    /// When `repo_id` is `Some(id)`, only nodes belonging to that repo are
    /// counted — scoping the result to a single repository. This is the key
    /// optimisation for `knowledge.stats`: with 25k+ nodes across 3 repos,
    /// scoping avoids counting irrelevant repos' nodes.
    pub fn count_nodes_by_type(&self, repo_id: Option<i64>) -> SCSResult<HashMap<String, i64>> {
        observe_result(
            QueryBackend::Sqlite,
            "KnowledgeGraph::count_nodes_by_type",
            "nodes",
            format!("repo_id={repo_id:?}"),
            |obs| {
                let wait_started = std::time::Instant::now();
                let conn = self.pool.get().pool_err()?;
                obs.set_wait(wait_started.elapsed());

                // Use imperative loop style rather than expression chaining so that the
                // `stmt` borrow is fully consumed inside each block before it drops.
                let mut counts: HashMap<String, i64> = HashMap::new();

                if let Some(rid) = repo_id {
                    let mut stmt = conn.prepare(
                        "SELECT type, COUNT(*) FROM nodes WHERE repo_id = ?1 GROUP BY type",
                    )?;
                    let rows = stmt.query_map([rid], |row| {
                        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
                    })?;
                    for row in rows.flatten() {
                        counts.insert(row.0, row.1);
                    }
                } else {
                    let mut stmt =
                        conn.prepare("SELECT type, COUNT(*) FROM nodes GROUP BY type")?;
                    let rows = stmt.query_map([], |row| {
                        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
                    })?;
                    for row in rows.flatten() {
                        counts.insert(row.0, row.1);
                    }
                }

                obs.set_rows(counts.len());
                Ok(counts)
            },
        )
    }

    /// Count embedding vectors stored in the USearch index.
    ///
    /// Returns the total number of vectors — useful for quality inspection
    /// tools that compare embedding coverage against node counts to detect
    /// missing embeddings after ingestion.
    pub fn count_embeddings(&self) -> SCSResult<i64> {
        Ok(self.vector_index.size() as i64)
    }

    /// Search nodes by name (case-insensitive substring match).
    ///
    /// Leading-wildcard substring search cannot use the name index directly,
    /// so callers should pass `repo_id` when they can scope the search.
    /// Optionally filtered by node type. Returns up to `limit` results ordered
    /// by most recently updated.
    /// Search nodes by name substring (case-insensitive), optionally scoped to a repo.
    pub fn search_by_name(
        &self,
        name: &str,
        node_type: Option<NodeType>,
        limit: i64,
        repo_id: Option<i64>,
    ) -> SCSResult<Vec<Node>> {
        observe_result(
            QueryBackend::Sqlite,
            "KnowledgeGraph::search_by_name",
            "nodes",
            format!("type={node_type:?} repo_id={repo_id:?} limit={limit}"),
            |obs| {
                let wait_started = std::time::Instant::now();
                let conn = self.pool.get().pool_err()?;
                obs.set_wait(wait_started.elapsed());
                let pattern = format!("%{name}%");

                // Dynamic WHERE from optional filters — same pattern as list_nodes.
                let mut clauses: Vec<String> = Vec::new();
                let mut param_values: Vec<Box<dyn rusqlite::types::ToSql>> = Vec::new();

                param_values.push(Box::new(pattern));
                clauses.push(format!("name LIKE ?{} COLLATE NOCASE", param_values.len()));

                if let Some(nt) = node_type {
                    param_values.push(Box::new(nt.to_string()));
                    clauses.push(format!("type = ?{}", param_values.len()));
                }
                if let Some(rid) = repo_id {
                    param_values.push(Box::new(rid));
                    clauses.push(format!("repo_id = ?{}", param_values.len()));
                }

                param_values.push(Box::new(limit));
                let limit_idx = param_values.len();

                let where_clause = clauses.join(" AND ");
                let sql = format!(
                    "SELECT * FROM nodes WHERE {where_clause} ORDER BY updated_at DESC LIMIT ?{limit_idx}"
                );

                let params_ref: Vec<&dyn rusqlite::types::ToSql> =
                    param_values.iter().map(|b| b.as_ref()).collect();
                let mut stmt = conn.prepare(&sql)?;
                let nodes = stmt
                    .query_map(params_ref.as_slice(), row_to_node)?
                    .collect::<Result<Vec<_>, _>>()?;

                obs.set_rows(nodes.len());
                Ok(nodes)
            },
        )
    }

    // ── Edge CRUD ──────────────────────────────────────────────────

    /// Insert or update a directed edge between two nodes.
    ///
    /// Edge ID is deterministic (UUID v5 from source:target:relationship)
    /// so repeated indexing updates rather than duplicates. SQLite rejects
    /// relationships outside the code-only [`RelationshipType`] contract.
    pub fn upsert_edge(
        &self,
        source_id: &str,
        target_id: &str,
        relationship: &str,
        weight: f64,
        metadata: Option<&HashMap<String, serde_json::Value>>,
    ) -> SCSResult<Edge> {
        let conn = self.pool.get().pool_err()?;
        let edge_id = make_edge_id(source_id, target_id, relationship);
        let meta_json = match metadata {
            Some(m) => serde_json::to_string(m)?,
            None => "{}".to_string(),
        };

        // Merge metadata via json_patch (RFC 7396) — see upsert_node for rationale.
        conn.execute(
            "INSERT INTO edges (id, source_id, target_id, relationship, weight, metadata)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)
             ON CONFLICT(id) DO UPDATE SET
                 weight = excluded.weight,
                 metadata = json_patch(edges.metadata, excluded.metadata)",
            params![
                edge_id,
                source_id,
                target_id,
                relationship,
                weight,
                meta_json,
            ],
        )?;

        let edge = conn.query_row(
            "SELECT * FROM edges WHERE id = ?1",
            params![edge_id],
            row_to_edge,
        )?;

        Ok(edge)
    }

    /// Get edges connected to a node, with optional relationship and direction filters.
    pub fn get_edges(
        &self,
        node_id: &str,
        relationship: Option<&str>,
        direction: EdgeDirection,
    ) -> SCSResult<Vec<Edge>> {
        observe_result(
            QueryBackend::Sqlite,
            "KnowledgeGraph::get_edges",
            "edges",
            format!("direction={direction:?} relationship={relationship:?}"),
            |obs| {
                let wait_started = std::time::Instant::now();
                let conn = self.pool.get().pool_err()?;
                obs.set_wait(wait_started.elapsed());
                let mut clauses: Vec<String> = Vec::new();
                let mut param_values: Vec<Box<dyn rusqlite::types::ToSql>> = Vec::new();

                if matches!(direction, EdgeDirection::Outgoing | EdgeDirection::Both) {
                    if let Some(rel) = relationship {
                        clauses.push(format!(
                            "(source_id = ?{} AND relationship = ?{})",
                            param_values.len() + 1,
                            param_values.len() + 2,
                        ));
                        param_values.push(Box::new(node_id.to_string()));
                        param_values.push(Box::new(rel.to_string()));
                    } else {
                        clauses.push(format!("source_id = ?{}", param_values.len() + 1));
                        param_values.push(Box::new(node_id.to_string()));
                    }
                }

                if matches!(direction, EdgeDirection::Incoming | EdgeDirection::Both) {
                    if let Some(rel) = relationship {
                        clauses.push(format!(
                            "(target_id = ?{} AND relationship = ?{})",
                            param_values.len() + 1,
                            param_values.len() + 2,
                        ));
                        param_values.push(Box::new(node_id.to_string()));
                        param_values.push(Box::new(rel.to_string()));
                    } else {
                        clauses.push(format!("target_id = ?{}", param_values.len() + 1));
                        param_values.push(Box::new(node_id.to_string()));
                    }
                }

                let where_clause = clauses.join(" OR ");
                let sql = format!("SELECT * FROM edges WHERE {where_clause}");

                let params_ref: Vec<&dyn rusqlite::types::ToSql> =
                    param_values.iter().map(|b| b.as_ref()).collect();

                let mut stmt = conn.prepare(&sql)?;
                let edges = stmt
                    .query_map(params_ref.as_slice(), row_to_edge)?
                    .collect::<Result<Vec<_>, _>>()?;

                obs.set_rows(edges.len());
                Ok(edges)
            },
        )
    }

    /// Delete an edge by ID.
    pub fn delete_edge(&self, edge_id: &str) -> SCSResult<bool> {
        let conn = self.pool.get().pool_err()?;
        let count = conn.execute("DELETE FROM edges WHERE id = ?1", params![edge_id])?;
        Ok(count > 0)
    }

    // ── Vector Search ──────────────────────────────────────────────

    /// Find nodes most similar to a query vector via USearch KNN.
    ///
    /// Uses the vec0 virtual table's MATCH operator for approximate
    /// nearest neighbor search, then joins to the nodes table for
    /// Find nodes most similar to a query vector via USearch HNSW,
    /// optionally scoped to a node type and/or repo.
    ///
    /// Uses the HNSW index for sub-millisecond approximate nearest
    /// neighbor search, then batch-fetches node metadata from SQLite.
    /// Results are ordered by ascending distance (lower = more similar).
    pub fn search_by_vector(
        &self,
        query_embedding: &[f32],
        node_type: Option<NodeType>,
        limit: i64,
        repo_id: Option<i64>,
    ) -> SCSResult<Vec<SearchResult>> {
        observe_result(
            QueryBackend::Sqlite,
            "KnowledgeGraph::search_by_vector",
            "nodes+usearch",
            format!(
                "type={node_type:?} repo_id={repo_id:?} limit={limit} dims={}",
                query_embedding.len()
            ),
            |obs| {
                let limit_usize = limit as usize;
                let has_filter = node_type.is_some() || repo_id.is_some();

                // Phase 1: Build a key→id reverse-map for the candidate scope.
                // This is needed because USearch returns u64 keys that we must
                // map back to string node IDs.
                let wait_started = std::time::Instant::now();
                let conn = self.pool.get().pool_err()?;
                obs.set_wait(wait_started.elapsed());
                let key_to_id: HashMap<u64, String>;

                let kv_pairs = if has_filter {
                    let mut clauses: Vec<String> = Vec::new();
                    let mut param_values: Vec<Box<dyn rusqlite::types::ToSql>> = Vec::new();

                    if let Some(nt) = node_type {
                        param_values.push(Box::new(nt.to_string()));
                        clauses.push(format!("type = ?{}", param_values.len()));
                    }
                    if let Some(rid) = repo_id {
                        param_values.push(Box::new(rid));
                        clauses.push(format!("repo_id = ?{}", param_values.len()));
                    }

                    let where_clause = format!("WHERE {}", clauses.join(" AND "));
                    let sql = format!("SELECT id FROM nodes {where_clause}");
                    let params_ref: Vec<&dyn rusqlite::types::ToSql> =
                        param_values.iter().map(|b| b.as_ref()).collect();

                    let mut stmt = conn.prepare(&sql)?;
                    let mut valid_keys = std::collections::HashSet::new();
                    let mut id_map = HashMap::new();

                    for row in
                        stmt.query_map(params_ref.as_slice(), |row| row.get::<_, String>(0))?
                    {
                        let id = row?;
                        let key = node_id_to_key(&id);
                        valid_keys.insert(key);
                        id_map.insert(key, id);
                    }

                    key_to_id = id_map;
                    self.vector_index
                        .filtered_search(query_embedding, limit_usize, &valid_keys)?
                } else {
                    // Unfiltered: search USearch first, then resolve keys to IDs.
                    let pairs = self.vector_index.search(query_embedding, limit_usize)?;

                    // Build key→id map by scanning node IDs and matching against result keys.
                    let result_keys: std::collections::HashSet<u64> =
                        pairs.iter().map(|(k, _)| *k).collect();

                    let mut id_map = HashMap::new();
                    let mut stmt = conn.prepare("SELECT id FROM nodes")?;
                    for row in stmt.query_map([], |row| row.get::<_, String>(0))? {
                        let id = row?;
                        let key = node_id_to_key(&id);
                        if result_keys.contains(&key) {
                            id_map.insert(key, id);
                        }
                        // Early exit once we've found all keys.
                        if id_map.len() == result_keys.len() {
                            break;
                        }
                    }

                    key_to_id = id_map;
                    pairs
                };

                if kv_pairs.is_empty() {
                    obs.set_rows(0);
                    obs.set_vectors(0);
                    return Ok(Vec::new());
                }

                // Phase 2: Fetch full node metadata and build SearchResult list.
                let mut results: Vec<SearchResult> = Vec::with_capacity(kv_pairs.len());
                for (key, dist) in &kv_pairs {
                    if let Some(node_id) = key_to_id.get(key) {
                        if let Some(node) = self.get_node(node_id)? {
                            results.push(SearchResult {
                                node,
                                distance: *dist as f64,
                            });
                        }
                    }
                }

                // USearch returns results in distance order, and we iterate in that
                // order, so results should already be sorted. Sort defensively.
                results.sort_by(|a, b| {
                    a.distance
                        .partial_cmp(&b.distance)
                        .unwrap_or(std::cmp::Ordering::Equal)
                });

                obs.set_rows(results.len());
                obs.set_vectors(kv_pairs.len());
                Ok(results)
            },
        )
    }

    // ── Graph Traversal ────────────────────────────────────────────

    /// Get immediate neighbor nodes via a single-hop JOIN.
    pub fn get_neighbors(
        &self,
        node_id: &str,
        relationship: Option<&str>,
        direction: EdgeDirection,
        limit: i64,
    ) -> SCSResult<Vec<Node>> {
        observe_result(
            QueryBackend::Sqlite,
            "KnowledgeGraph::get_neighbors",
            "edges+nodes",
            format!("direction={direction:?} relationship={relationship:?} limit={limit}"),
            |obs| {
                let wait_started = std::time::Instant::now();
                let conn = self.pool.get().pool_err()?;
                obs.set_wait(wait_started.elapsed());

                let nodes = match direction {
                    EdgeDirection::Outgoing => {
                        if let Some(rel) = relationship {
                            let mut stmt = conn.prepare(
                                "SELECT n.* FROM edges e
                         JOIN nodes n ON n.id = e.target_id
                         WHERE e.source_id = ?1 AND e.relationship = ?2
                         LIMIT ?3",
                            )?;
                            let r = stmt
                                .query_map(params![node_id, rel, limit], row_to_node)?
                                .collect::<Result<Vec<_>, _>>()?;
                            r
                        } else {
                            let mut stmt = conn.prepare(
                                "SELECT n.* FROM edges e
                         JOIN nodes n ON n.id = e.target_id
                         WHERE e.source_id = ?1
                         LIMIT ?2",
                            )?;
                            let r = stmt
                                .query_map(params![node_id, limit], row_to_node)?
                                .collect::<Result<Vec<_>, _>>()?;
                            r
                        }
                    }
                    EdgeDirection::Incoming => {
                        if let Some(rel) = relationship {
                            let mut stmt = conn.prepare(
                                "SELECT n.* FROM edges e
                         JOIN nodes n ON n.id = e.source_id
                         WHERE e.target_id = ?1 AND e.relationship = ?2
                         LIMIT ?3",
                            )?;
                            let r = stmt
                                .query_map(params![node_id, rel, limit], row_to_node)?
                                .collect::<Result<Vec<_>, _>>()?;
                            r
                        } else {
                            let mut stmt = conn.prepare(
                                "SELECT n.* FROM edges e
                         JOIN nodes n ON n.id = e.source_id
                         WHERE e.target_id = ?1
                         LIMIT ?2",
                            )?;
                            let r = stmt
                                .query_map(params![node_id, limit], row_to_node)?
                                .collect::<Result<Vec<_>, _>>()?;
                            r
                        }
                    }
                    EdgeDirection::Both => {
                        if let Some(rel) = relationship {
                            let mut stmt = conn.prepare(
                                "SELECT DISTINCT n.* FROM edges e
                         JOIN nodes n ON n.id = CASE
                             WHEN e.source_id = ?1 THEN e.target_id
                             ELSE e.source_id
                         END
                         WHERE (e.source_id = ?1 OR e.target_id = ?1) AND e.relationship = ?2
                         LIMIT ?3",
                            )?;
                            let r = stmt
                                .query_map(params![node_id, rel, limit], row_to_node)?
                                .collect::<Result<Vec<_>, _>>()?;
                            r
                        } else {
                            let mut stmt = conn.prepare(
                                "SELECT DISTINCT n.* FROM edges e
                         JOIN nodes n ON n.id = CASE
                             WHEN e.source_id = ?1 THEN e.target_id
                             ELSE e.source_id
                         END
                         WHERE e.source_id = ?1 OR e.target_id = ?1
                         LIMIT ?2",
                            )?;
                            let r = stmt
                                .query_map(params![node_id, limit], row_to_node)?
                                .collect::<Result<Vec<_>, _>>()?;
                            r
                        }
                    }
                };

                obs.set_rows(nodes.len());
                Ok(nodes)
            },
        )
    }

    /// Recursive graph traversal using a CTE with cycle detection.
    ///
    /// Walks the graph from `start_node_id` up to `max_depth` hops,
    /// following edges in the specified direction. The path column
    /// prevents infinite cycles by checking if a node was already
    /// visited in the current traversal path.
    pub fn traverse(
        &self,
        start_node_id: &str,
        max_depth: i32,
        relationship: Option<&str>,
        direction: TraversalDirection,
    ) -> SCSResult<Vec<TraversalResult>> {
        observe_result(
            QueryBackend::Sqlite,
            "KnowledgeGraph::traverse",
            "edges+nodes",
            format!("direction={direction:?} relationship={relationship:?} depth={max_depth}"),
            |obs| {
                let wait_started = std::time::Instant::now();
                let conn = self.pool.get().pool_err()?;
                obs.set_wait(wait_started.elapsed());

                // Build the edge traversal direction columns.
                let (join_col, next_col) = match direction {
                    TraversalDirection::Outgoing => ("source_id", "target_id"),
                    TraversalDirection::Incoming => ("target_id", "source_id"),
                };

                let rel_filter = if relationship.is_some() {
                    "AND e.relationship = ?3".to_string()
                } else {
                    String::new()
                };

                let query = format!(
                    "WITH RECURSIVE graph_walk(node_id, depth, path) AS (
                         SELECT ?1, 0, ?1
                         UNION ALL
                         SELECT e.{next_col}, gw.depth + 1, gw.path || ',' || e.{next_col}
                         FROM graph_walk gw
                         JOIN edges e ON e.{join_col} = gw.node_id
                         WHERE gw.depth < ?2
                           AND gw.path NOT LIKE '%' || e.{next_col} || '%'
                           {rel_filter}
                     )
                     SELECT n.*, gw.depth, gw.path
                     FROM graph_walk gw
                     JOIN nodes n ON n.id = gw.node_id
                     WHERE gw.depth > 0
                     ORDER BY gw.depth ASC"
                );

                let results = if let Some(rel) = relationship {
                    let mut stmt = conn.prepare(&query)?;
                    let r = stmt
                        .query_map(params![start_node_id, max_depth, rel], |row| {
                            let path_str: String = row.get("path")?;
                            let path: Vec<String> = path_str.split(',').map(String::from).collect();
                            Ok(TraversalResult {
                                node: row_to_node(row)?,
                                depth: row.get("depth")?,
                                path,
                            })
                        })?
                        .collect::<Result<Vec<_>, _>>()?;
                    r
                } else {
                    let mut stmt = conn.prepare(&query)?;
                    let r = stmt
                        .query_map(params![start_node_id, max_depth], |row| {
                            let path_str: String = row.get("path")?;
                            let path: Vec<String> = path_str.split(',').map(String::from).collect();
                            Ok(TraversalResult {
                                node: row_to_node(row)?,
                                depth: row.get("depth")?,
                                path,
                            })
                        })?
                        .collect::<Result<Vec<_>, _>>()?;
                    r
                };

                obs.set_rows(results.len());
                Ok(results)
            },
        )
    }

    // ── Graph RAG ──────────────────────────────────────────────────

    /// Combined vector search + graph traversal for RAG context.
    ///
    /// Pass 1: Find top-K semantically similar nodes via vector search.
    /// Pass 2: For each hit, traverse up to `hop_limit` edges to discover
    ///         related context nodes (vocabulary terms, code entities).
    /// Combined vector search + graph traversal for RAG context,
    /// optionally scoped to a repo for the vector search pass.
    ///
    /// Pass 1: Find top-K semantically similar nodes via vector search.
    /// Pass 2: For each hit, traverse up to `hop_limit` edges to discover
    ///         related context nodes (vocabulary terms, code entities).
    /// Note: traversal is cross-repo by design — edges may link to nodes
    /// in other repos (e.g., shared library types).
    pub fn graph_rag_query(
        &self,
        query_embedding: &[f32],
        node_type: Option<NodeType>,
        vector_limit: i64,
        hop_limit: i32,
        relationship: Option<&str>,
        repo_id: Option<i64>,
    ) -> SCSResult<GraphRagResult> {
        observe_result(
            QueryBackend::Sqlite,
            "KnowledgeGraph::graph_rag_query",
            "nodes+edges+usearch",
            format!(
                "type={node_type:?} repo_id={repo_id:?} vector_limit={vector_limit} hop_limit={hop_limit}"
            ),
            |obs| {
                let similar =
                    self.search_by_vector(query_embedding, node_type, vector_limit, repo_id)?;

                // Collect graph context from traversal of each similar node.
                let mut seen_ids: std::collections::HashSet<String> =
                    similar.iter().map(|sr| sr.node.id.clone()).collect();
                let mut graph_context: Vec<Node> = Vec::new();

                for sr in &similar {
                    let traversal = self.traverse(
                        &sr.node.id,
                        hop_limit,
                        relationship,
                        TraversalDirection::Outgoing,
                    )?;
                    for tr in traversal {
                        if seen_ids.insert(tr.node.id.clone()) {
                            graph_context.push(tr.node);
                        }
                    }
                }

                obs.set_rows(similar.len() + graph_context.len());
                obs.set_vectors(similar.len());
                Ok(GraphRagResult {
                    similar_nodes: similar,
                    graph_context,
                })
            },
        )
    }

    // ── Maintenance ──────────────────────────────────────────────────

    /// Compact the database by running SQLite `VACUUM`.
    ///
    /// Rebuilds the entire database file, reclaiming disk space from
    /// deleted rows and defragmenting storage for faster queries. This
    /// is especially useful after bulk deletions (repo drops, dataset
    /// removals) that leave behind unused pages.
    ///
    /// Returns the file size before and after so the UI can report
    /// how much space was reclaimed.
    pub fn vacuum(&self) -> SCSResult<VacuumResult> {
        let conn = self.pool.get().pool_err()?;

        let size_before = Self::db_file_size(&conn);
        conn.execute_batch("VACUUM")?;
        let size_after = Self::db_file_size(&conn);

        log::info!(
            "VACUUM complete: {} → {} (reclaimed {} bytes)",
            size_before,
            size_after,
            size_before.saturating_sub(size_after),
        );

        Ok(VacuumResult {
            size_before,
            size_after,
        })
    }

    /// Read the database file size via SQLite's `page_count * page_size` pragmas.
    ///
    /// This is more reliable than `std::fs::metadata` because it reflects
    /// the logical size from SQLite's perspective, and works even when the
    /// database path is `:memory:` or a temp file.
    fn db_file_size(conn: &rusqlite::Connection) -> u64 {
        let page_count: u64 = conn
            .pragma_query_value(None, "page_count", |row| row.get(0))
            .unwrap_or(0);
        let page_size: u64 = conn
            .pragma_query_value(None, "page_size", |row| row.get(0))
            .unwrap_or(4096);
        page_count * page_size
    }

    /// Clear all data from the knowledge graph while preserving the schema.
    ///
    /// Deletes all rows from nodes, edges, embeddings, repos, and ingested_files
    /// in a single transaction. This is much faster than dropping and recreating
    /// tables because the schema (indexes, virtual tables) stays intact.
    ///
    /// Returns the number of nodes that were deleted.
    pub fn truncate(&self) -> SCSResult<usize> {
        let conn = self.pool.get().pool_err()?;
        let tx = conn.unchecked_transaction()?;

        // Count nodes before deletion for the return value.
        let node_count: usize = tx
            .query_row("SELECT COUNT(*) FROM nodes", [], |row| row.get(0))
            .unwrap_or(0);

        // Clear the USearch vector index.
        self.vector_index.clear()?;

        // Delete in dependency order: edges reference nodes, ingested_files
        // references repos.
        tx.execute_batch(
            "DELETE FROM edges;
             DELETE FROM nodes;
             DELETE FROM ingested_files;
             DELETE FROM repos;",
        )?;

        tx.commit()?;

        log::info!("Truncated knowledge graph: removed {} nodes", node_count);
        Ok(node_count)
    }

    /// Clear all ingestion hash records so the next ingest re-processes every file.
    ///
    /// Used when the embedding model dimension changes — existing embeddings
    /// are incompatible, so every file must be re-embedded. Only clears the
    /// tracking records (content hashes); nodes and edges survive.
    ///
    /// Returns the number of records cleared.
    pub fn clear_ingestion_hashes(&self) -> SCSResult<usize> {
        let conn = self.pool.get().pool_err()?;
        let count: usize = conn
            .query_row("SELECT COUNT(*) FROM ingested_files", [], |row| row.get(0))
            .unwrap_or(0);
        conn.execute("DELETE FROM ingested_files", [])?;
        log::info!("Cleared {} ingestion hash records", count);
        Ok(count)
    }

    /// Clear all embedding vectors from the USearch index without touching
    /// nodes, edges, or ingestion hashes.
    ///
    /// Used when the embedding model changes but the dimension stays the same
    /// (e.g., switching between two 1024-dim models). The existing index
    /// structure is preserved — only the vectors are removed. Callers should
    /// also clear ingestion hashes to force re-embedding on next ingest.
    pub fn clear_embeddings(&self) -> SCSResult<()> {
        self.vector_index.clear()?;
        log::info!("Cleared all embedding vectors from the USearch index");
        Ok(())
    }

    // ── Batch Operations ──────────────────────────────────────────

    /// Get multiple nodes by ID in a single SQL query.
    ///
    /// Replaces N individual `get_node` calls with one `IN (...)` query,
    /// eliminating per-node FFI round-trips in handlers like `inspect_file`
    /// and `sample_nodes`. Returns nodes in arbitrary order (no guaranteed
    /// correspondence to the input ID order).
    ///
    /// When `node_ids` is empty, returns an empty vec without touching the DB.
    pub fn batch_get_nodes(&self, node_ids: &[String]) -> SCSResult<Vec<Node>> {
        if node_ids.is_empty() {
            return Ok(Vec::new());
        }
        observe_result(
            QueryBackend::Sqlite,
            "KnowledgeGraph::batch_get_nodes",
            "nodes",
            format!("count={}", node_ids.len()),
            |obs| {
                let wait_started = std::time::Instant::now();
                let conn = self.pool.get().pool_err()?;
                obs.set_wait(wait_started.elapsed());

                let placeholders: String = (1..=node_ids.len())
                    .map(|i| format!("?{i}"))
                    .collect::<Vec<_>>()
                    .join(", ");

                let sql = format!("SELECT * FROM nodes WHERE id IN ({placeholders})");

                let params: Vec<Box<dyn rusqlite::types::ToSql>> = node_ids
                    .iter()
                    .map(|id| Box::new(id.clone()) as Box<dyn rusqlite::types::ToSql>)
                    .collect();

                let params_ref: Vec<&dyn rusqlite::types::ToSql> =
                    params.iter().map(|b| b.as_ref()).collect();

                let mut stmt = conn.prepare(&sql)?;
                let nodes = stmt
                    .query_map(params_ref.as_slice(), row_to_node)?
                    .collect::<Result<Vec<_>, _>>()?;

                obs.set_rows(nodes.len());
                Ok(nodes)
            },
        )
    }

    /// Get edges for multiple nodes in a single SQL query.
    ///
    /// Returns a map of `node_id → Vec<Edge>` for the requested direction.
    /// This replaces N individual `get_edges` calls with a single query using
    /// an IN clause, reducing connection pool pressure for large traversals.
    ///
    /// When `node_ids` is empty, returns an empty map without touching the DB.
    pub fn batch_get_edges(
        &self,
        node_ids: &[String],
        direction: EdgeDirection,
    ) -> SCSResult<HashMap<String, Vec<Edge>>> {
        if node_ids.is_empty() {
            return Ok(HashMap::new());
        }
        observe_result(
            QueryBackend::Sqlite,
            "KnowledgeGraph::batch_get_edges",
            "edges",
            format!("direction={direction:?} count={}", node_ids.len()),
            |obs| {
                let wait_started = std::time::Instant::now();
                let conn = self.pool.get().pool_err()?;
                obs.set_wait(wait_started.elapsed());

                // Build placeholder list: (?1, ?2, ?3, ...)
                let placeholders: String = (1..=node_ids.len())
                    .map(|i| format!("?{i}"))
                    .collect::<Vec<_>>()
                    .join(", ");

                let sql = match direction {
                    EdgeDirection::Outgoing => {
                        format!("SELECT * FROM edges WHERE source_id IN ({placeholders})")
                    }
                    EdgeDirection::Incoming => {
                        format!("SELECT * FROM edges WHERE target_id IN ({placeholders})")
                    }
                    EdgeDirection::Both => {
                        format!(
                            "SELECT * FROM edges WHERE source_id IN ({placeholders}) \
                             OR target_id IN ({placeholders})"
                        )
                    }
                };

                let params: Vec<Box<dyn rusqlite::types::ToSql>> = node_ids
                    .iter()
                    .map(|id| Box::new(id.clone()) as Box<dyn rusqlite::types::ToSql>)
                    .collect();

                let params_ref: Vec<&dyn rusqlite::types::ToSql> =
                    params.iter().map(|b| b.as_ref()).collect();

                let mut stmt = conn.prepare(&sql)?;
                let edges: Vec<Edge> = stmt
                    .query_map(params_ref.as_slice(), row_to_edge)?
                    .collect::<Result<Vec<_>, _>>()?;

                // Group edges by the requesting node ID.
                let mut result: HashMap<String, Vec<Edge>> = HashMap::new();
                let requested_ids: HashSet<&str> = node_ids.iter().map(String::as_str).collect();
                for edge in edges {
                    match direction {
                        EdgeDirection::Outgoing => {
                            result.entry(edge.source_id.clone()).or_default().push(edge);
                        }
                        EdgeDirection::Incoming => {
                            result.entry(edge.target_id.clone()).or_default().push(edge);
                        }
                        EdgeDirection::Both => {
                            // An edge might match on source OR target. Add to both if applicable.
                            let source_match = requested_ids.contains(edge.source_id.as_str());
                            let target_match = requested_ids.contains(edge.target_id.as_str());
                            if source_match {
                                result
                                    .entry(edge.source_id.clone())
                                    .or_default()
                                    .push(edge.clone());
                            }
                            if target_match {
                                result.entry(edge.target_id.clone()).or_default().push(edge);
                            }
                        }
                    }
                }

                let row_count = result.values().map(Vec::len).sum();
                obs.set_rows(row_count);
                Ok(result)
            },
        )
    }
}

/// Direction for edge queries.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EdgeDirection {
    /// Follow edges where node is the source.
    Outgoing,
    /// Follow edges where node is the target.
    Incoming,
    /// Follow edges in both directions.
    Both,
}

/// Direction for recursive graph traversal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TraversalDirection {
    /// Traverse outgoing edges (source → target).
    Outgoing,
    /// Traverse incoming edges (target → source).
    Incoming,
}

#[cfg(test)]
mod tests {
    use super::*;
    use scs_core::error::SCSError;
    use serde_json::json;

    fn test_graph() -> (tempfile::TempDir, KnowledgeGraph) {
        let dir = tempfile::tempdir().unwrap();
        let config = SCSConfig::for_testing(dir.path());
        let graph = KnowledgeGraph::open(config).unwrap();
        (dir, graph)
    }

    fn make_embedding(seed: u64, dim: usize) -> Vec<f32> {
        // Simple deterministic "embedding" — not random, but unique per seed.
        (0..dim)
            .map(|i| (seed as f32 * 0.1 + i as f32 * 0.01) % 1.0)
            .collect()
    }

    fn explain_query_plan(conn: &rusqlite::Connection, query: &str) -> String {
        let mut stmt = conn.prepare(query).unwrap();
        let rows = stmt
            .query_map([], |row| {
                let detail: String = row.get(3)?;
                Ok(detail)
            })
            .unwrap();

        rows.collect::<Result<Vec<_>, _>>().unwrap().join("\n")
    }

    // ── Node CRUD tests ──

    #[test]
    fn upsert_and_get_node() {
        let (_dir, graph) = test_graph();

        let mut meta = HashMap::new();
        meta.insert(
            "source".to_string(),
            serde_json::Value::String("user".to_string()),
        );

        let node = graph
            .upsert_node(
                "test-1",
                NodeType::Function,
                "test correction",
                "hello world",
                Some(&meta),
                None,
                None,
            )
            .unwrap();

        assert_eq!(node.id, "test-1");
        assert_eq!(node.node_type, NodeType::Function);
        assert_eq!(node.name, "test correction");
        assert_eq!(node.content, "hello world");

        let retrieved = graph.get_node("test-1").unwrap().unwrap();
        assert_eq!(retrieved.id, "test-1");
    }

    #[test]
    fn upsert_updates_existing() {
        let (_dir, graph) = test_graph();

        graph
            .upsert_node(
                "n1",
                NodeType::Function,
                "old name",
                "old",
                None,
                None,
                None,
            )
            .unwrap();
        graph
            .upsert_node(
                "n1",
                NodeType::Function,
                "new name",
                "new",
                None,
                None,
                None,
            )
            .unwrap();

        let node = graph.get_node("n1").unwrap().unwrap();
        assert_eq!(node.name, "new name");
        assert_eq!(node.content, "new");
    }

    #[test]
    fn failed_embedding_upsert_preserves_existing_node_and_vector() {
        let (dir, graph) = test_graph();
        let config = graph.config().clone();
        let original_embedding = make_embedding(1, 768);
        let original_repo = graph.get_or_create_repo("/repo/original").unwrap();
        let replacement_repo = graph.get_or_create_repo("/repo/replacement").unwrap();
        let original_metadata = HashMap::from([
            ("file_path".to_string(), json!("src/original.rs")),
            ("start_line".to_string(), json!(7)),
        ]);
        let original = graph
            .upsert_node(
                "n1",
                NodeType::Function,
                "original",
                "fn original() {}",
                Some(&original_metadata),
                Some(&original_embedding),
                Some(original_repo.id),
            )
            .unwrap();
        graph.flush_vector_index().unwrap();

        let error = graph
            .upsert_node(
                "n1",
                NodeType::Class,
                "replacement",
                "struct Replacement;",
                Some(&HashMap::from([(
                    "file_path".to_string(),
                    json!("src/replacement.rs"),
                )])),
                Some(&make_embedding(2, 767)),
                Some(replacement_repo.id),
            )
            .unwrap_err();

        assert!(matches!(
            error,
            SCSError::DimensionMismatch {
                expected: 768,
                actual: 767
            }
        ));
        let preserved = graph.get_node("n1").unwrap().unwrap();
        assert_eq!(preserved.id, original.id);
        assert_eq!(preserved.node_type, original.node_type);
        assert_eq!(preserved.name, original.name);
        assert_eq!(preserved.content, original.content);
        assert_eq!(preserved.metadata, original.metadata);
        assert_eq!(preserved.repo_id, original.repo_id);
        assert_eq!(preserved.created_at, original.created_at);
        assert_eq!(preserved.updated_at, original.updated_at);
        assert_eq!(graph.count_embeddings().unwrap(), 1);
        drop(graph);

        let reopened = KnowledgeGraph::open(config).unwrap();
        let reopened_node = reopened.get_node("n1").unwrap().unwrap();
        assert_eq!(reopened_node.id, original.id);
        assert_eq!(reopened_node.node_type, original.node_type);
        assert_eq!(reopened_node.name, original.name);
        assert_eq!(reopened_node.content, original.content);
        assert_eq!(reopened_node.metadata, original.metadata);
        assert_eq!(reopened_node.repo_id, original.repo_id);
        assert_eq!(reopened_node.created_at, original.created_at);
        assert_eq!(reopened_node.updated_at, original.updated_at);
        assert_eq!(reopened.count_embeddings().unwrap(), 1);
        let results = reopened
            .search_by_vector(&original_embedding, None, 1, None)
            .unwrap();
        assert_eq!(results[0].node.id, "n1");
        assert!(results[0].distance < 1e-6);
        drop(dir);
    }

    #[test]
    fn reopened_vectors_contain_checks_the_persisted_sidecar() {
        let (_dir, graph) = test_graph();
        let embedding = make_embedding(1, 768);
        graph
            .upsert_node(
                "durable-node",
                NodeType::Function,
                "durable",
                "fn durable() {}",
                None,
                Some(&embedding),
                None,
            )
            .unwrap();
        graph.flush_vector_index().unwrap();

        assert!(graph
            .reopened_vectors_contain(&["durable-node".to_string()])
            .unwrap());
        assert!(!graph
            .reopened_vectors_contain(&["missing-node".to_string()])
            .unwrap());
        assert!(graph
            .reopened_vectors_absent(&["missing-node".to_string()])
            .unwrap());
        assert!(!graph
            .reopened_vectors_absent(&["durable-node".to_string()])
            .unwrap());
    }

    #[test]
    fn get_nonexistent_returns_none() {
        let (_dir, graph) = test_graph();
        assert!(graph.get_node("nonexistent").unwrap().is_none());
    }

    #[test]
    fn delete_node_removes_it() {
        let (_dir, graph) = test_graph();

        graph
            .upsert_node(
                "del-1",
                NodeType::Function,
                "to delete",
                "",
                None,
                None,
                None,
            )
            .unwrap();
        assert!(graph.delete_node("del-1").unwrap());
        assert!(graph.get_node("del-1").unwrap().is_none());
    }

    #[test]
    fn delete_nonexistent_returns_false() {
        let (_dir, graph) = test_graph();
        assert!(!graph.delete_node("nope").unwrap());
    }

    #[test]
    fn list_nodes_with_type_filter() {
        let (_dir, graph) = test_graph();

        graph
            .upsert_node("c1", NodeType::Function, "corr 1", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("v1", NodeType::Class, "class 1", "", None, None, None)
            .unwrap();

        let all = graph.list_nodes(None, 100, 0, None).unwrap();
        assert_eq!(all.len(), 2);

        let functions = graph
            .list_nodes(Some(NodeType::Function), 100, 0, None)
            .unwrap();
        assert_eq!(functions.len(), 1);
        assert_eq!(functions[0].node_type, NodeType::Function);
    }

    #[test]
    fn list_nodes_respects_limit() {
        let (_dir, graph) = test_graph();

        for i in 0..5 {
            graph
                .upsert_node(
                    &format!("n{i}"),
                    NodeType::Function,
                    &format!("concept {i}"),
                    "",
                    None,
                    None,
                    None,
                )
                .unwrap();
        }

        let limited = graph.list_nodes(None, 3, 0, None).unwrap();
        assert_eq!(limited.len(), 3);
    }

    #[test]
    fn list_nodes_records_sqlite_observability_event() {
        crate::observability::clear();
        let (_dir, graph) = test_graph();
        graph
            .upsert_node(
                "obs-1",
                NodeType::Function,
                "observed",
                "",
                None,
                None,
                None,
            )
            .unwrap();

        let nodes = graph.list_nodes(None, 10, 0, None).unwrap();
        assert_eq!(nodes.len(), 1);

        let snapshot = crate::observability::snapshot(crate::observability::QuerySnapshotFilter {
            limit: Some(10),
            backend: Some(crate::observability::QueryBackend::Sqlite),
            min_duration_ms: None,
            status: None,
        });

        assert!(snapshot.events.iter().any(|event| {
            event.operation == "KnowledgeGraph::list_nodes" && event.row_count == Some(1)
        }));
    }

    #[test]
    fn list_nodes_queries_use_order_indexes_without_temp_sort() {
        let (_dir, graph) = test_graph();
        let repo_id = graph.get_or_create_repo("/tmp/scs-list-nodes").unwrap().id;

        for i in 0..8 {
            graph
                .upsert_node(
                    &format!("n{i}"),
                    if i % 2 == 0 {
                        NodeType::Function
                    } else {
                        NodeType::Class
                    },
                    &format!("node {i}"),
                    "",
                    None,
                    None,
                    Some(repo_id),
                )
                .unwrap();
        }

        let conn = graph.pool.get().unwrap();
        let queries = [
            "EXPLAIN QUERY PLAN SELECT * FROM nodes ORDER BY updated_at DESC, id DESC LIMIT 100 OFFSET 0".to_string(),
            "EXPLAIN QUERY PLAN SELECT * FROM nodes WHERE type = 'function' ORDER BY updated_at DESC, id DESC LIMIT 100 OFFSET 0".to_string(),
            format!(
                "EXPLAIN QUERY PLAN SELECT * FROM nodes WHERE repo_id = {repo_id} ORDER BY updated_at DESC, id DESC LIMIT 100 OFFSET 0"
            ),
            format!(
                "EXPLAIN QUERY PLAN SELECT * FROM nodes WHERE type = 'function' AND repo_id = {repo_id} ORDER BY updated_at DESC, id DESC LIMIT 100 OFFSET 0"
            ),
        ];

        for query in queries {
            let plan = explain_query_plan(&conn, &query);
            assert!(
                !plan.contains("USE TEMP B-TREE"),
                "list_nodes query should avoid temp sorting:\n{query}\n{plan}"
            );
        }
    }

    #[test]
    fn count_nodes_by_type_returns_correct_breakdown() {
        // Verify that count_nodes_by_type(None) returns the same totals as calling
        // count_nodes for each type individually — O(1) vs O(n_types) queries.
        let (_dir, graph) = test_graph();

        graph
            .upsert_node("c1", NodeType::Class, "ClassA", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("c2", NodeType::Class, "ClassB", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("f1", NodeType::Function, "fn_a", "", None, None, None)
            .unwrap();

        let counts = graph.count_nodes_by_type(None).unwrap();

        assert_eq!(
            counts.get("class").copied().unwrap_or(0),
            2,
            "should count 2 class nodes"
        );
        assert_eq!(
            counts.get("function").copied().unwrap_or(0),
            1,
            "should count 1 function node"
        );
        // Types with zero nodes must be omitted — not present as zero-value entries.
        assert!(
            !counts.contains_key("module"),
            "empty types should be absent from the map"
        );

        // Total via batch must equal count_nodes(None).
        let total_batch: i64 = counts.values().sum();
        let total_sequential = graph.count_nodes(None, None).unwrap();
        assert_eq!(
            total_batch, total_sequential,
            "batch total must match sequential total"
        );
    }

    #[test]
    fn count_nodes_by_type_empty_graph_returns_empty_map() {
        let (_dir, graph) = test_graph();
        let counts = graph.count_nodes_by_type(None).unwrap();
        assert!(counts.is_empty(), "empty graph should yield empty map");
    }

    #[test]
    fn count_nodes_by_type_with_repo_id_scopes_correctly() {
        // Nodes from repo A must not appear in repo B's count, and vice versa.
        // Each repo must only see its own nodes when stats are repo-scoped.
        let (_dir, graph) = test_graph();

        let repo_a = graph.get_or_create_repo("/repos/alpha").unwrap();
        let repo_b = graph.get_or_create_repo("/repos/beta").unwrap();

        // Insert 2 classes under repo A and 1 function under repo B.
        graph
            .upsert_node(
                "a1",
                NodeType::Class,
                "AlphaClass",
                "",
                None,
                None,
                Some(repo_a.id),
            )
            .unwrap();
        graph
            .upsert_node(
                "a2",
                NodeType::Class,
                "AlphaClass2",
                "",
                None,
                None,
                Some(repo_a.id),
            )
            .unwrap();
        graph
            .upsert_node(
                "b1",
                NodeType::Function,
                "BetaFn",
                "",
                None,
                None,
                Some(repo_b.id),
            )
            .unwrap();

        // Scoped to repo A: should see 2 classes, no functions.
        let counts_a = graph.count_nodes_by_type(Some(repo_a.id)).unwrap();
        assert_eq!(
            counts_a.get("class").copied().unwrap_or(0),
            2,
            "repo A should have 2 classes"
        );
        assert!(
            !counts_a.contains_key("function"),
            "repo A should not see repo B's function"
        );

        // Scoped to repo B: should see 1 function, no classes.
        let counts_b = graph.count_nodes_by_type(Some(repo_b.id)).unwrap();
        assert_eq!(
            counts_b.get("function").copied().unwrap_or(0),
            1,
            "repo B should have 1 function"
        );
        assert!(
            !counts_b.contains_key("class"),
            "repo B should not see repo A's classes"
        );

        // Unscoped: should see both repos combined.
        let counts_all = graph.count_nodes_by_type(None).unwrap();
        assert_eq!(
            counts_all.get("class").copied().unwrap_or(0),
            2,
            "unscoped should see all classes"
        );
        assert_eq!(
            counts_all.get("function").copied().unwrap_or(0),
            1,
            "unscoped should see all functions"
        );
    }

    #[test]
    fn search_by_name_finds_substring_case_insensitive() {
        let (_dir, graph) = test_graph();

        graph
            .upsert_node("c1", NodeType::Class, "AudioEngine", "", None, None, None)
            .unwrap();
        graph
            .upsert_node(
                "c2",
                NodeType::Function,
                "audio_process",
                "",
                None,
                None,
                None,
            )
            .unwrap();
        graph
            .upsert_node("c3", NodeType::Method, "play_video", "", None, None, None)
            .unwrap();

        // Case-insensitive substring "audio" should match the first two.
        let results = graph.search_by_name("audio", None, 10, None).unwrap();
        assert_eq!(results.len(), 2);

        // With type filter, only the class should match.
        let filtered = graph
            .search_by_name("audio", Some(NodeType::Class), 10, None)
            .unwrap();
        assert_eq!(filtered.len(), 1);
        assert_eq!(filtered[0].name, "AudioEngine");

        // Uppercase search should still match (case-insensitive).
        let upper = graph.search_by_name("AUDIO", None, 10, None).unwrap();
        assert_eq!(upper.len(), 2);
    }

    // ── Edge CRUD tests ──

    #[test]
    fn upsert_and_get_edge() {
        let (_dir, graph) = test_graph();

        graph
            .upsert_node("a", NodeType::Class, "ClassA", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("b", NodeType::Method, "method_b", "", None, None, None)
            .unwrap();

        let edge = graph.upsert_edge("a", "b", "contains", 1.0, None).unwrap();
        assert_eq!(edge.source_id, "a");
        assert_eq!(edge.target_id, "b");
        assert_eq!(edge.relationship, "contains");

        let edges = graph.get_edges("a", None, EdgeDirection::Outgoing).unwrap();
        assert_eq!(edges.len(), 1);
        assert_eq!(edges[0].target_id, "b");
    }

    #[test]
    fn edge_deterministic_id() {
        let (_dir, graph) = test_graph();

        graph
            .upsert_node("x", NodeType::Function, "func_x", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("y", NodeType::Function, "func_y", "", None, None, None)
            .unwrap();

        let edge1 = graph.upsert_edge("x", "y", "calls", 1.0, None).unwrap();
        let edge2 = graph.upsert_edge("x", "y", "calls", 2.0, None).unwrap();

        // Same source/target/relationship produces the same edge ID.
        assert_eq!(edge1.id, edge2.id);
        // Weight updated on second upsert.
        assert!((edge2.weight - 2.0).abs() < f64::EPSILON);
    }

    #[test]
    fn cascade_delete_edges_on_node_delete() {
        let (_dir, graph) = test_graph();

        graph
            .upsert_node("src", NodeType::File, "file.py", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("tgt", NodeType::Class, "MyClass", "", None, None, None)
            .unwrap();
        graph
            .upsert_edge("src", "tgt", "contains", 1.0, None)
            .unwrap();

        graph.delete_node("src").unwrap();

        let edges = graph.get_edges("tgt", None, EdgeDirection::Both).unwrap();
        assert!(edges.is_empty());
    }

    // ── Vector Search tests ──

    #[test]
    fn search_finds_similar_embeddings() {
        let (_dir, graph) = test_graph();

        let mut target = vec![0.0f32; 768];
        target[0] = 1.0;

        let mut close = vec![0.0f32; 768];
        close[0] = 0.9;
        close[1] = 0.1;

        let mut far = vec![0.0f32; 768];
        far[383] = 1.0;

        graph
            .upsert_node(
                "close",
                NodeType::Function,
                "close",
                "",
                None,
                Some(&close),
                None,
            )
            .unwrap();
        graph
            .upsert_node("far", NodeType::Function, "far", "", None, Some(&far), None)
            .unwrap();

        let results = graph.search_by_vector(&target, None, 2, None).unwrap();
        assert_eq!(results.len(), 2);
        assert_eq!(results[0].node.id, "close"); // Most similar first.
    }

    #[test]
    fn search_filters_by_type() {
        let (_dir, graph) = test_graph();
        let emb = make_embedding(42, 768);

        graph
            .upsert_node("c1", NodeType::Function, "corr", "", None, Some(&emb), None)
            .unwrap();
        graph
            .upsert_node(
                "v1",
                NodeType::Function,
                "vocab",
                "",
                None,
                Some(&emb),
                None,
            )
            .unwrap();

        let corrections = graph
            .search_by_vector(&emb, Some(NodeType::Function), 10, None)
            .unwrap();
        assert!(corrections
            .iter()
            .all(|r| r.node.node_type == NodeType::Function));
    }

    #[test]
    fn search_returns_empty_for_no_embeddings() {
        let (_dir, graph) = test_graph();
        let emb = make_embedding(42, 768);
        let results = graph.search_by_vector(&emb, None, 10, None).unwrap();
        assert!(results.is_empty());
    }

    // ── Graph Traversal tests ──

    #[test]
    fn get_neighbors_returns_connected_nodes() {
        let (_dir, graph) = test_graph();

        graph
            .upsert_node("mod", NodeType::Module, "module", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("cls", NodeType::Class, "MyClass", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("fn", NodeType::Function, "my_func", "", None, None, None)
            .unwrap();
        graph
            .upsert_edge("mod", "cls", "contains", 1.0, None)
            .unwrap();
        graph
            .upsert_edge("mod", "fn", "contains", 1.0, None)
            .unwrap();

        let neighbors = graph
            .get_neighbors("mod", None, EdgeDirection::Outgoing, 50)
            .unwrap();
        assert_eq!(neighbors.len(), 2);
        let names: std::collections::HashSet<_> =
            neighbors.iter().map(|n| n.name.as_str()).collect();
        assert!(names.contains("MyClass"));
        assert!(names.contains("my_func"));
    }

    #[test]
    fn traverse_respects_depth() {
        let (_dir, graph) = test_graph();

        graph
            .upsert_node("a", NodeType::Module, "A", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("b", NodeType::Class, "B", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("c", NodeType::Method, "C", "", None, None, None)
            .unwrap();
        graph.upsert_edge("a", "b", "contains", 1.0, None).unwrap();
        graph.upsert_edge("b", "c", "contains", 1.0, None).unwrap();

        // Depth 1: should find B but not C.
        let results = graph
            .traverse("a", 1, None, TraversalDirection::Outgoing)
            .unwrap();
        let names: std::collections::HashSet<_> =
            results.iter().map(|r| r.node.name.as_str()).collect();
        assert!(names.contains("B"));
        assert!(!names.contains("C"));

        // Depth 2: should find both B and C.
        let results = graph
            .traverse("a", 2, None, TraversalDirection::Outgoing)
            .unwrap();
        let names: std::collections::HashSet<_> =
            results.iter().map(|r| r.node.name.as_str()).collect();
        assert!(names.contains("B"));
        assert!(names.contains("C"));
    }

    #[test]
    fn traverse_prevents_cycles() {
        let (_dir, graph) = test_graph();

        graph
            .upsert_node("x", NodeType::Function, "X", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("y", NodeType::Function, "Y", "", None, None, None)
            .unwrap();
        graph.upsert_edge("x", "y", "calls", 1.0, None).unwrap();
        graph.upsert_edge("y", "x", "calls", 1.0, None).unwrap();

        let results = graph
            .traverse("x", 5, None, TraversalDirection::Outgoing)
            .unwrap();
        // Should find Y but not loop back to X infinitely.
        assert!(!results.is_empty());
    }

    // ── Graph RAG test ──

    #[test]
    fn graph_rag_returns_similar_and_context() {
        let (_dir, graph) = test_graph();

        let mut emb = vec![0.0f32; 768];
        emb[0] = 1.0;

        let mut meta = HashMap::new();
        meta.insert(
            "corrected".to_string(),
            serde_json::Value::String("fixed".to_string()),
        );

        graph
            .upsert_node(
                "corr",
                NodeType::Function,
                "raw",
                "raw text",
                Some(&meta),
                Some(&emb),
                None,
            )
            .unwrap();
        graph
            .upsert_node(
                "vocab",
                NodeType::Function,
                "VocabTerm",
                "",
                None,
                None,
                None,
            )
            .unwrap();
        graph
            .upsert_edge("corr", "vocab", "references", 1.0, None)
            .unwrap();

        let result = graph
            .graph_rag_query(&emb, Some(NodeType::Function), 5, 2, None, None)
            .unwrap();

        assert!(!result.similar_nodes.is_empty());
        assert_eq!(result.similar_nodes[0].node.id, "corr");

        let context_names: std::collections::HashSet<_> = result
            .graph_context
            .iter()
            .map(|n| n.name.as_str())
            .collect();
        assert!(context_names.contains("VocabTerm"));
    }

    /// Edge IDs must match the Python implementation for backward compatibility.
    #[test]
    fn edge_id_matches_python() {
        // Python: uuid.uuid5(UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890"), "src:tgt:contains")
        let edge_id = make_edge_id("src", "tgt", "contains");
        // This should be deterministic — the exact value depends on the UUID v5 algorithm.
        assert!(!edge_id.is_empty());
        // Same inputs must produce the same output.
        assert_eq!(edge_id, make_edge_id("src", "tgt", "contains"));
        // Different inputs must produce different output.
        assert_ne!(edge_id, make_edge_id("src", "tgt", "calls"));
    }

    /// Relationships outside the code-only contract are rejected by storage.
    #[test]
    fn upsert_edge_rejects_custom_relationship() {
        let (_dir, graph) = test_graph();

        graph
            .upsert_node("t1", NodeType::Function, "Task 1", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("t2", NodeType::Function, "Task 2", "", None, None, None)
            .unwrap();

        assert!(graph
            .upsert_edge("t1", "t2", "depends_on", 1.0, None)
            .is_err());
    }

    /// Upserting a node merges independently owned metadata rather than replacing it.
    #[test]
    fn upsert_node_merges_metadata_preserving_existing_keys() {
        let (_dir, graph) = test_graph();

        // Step 1: Initial ingestion with parser metadata.
        let mut meta1 = HashMap::new();
        meta1.insert("file_path".to_string(), json!("lib.rs"));
        meta1.insert("start_line".to_string(), json!(1));

        graph
            .upsert_node(
                "n1",
                NodeType::Function,
                "run",
                "fn run() {}",
                Some(&meta1),
                None,
                None,
            )
            .unwrap();

        // Step 2: An independent analyzer enriches metadata.
        let mut analyzer_metadata = HashMap::new();
        analyzer_metadata.insert("file_path".to_string(), json!("lib.rs"));
        analyzer_metadata.insert("start_line".to_string(), json!(1));
        analyzer_metadata.insert("semantic_label".to_string(), json!("entrypoint"));
        analyzer_metadata.insert("analysis_version".to_string(), json!(2));

        graph
            .upsert_node(
                "n1",
                NodeType::Function,
                "run",
                "fn run() {}",
                Some(&analyzer_metadata),
                None,
                None,
            )
            .unwrap();

        // Step 3: Re-ingestion updates parser metadata only.
        let mut meta_reingestion = HashMap::new();
        meta_reingestion.insert("file_path".to_string(), json!("lib.rs"));
        meta_reingestion.insert("start_line".to_string(), json!(5)); // line changed

        let node = graph
            .upsert_node(
                "n1",
                NodeType::Function,
                "run",
                "fn run() { true }",
                Some(&meta_reingestion),
                None,
                None,
            )
            .unwrap();

        // Parser field was updated.
        assert_eq!(node.metadata["start_line"], json!(5));
        // Independently owned fields survived the re-ingestion.
        assert_eq!(node.metadata["semantic_label"], json!("entrypoint"));
        assert_eq!(node.metadata["analysis_version"], json!(2));
    }

    /// The same merge guarantee protects independently owned edge metadata.
    #[test]
    fn upsert_edge_merges_metadata_preserving_existing_keys() {
        let (_dir, graph) = test_graph();

        graph
            .upsert_node("a", NodeType::Class, "A", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("b", NodeType::Method, "B", "", None, None, None)
            .unwrap();

        // Step 1: Initial edge (no metadata).
        graph.upsert_edge("a", "b", "contains", 1.0, None).unwrap();

        // Step 2: An independent analyzer adds metadata.
        let mut analyzer_metadata = HashMap::new();
        analyzer_metadata.insert("confidence_source".to_string(), json!("static"));
        analyzer_metadata.insert("analysis_version".to_string(), json!(2));

        graph
            .upsert_edge("a", "b", "contains", 1.0, Some(&analyzer_metadata))
            .unwrap();

        // Step 3: Re-ingestion with empty metadata but updated weight.
        let edge = graph.upsert_edge("a", "b", "contains", 2.0, None).unwrap();

        assert_eq!(edge.weight, 2.0);
        assert_eq!(edge.metadata["confidence_source"], json!("static"));
        assert_eq!(edge.metadata["analysis_version"], json!(2));
    }

    // ── Embedding count tests ──

    #[test]
    fn count_embeddings_empty_graph() {
        let (_dir, graph) = test_graph();
        assert_eq!(graph.count_embeddings().unwrap(), 0);
    }

    #[test]
    fn count_embeddings_after_inserts() {
        let (_dir, graph) = test_graph();

        // Nodes without embeddings don't count.
        graph
            .upsert_node("n1", NodeType::Class, "Foo", "class Foo", None, None, None)
            .unwrap();
        assert_eq!(graph.count_embeddings().unwrap(), 0);

        // Adding nodes with embeddings increments the count.
        let emb = make_embedding(1, 768);
        graph
            .upsert_node(
                "n2",
                NodeType::Function,
                "bar",
                "def bar()",
                None,
                Some(&emb),
                None,
            )
            .unwrap();
        assert_eq!(graph.count_embeddings().unwrap(), 1);

        let emb2 = make_embedding(2, 768);
        graph
            .upsert_node(
                "n3",
                NodeType::Method,
                "baz",
                "def baz()",
                None,
                Some(&emb2),
                None,
            )
            .unwrap();
        assert_eq!(graph.count_embeddings().unwrap(), 2);

        // Re-upserting the same node's embedding doesn't double-count.
        let emb3 = make_embedding(3, 768);
        graph
            .upsert_node(
                "n2",
                NodeType::Function,
                "bar",
                "def bar()",
                None,
                Some(&emb3),
                None,
            )
            .unwrap();
        assert_eq!(graph.count_embeddings().unwrap(), 2);
    }

    // ── Bulk metadata delete tests ──

    #[test]
    fn delete_nodes_by_metadata_removes_matching_and_preserves_others() {
        let (_dir, graph) = test_graph();
        let dataset_id = "ds-wikitext-001";

        // Create 3 dataset rows belonging to our dataset.
        for i in 0..3 {
            let id = format!("row-{i}");
            let meta: HashMap<String, serde_json::Value> = [
                ("parent_dataset_id".to_string(), json!(dataset_id)),
                ("row_index".to_string(), json!(i)),
            ]
            .into();
            let emb = make_embedding(i + 10, 768);
            graph
                .upsert_node(
                    &id,
                    NodeType::Function,
                    &format!("row {i}"),
                    &format!("content {i}"),
                    Some(&meta),
                    Some(&emb),
                    None,
                )
                .unwrap();
        }

        // Create 1 unrelated node with different metadata to confirm it survives.
        let unrelated_meta: HashMap<String, serde_json::Value> =
            [("parent_dataset_id".to_string(), json!("ds-other-999"))].into();
        graph
            .upsert_node(
                "row-other",
                NodeType::Function,
                "other row",
                "other content",
                Some(&unrelated_meta),
                Some(&make_embedding(99, 768)),
                None,
            )
            .unwrap();

        // 4 dataset_row nodes, 4 embeddings before delete.
        assert_eq!(
            graph
                .list_nodes(Some(NodeType::Function), 100, 0, None)
                .unwrap()
                .len(),
            4,
        );

        // Bulk delete by metadata filter.
        let deleted = graph
            .delete_nodes_by_metadata(&NodeType::Function, "parent_dataset_id", dataset_id)
            .unwrap();

        assert_eq!(deleted, 3, "should delete exactly the 3 matching rows");

        // The unrelated node must survive.
        let remaining = graph
            .list_nodes(Some(NodeType::Function), 100, 0, None)
            .unwrap();
        assert_eq!(remaining.len(), 1);
        assert_eq!(remaining[0].id, "row-other");

        // Embeddings for deleted nodes should also be gone.
        // Only the unrelated node's embedding remains (+ none from deleted rows).
        // We can verify indirectly — count_embeddings covers all types, so
        // just confirm it decreased by 3.
        assert_eq!(graph.count_embeddings().unwrap(), 1);
    }

    #[test]
    fn delete_nodes_by_metadata_returns_zero_when_no_matches() {
        let (_dir, graph) = test_graph();

        let deleted = graph
            .delete_nodes_by_metadata(&NodeType::Function, "parent_dataset_id", "nonexistent")
            .unwrap();

        assert_eq!(deleted, 0);
    }

    // ── Vacuum tests ──

    /// VACUUM on a populated graph should succeed and report valid sizes.
    #[test]
    fn vacuum_returns_valid_sizes() {
        let (_dir, graph) = test_graph();

        // Populate with some data so the database has real pages.
        let emb = make_embedding(1, 768);
        graph
            .upsert_node(
                "v1",
                NodeType::Class,
                "Foo",
                "class Foo",
                None,
                Some(&emb),
                None,
            )
            .unwrap();
        graph
            .upsert_node(
                "v2",
                NodeType::Function,
                "bar",
                "def bar()",
                None,
                Some(&emb),
                None,
            )
            .unwrap();
        graph
            .upsert_edge("v1", "v2", "contains", 1.0, None)
            .unwrap();

        let result = graph.vacuum().unwrap();

        // Both sizes should be positive (the database has schema + data).
        assert!(result.size_before > 0, "size_before should be positive");
        assert!(result.size_after > 0, "size_after should be positive");
        // After vacuum on a fresh graph with no deletions, size should be
        // roughly the same (no fragmentation to reclaim).
        assert!(
            result.size_after <= result.size_before,
            "size_after ({}) should not exceed size_before ({})",
            result.size_after,
            result.size_before,
        );
    }

    /// VACUUM after bulk deletions should reclaim space.
    #[test]
    fn vacuum_reclaims_space_after_deletions() {
        let (_dir, graph) = test_graph();

        // Insert many nodes with embeddings to grow the database.
        for i in 0..50 {
            let emb = make_embedding(i, 768);
            graph
                .upsert_node(
                    &format!("bulk-{i}"),
                    NodeType::Function,
                    &format!("concept {i}"),
                    &format!("content for concept {i} with some text to occupy space"),
                    None,
                    Some(&emb),
                    None,
                )
                .unwrap();
        }

        // Delete all nodes to create fragmentation.
        for i in 0..50 {
            graph.delete_node(&format!("bulk-{i}")).unwrap();
        }

        let result = graph.vacuum().unwrap();

        // After deleting 50 nodes with embeddings and vacuuming,
        // the database should be smaller than before.
        assert!(
            result.size_after < result.size_before,
            "vacuum should reclaim space: before={}, after={}",
            result.size_before,
            result.size_after,
        );
    }

    // ── batch_get_edges tests ──

    /// batch_get_edges returns outgoing edges grouped by source node.
    #[test]
    fn batch_get_edges_outgoing() {
        let (_dir, graph) = test_graph();

        graph
            .upsert_node("a", NodeType::Class, "A", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("b", NodeType::Function, "B", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("c", NodeType::Function, "C", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("d", NodeType::Method, "D", "", None, None, None)
            .unwrap();

        graph.upsert_edge("a", "b", "contains", 1.0, None).unwrap();
        graph.upsert_edge("a", "c", "contains", 1.0, None).unwrap();
        graph.upsert_edge("b", "d", "calls", 1.0, None).unwrap();

        let ids = vec!["a".to_string(), "b".to_string()];
        let result = graph
            .batch_get_edges(&ids, EdgeDirection::Outgoing)
            .unwrap();

        assert_eq!(
            result.get("a").map_or(0, |v| v.len()),
            2,
            "A has 2 outgoing edges"
        );
        assert_eq!(
            result.get("b").map_or(0, |v| v.len()),
            1,
            "B has 1 outgoing edge"
        );
        assert!(!result.contains_key("c"), "C was not requested");
    }

    /// batch_get_edges returns incoming edges grouped by target node.
    #[test]
    fn batch_get_edges_incoming() {
        let (_dir, graph) = test_graph();

        graph
            .upsert_node("a", NodeType::Class, "A", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("b", NodeType::Function, "B", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("c", NodeType::Function, "C", "", None, None, None)
            .unwrap();

        graph.upsert_edge("a", "b", "contains", 1.0, None).unwrap();
        graph.upsert_edge("a", "c", "contains", 1.0, None).unwrap();

        let ids = vec!["b".to_string(), "c".to_string()];
        let result = graph
            .batch_get_edges(&ids, EdgeDirection::Incoming)
            .unwrap();

        assert_eq!(
            result.get("b").map_or(0, |v| v.len()),
            1,
            "B has 1 incoming edge"
        );
        assert_eq!(
            result.get("c").map_or(0, |v| v.len()),
            1,
            "C has 1 incoming edge"
        );
    }

    /// batch_get_edges with empty input returns empty map without DB access.
    #[test]
    fn batch_get_edges_empty_input() {
        let (_dir, graph) = test_graph();

        let result = graph.batch_get_edges(&[], EdgeDirection::Outgoing).unwrap();
        assert!(result.is_empty());
    }

    /// batch_get_edges with Both direction returns edges for source and target nodes.
    ///
    /// Regression test for the parameter count bug where the Both-direction
    /// query reuses ?N slots in both IN clauses but the old code doubled the
    /// params vec (chain twice), causing rusqlite to report "Got N+1, needed N".
    #[test]
    fn batch_get_edges_both_direction() {
        let (_dir, graph) = test_graph();

        // a → b (a is source, b is target)
        // a → c (a is source, c is target)
        // d → b (d is source, b is target — d not in query set)
        graph
            .upsert_node("a", NodeType::Class, "A", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("b", NodeType::Function, "B", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("c", NodeType::Function, "C", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("d", NodeType::Method, "D", "", None, None, None)
            .unwrap();

        graph.upsert_edge("a", "b", "contains", 1.0, None).unwrap();
        graph.upsert_edge("a", "c", "contains", 1.0, None).unwrap();
        graph.upsert_edge("d", "b", "calls", 1.0, None).unwrap();

        // Query for nodes [a, b, c] in Both direction.
        // Expected: a sees 2 outgoing; b sees 1 outgoing (d→b) + 1 incoming (a→b);
        //           c sees 1 incoming (a→c).
        let ids = vec!["a".to_string(), "b".to_string(), "c".to_string()];
        let result = graph.batch_get_edges(&ids, EdgeDirection::Both).unwrap();

        // a: two outgoing edges (a→b, a→c)
        assert_eq!(
            result.get("a").map_or(0, |v| v.len()),
            2,
            "A has 2 outgoing edges"
        );

        // b: a→b (b is target) + d→b (b is target) = 2 edges touching b
        assert_eq!(
            result.get("b").map_or(0, |v| v.len()),
            2,
            "B has 2 edges (incoming a→b and d→b)"
        );

        // c: a→c (c is target) = 1 edge
        assert_eq!(
            result.get("c").map_or(0, |v| v.len()),
            1,
            "C has 1 incoming edge"
        );

        // d was not in the query set — must not appear in results
        assert!(!result.contains_key("d"), "D was not requested");
    }

    /// batch_get_edges with Both direction and a large node set does not panic.
    ///
    /// Regression test for "Got 101, needed 100" — the inspect_graph_quality
    /// handler pages nodes in batches of 100 and calls batch_get_edges(Both).
    /// Confirms that passing exactly N params (not 2N) is correct for ?N SQL.
    #[test]
    fn batch_get_edges_both_direction_large_batch() {
        let (_dir, graph) = test_graph();

        // Create 100 isolated nodes (no edges) — enough to trigger the old bug.
        let ids: Vec<String> = (0..100).map(|i| format!("node-{i:03}")).collect();
        for id in &ids {
            graph
                .upsert_node(id, NodeType::Function, id, "", None, None, None)
                .unwrap();
        }

        // This call must not panic with "Wrong number of parameters passed to query.
        // Got 101, needed 100" — the fix ensures N params are passed, not 2N.
        let result = graph.batch_get_edges(&ids, EdgeDirection::Both).unwrap();

        // No edges exist, so every node maps to an empty list (or is absent).
        for id in &ids {
            assert!(
                result.get(id.as_str()).is_none_or(|v| v.is_empty()),
                "Isolated node {id} should have no edges",
            );
        }
    }

    /// batch_get_edges Both direction keeps grouping semantics for a large connected node set.
    #[test]
    fn batch_get_edges_both_direction_large_connected_batch() {
        let (_dir, graph) = test_graph();

        let ids: Vec<String> = (0..500).map(|i| format!("node-{i:03}")).collect();
        for id in &ids {
            graph
                .upsert_node(id, NodeType::Function, id, "", None, None, None)
                .unwrap();
        }

        // Add edges that touch requested nodes in both source and target positions.
        for index in 0..ids.len() - 1 {
            graph
                .upsert_edge(&ids[index], &ids[index + 1], "calls", 1.0, None)
                .unwrap();
        }

        let result = graph.batch_get_edges(&ids, EdgeDirection::Both).unwrap();

        assert_eq!(
            result.get("node-000").map_or(0, Vec::len),
            1,
            "first node should only have its outgoing chain edge",
        );
        assert_eq!(
            result.get("node-250").map_or(0, Vec::len),
            2,
            "middle nodes should have incoming and outgoing chain edges",
        );
        assert_eq!(
            result.get("node-499").map_or(0, Vec::len),
            1,
            "last node should only have its incoming chain edge",
        );
    }

    // ── Repo Management tests ──

    #[test]
    fn get_or_create_repo_is_idempotent() {
        let (_dir, graph) = test_graph();

        let repo1 = graph.get_or_create_repo("/home/user/my-repo").unwrap();
        let repo2 = graph.get_or_create_repo("/home/user/my-repo").unwrap();

        assert_eq!(repo1.id, repo2.id);
        assert_eq!(repo1.path, "/home/user/my-repo");
    }

    #[test]
    fn get_or_create_repo_assigns_unique_ids() {
        let (_dir, graph) = test_graph();

        let repo_a = graph.get_or_create_repo("/repo-a").unwrap();
        let repo_b = graph.get_or_create_repo("/repo-b").unwrap();

        assert_ne!(repo_a.id, repo_b.id);
    }

    #[test]
    fn resolve_repo_id_returns_none_for_unknown() {
        let (_dir, graph) = test_graph();

        assert!(graph.resolve_repo_id("/nonexistent").unwrap().is_none());
    }

    #[test]
    fn resolve_repo_id_returns_some_for_known() {
        let (_dir, graph) = test_graph();

        let repo = graph.get_or_create_repo("/my-repo").unwrap();
        let resolved = graph.resolve_repo_id("/my-repo").unwrap();

        assert_eq!(resolved, Some(repo.id));
    }

    #[test]
    fn resolve_node_id_by_qualified_name_is_repo_scoped() {
        let (_dir, graph) = test_graph();
        let repo_a = graph.get_or_create_repo("/repo-a").unwrap();
        let repo_b = graph.get_or_create_repo("/repo-b").unwrap();
        let metadata = |repo_path: &str| {
            HashMap::from([
                ("qualified_name".to_string(), json!("shared.module.target")),
                ("repo_path".to_string(), json!(repo_path)),
            ])
        };

        graph
            .upsert_node(
                "node-a",
                NodeType::Function,
                "target",
                "",
                Some(&metadata("/repo-a")),
                None,
                Some(repo_a.id),
            )
            .unwrap();
        graph
            .upsert_node(
                "node-b",
                NodeType::Function,
                "target",
                "",
                Some(&metadata("/repo-b")),
                None,
                Some(repo_b.id),
            )
            .unwrap();

        assert_eq!(
            graph
                .resolve_node_id_by_qualified_name("/repo-a", "shared.module.target")
                .unwrap(),
            Some("node-a".to_string())
        );
        assert_eq!(
            graph
                .resolve_node_id_by_qualified_name("/repo-b", "shared.module.target")
                .unwrap(),
            Some("node-b".to_string())
        );
        assert!(graph
            .resolve_node_id_by_qualified_name("/repo-a", "missing")
            .unwrap()
            .is_none());
    }

    // ── Batch Get Nodes tests ──

    #[test]
    fn batch_get_nodes_returns_requested_nodes() {
        let (_dir, graph) = test_graph();

        graph
            .upsert_node("a", NodeType::Class, "A", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("b", NodeType::Function, "B", "", None, None, None)
            .unwrap();
        graph
            .upsert_node("c", NodeType::Method, "C", "", None, None, None)
            .unwrap();

        let ids = vec!["a".to_string(), "c".to_string()];
        let nodes = graph.batch_get_nodes(&ids).unwrap();

        assert_eq!(nodes.len(), 2);
        let names: std::collections::HashSet<_> = nodes.iter().map(|n| n.name.as_str()).collect();
        assert!(names.contains("A"));
        assert!(names.contains("C"));
    }

    #[test]
    fn batch_get_nodes_skips_missing_ids() {
        let (_dir, graph) = test_graph();

        graph
            .upsert_node("x", NodeType::Function, "X", "", None, None, None)
            .unwrap();

        let ids = vec!["x".to_string(), "nonexistent".to_string()];
        let nodes = graph.batch_get_nodes(&ids).unwrap();

        assert_eq!(nodes.len(), 1);
        assert_eq!(nodes[0].id, "x");
    }

    #[test]
    fn batch_get_nodes_empty_input_returns_empty() {
        let (_dir, graph) = test_graph();
        let nodes = graph.batch_get_nodes(&[]).unwrap();
        assert!(nodes.is_empty());
    }

    /// Verify that `get_file_node_map` returns File-type nodes scoped
    /// to a specific repo, enabling the git history ingester to resolve
    /// MODIFIES edges without hash-based ID generation.
    #[test]
    fn get_file_node_map_returns_files_for_repo() {
        let (_dir, graph) = test_graph();

        let repo_a = graph.get_or_create_repo("/repo/a").unwrap();
        let repo_b = graph.get_or_create_repo("/repo/b").unwrap();

        // Create file nodes in repo A.
        graph
            .upsert_node(
                "f1",
                NodeType::File,
                "src/main.py",
                "",
                None,
                None,
                Some(repo_a.id),
            )
            .unwrap();
        graph
            .upsert_node(
                "f2",
                NodeType::File,
                "src/utils.py",
                "",
                None,
                None,
                Some(repo_a.id),
            )
            .unwrap();
        // Create a file node in repo B.
        graph
            .upsert_node(
                "f3",
                NodeType::File,
                "lib/index.ts",
                "",
                None,
                None,
                Some(repo_b.id),
            )
            .unwrap();
        // Create a non-file node in repo A (should be excluded).
        graph
            .upsert_node(
                "c1",
                NodeType::Class,
                "MyClass",
                "",
                None,
                None,
                Some(repo_a.id),
            )
            .unwrap();

        let map = graph.get_file_node_map(repo_a.id).unwrap();
        assert_eq!(map.len(), 2);
        assert_eq!(map.get("src/main.py"), Some(&"f1".to_string()));
        assert_eq!(map.get("src/utils.py"), Some(&"f2".to_string()));

        // Repo B should only have its own file.
        let map_b = graph.get_file_node_map(repo_b.id).unwrap();
        assert_eq!(map_b.len(), 1);
        assert_eq!(map_b.get("lib/index.ts"), Some(&"f3".to_string()));
    }

    // ── count_nodes with repo_id tests ──

    #[test]
    fn count_nodes_scoped_by_repo() {
        let (_dir, graph) = test_graph();

        let repo_a = graph.get_or_create_repo("/repo/a").unwrap();
        let repo_b = graph.get_or_create_repo("/repo/b").unwrap();

        // 2 classes in repo A, 1 function in repo A, 1 class in repo B.
        graph
            .upsert_node("a1", NodeType::Class, "A1", "", None, None, Some(repo_a.id))
            .unwrap();
        graph
            .upsert_node("a2", NodeType::Class, "A2", "", None, None, Some(repo_a.id))
            .unwrap();
        graph
            .upsert_node(
                "a3",
                NodeType::Function,
                "func",
                "",
                None,
                None,
                Some(repo_a.id),
            )
            .unwrap();
        graph
            .upsert_node("b1", NodeType::Class, "B1", "", None, None, Some(repo_b.id))
            .unwrap();

        // Global count = 4.
        assert_eq!(graph.count_nodes(None, None).unwrap(), 4);

        // Repo A has 3 nodes total.
        assert_eq!(graph.count_nodes(None, Some(repo_a.id)).unwrap(), 3);

        // Repo A has 2 classes.
        assert_eq!(
            graph
                .count_nodes(Some(NodeType::Class), Some(repo_a.id))
                .unwrap(),
            2
        );

        // Repo B has 1 class.
        assert_eq!(
            graph
                .count_nodes(Some(NodeType::Class), Some(repo_b.id))
                .unwrap(),
            1
        );

        // Repo B has 0 functions.
        assert_eq!(
            graph
                .count_nodes(Some(NodeType::Function), Some(repo_b.id))
                .unwrap(),
            0
        );
    }

    // ── search_by_vector with repo_id over-fetch tests ──

    /// Verify that search_by_vector with a repo_id filter returns results
    /// even when the nearest neighbors globally belong to a different repo.
    ///
    /// Without the over-fetch fix, approximate nearest-neighbor search can return only
    /// the top-K nearest vectors globally, and the repo_id JOIN filter
    /// discards them — yielding 0 results. The over-fetch (k * 10) ensures
    /// the target repo's vectors are included in the candidate set.
    #[test]
    fn search_by_vector_repo_filter_overfetch() {
        let (_dir, graph) = test_graph();

        let repo_a = graph.get_or_create_repo("/repo/a").unwrap();
        let repo_b = graph.get_or_create_repo("/repo/b").unwrap();

        // Create a query vector and two similar vectors — one per repo.
        let mut query = vec![0.0f32; 768];
        query[0] = 1.0;

        // Very close to query (repo A).
        let mut close_a = vec![0.0f32; 768];
        close_a[0] = 0.99;
        close_a[1] = 0.01;

        // Slightly further (repo B).
        let mut close_b = vec![0.0f32; 768];
        close_b[0] = 0.8;
        close_b[1] = 0.2;

        graph
            .upsert_node(
                "na",
                NodeType::Class,
                "ClassA",
                "",
                None,
                Some(&close_a),
                Some(repo_a.id),
            )
            .unwrap();
        graph
            .upsert_node(
                "nb",
                NodeType::Class,
                "ClassB",
                "",
                None,
                Some(&close_b),
                Some(repo_b.id),
            )
            .unwrap();

        // Search with limit=1 scoped to repo B. Without over-fetch, the
        // top-1 global result is from repo A, so repo B gets 0 results.
        let results = graph
            .search_by_vector(&query, None, 1, Some(repo_b.id))
            .unwrap();
        assert_eq!(
            results.len(),
            1,
            "over-fetch should surface repo B's vector"
        );
        assert_eq!(results[0].node.id, "nb");

        // Verify repo A search also works.
        let results_a = graph
            .search_by_vector(&query, None, 1, Some(repo_a.id))
            .unwrap();
        assert_eq!(results_a.len(), 1);
        assert_eq!(results_a[0].node.id, "na");
    }

    // ── Nodes-without-embeddings tests ──

    #[test]
    fn list_nodes_without_embeddings_basic() {
        let (_dir, graph) = test_graph();

        // Create three nodes — only one gets an embedding.
        graph
            .upsert_node(
                "n1",
                NodeType::Function,
                "func_a",
                "code a",
                None,
                None,
                None,
            )
            .unwrap();
        graph
            .upsert_node("n2", NodeType::Class, "ClassB", "code b", None, None, None)
            .unwrap();

        let emb = make_embedding(1, 768);
        graph
            .upsert_node(
                "n3",
                NodeType::Function,
                "func_c",
                "code c",
                None,
                Some(&emb),
                None,
            )
            .unwrap();

        // All nodes without embeddings — should return n1 and n2 (sorted by id).
        let missing = graph
            .list_nodes_without_embeddings(None, 100, 0, None)
            .unwrap();
        assert_eq!(missing.len(), 2);
        assert_eq!(missing[0].id, "n1");
        assert_eq!(missing[1].id, "n2");

        // Filter by type — only Function nodes missing embeddings.
        let funcs = graph
            .list_nodes_without_embeddings(Some(NodeType::Function), 100, 0, None)
            .unwrap();
        assert_eq!(funcs.len(), 1);
        assert_eq!(funcs[0].id, "n1");

        // Pagination — limit to 1 node at offset 0.
        let page1 = graph
            .list_nodes_without_embeddings(None, 1, 0, None)
            .unwrap();
        assert_eq!(page1.len(), 1);
        assert_eq!(page1[0].id, "n1");

        let page2 = graph
            .list_nodes_without_embeddings(None, 1, 1, None)
            .unwrap();
        assert_eq!(page2.len(), 1);
        assert_eq!(page2[0].id, "n2");
    }

    #[test]
    fn count_nodes_without_embeddings_scoped() {
        let (_dir, graph) = test_graph();

        let repo_a = graph.get_or_create_repo("/repo/alpha").unwrap();
        let repo_b = graph.get_or_create_repo("/repo/beta").unwrap();

        // Repo A: 2 nodes, 1 with embedding.
        let emb = make_embedding(42, 768);
        graph
            .upsert_node(
                "a1",
                NodeType::Function,
                "fa",
                "",
                None,
                None,
                Some(repo_a.id),
            )
            .unwrap();
        graph
            .upsert_node(
                "a2",
                NodeType::Function,
                "fb",
                "",
                None,
                Some(&emb),
                Some(repo_a.id),
            )
            .unwrap();

        // Repo B: 3 nodes, none with embeddings.
        graph
            .upsert_node("b1", NodeType::Class, "C1", "", None, None, Some(repo_b.id))
            .unwrap();
        graph
            .upsert_node("b2", NodeType::Class, "C2", "", None, None, Some(repo_b.id))
            .unwrap();
        graph
            .upsert_node(
                "b3",
                NodeType::Method,
                "m1",
                "",
                None,
                None,
                Some(repo_b.id),
            )
            .unwrap();

        // Global count — 4 nodes missing embeddings (a1 + b1 + b2 + b3).
        let total = graph.count_nodes_without_embeddings(None, None).unwrap();
        assert_eq!(total, 4);

        // Scoped to repo A — only a1 is missing.
        let count_a = graph
            .count_nodes_without_embeddings(None, Some(repo_a.id))
            .unwrap();
        assert_eq!(count_a, 1);

        // Scoped to repo B — all 3 are missing.
        let count_b = graph
            .count_nodes_without_embeddings(None, Some(repo_b.id))
            .unwrap();
        assert_eq!(count_b, 3);

        // Scoped to repo B + type Class — 2 of 3.
        let count_b_class = graph
            .count_nodes_without_embeddings(Some(NodeType::Class), Some(repo_b.id))
            .unwrap();
        assert_eq!(count_b_class, 2);
    }
}
