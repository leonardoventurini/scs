//! PyO3 Python bindings for the SCS knowledge graph.
//!
//! Provides private parser and graph primitives consumed by SCS's typed
//! Python adapters. Complex values cross the boundary as JSON strings so
//! native storage models remain independent of Pydantic.
//!
//! # Python Usage
//!
//! ```python
//! import _scs_native
//!
//! graph = _scs_native.KnowledgeGraph("/path/to/index.db")
//! result_json = graph.upsert_node("id", "function", "name", "content", {"key": "val"})
//! node = json.loads(result_json)
//! ```

#[cfg(feature = "python")]
use std::collections::HashMap;

#[cfg(feature = "python")]
use pyo3::exceptions::PyRuntimeError;
#[cfg(feature = "python")]
use pyo3::prelude::*;
#[cfg(feature = "python")]
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};

#[cfg(feature = "python")]
use scs_core::node_types::{NodeType, RelationshipType};
#[cfg(feature = "python")]
use scs_core::SCSConfig;
#[cfg(feature = "python")]
use scs_store::KnowledgeGraph as RustKnowledgeGraph;
#[cfg(feature = "python")]
use scs_store::{EdgeDirection, TraversalDirection};
#[cfg(feature = "python")]
use strum::IntoEnumIterator;

// ── Helpers ─────────────────────────────────────────────────────────

/// Convert a `PyAny` value to `serde_json::Value`, preserving type information.
///
/// The original FFI coerced everything to strings via `v.str()`, losing
/// bool/int/float/list/dict types. This recursive converter preserves the
/// full type hierarchy so metadata round-trips through Rust unchanged.
#[cfg(feature = "python")]
fn py_to_json(obj: &Bound<'_, pyo3::types::PyAny>) -> serde_json::Value {
    // Order matters: PyBool must be checked before PyInt because
    // Python's `bool` is a subclass of `int`.
    if obj.is_none() {
        serde_json::Value::Null
    } else if obj.is_instance_of::<PyBool>() {
        serde_json::Value::Bool(obj.extract::<bool>().unwrap_or(false))
    } else if obj.is_instance_of::<PyInt>() {
        match obj.extract::<i64>() {
            Ok(i) => serde_json::Value::Number(i.into()),
            Err(_) => serde_json::Value::Null,
        }
    } else if obj.is_instance_of::<PyFloat>() {
        match obj.extract::<f64>() {
            Ok(f) => serde_json::Number::from_f64(f)
                .map(serde_json::Value::Number)
                .unwrap_or(serde_json::Value::Null),
            Err(_) => serde_json::Value::Null,
        }
    } else if obj.is_instance_of::<PyString>() {
        match obj.extract::<String>() {
            Ok(s) => serde_json::Value::String(s),
            Err(_) => serde_json::Value::Null,
        }
    } else if obj.is_instance_of::<PyList>() {
        let list = obj.downcast::<PyList>().unwrap();
        let arr: Vec<serde_json::Value> = list.iter().map(|item| py_to_json(&item)).collect();
        serde_json::Value::Array(arr)
    } else if obj.is_instance_of::<PyDict>() {
        let dict = obj.downcast::<PyDict>().unwrap();
        let mut map = serde_json::Map::new();
        for (k, v) in dict.iter() {
            if let Ok(key) = k.extract::<String>() {
                map.insert(key, py_to_json(&v));
            }
        }
        serde_json::Value::Object(map)
    } else {
        // Fallback: stringify unknown types.
        match obj.str() {
            Ok(s) => serde_json::Value::String(s.to_string()),
            Err(_) => serde_json::Value::Null,
        }
    }
}

/// Convert a Python dict to a Rust HashMap with proper JSON value types.
#[cfg(feature = "python")]
fn pydict_to_hashmap(dict: &Bound<'_, PyDict>) -> HashMap<String, serde_json::Value> {
    let mut map = HashMap::new();
    for (k, v) in dict.iter() {
        if let Ok(key) = k.extract::<String>() {
            map.insert(key, py_to_json(&v));
        }
    }
    map
}

/// Shorthand for converting SCSError → PyRuntimeError.
#[cfg(feature = "python")]
fn scs_err(e: scs_core::SCSError) -> PyErr {
    pyo3::exceptions::PyRuntimeError::new_err(e.to_string())
}

/// Shorthand for converting serde_json::Error → PyRuntimeError.
#[cfg(feature = "python")]
fn json_err(e: serde_json::Error) -> PyErr {
    pyo3::exceptions::PyRuntimeError::new_err(e.to_string())
}

/// Parse an edge direction string from Python into the Rust enum.
///
/// Accepts `"outgoing"`, `"incoming"`, `"both"` (case-insensitive).
/// Defaults to `Both` for unrecognized values.
#[cfg(feature = "python")]
fn parse_edge_direction(s: &str) -> EdgeDirection {
    match s.to_lowercase().as_str() {
        "outgoing" => EdgeDirection::Outgoing,
        "incoming" => EdgeDirection::Incoming,
        _ => EdgeDirection::Both,
    }
}

/// Parse a traversal direction string from Python into the Rust enum.
///
/// Accepts `"outgoing"`, `"incoming"` (case-insensitive).
/// Defaults to `Outgoing` for unrecognized values.
#[cfg(feature = "python")]
fn parse_traversal_direction(s: &str) -> TraversalDirection {
    match s.to_lowercase().as_str() {
        "incoming" => TraversalDirection::Incoming,
        _ => TraversalDirection::Outgoing,
    }
}

/// Serialize a value to a JSON string and wrap it in a `PyObject`.
///
/// Uses `IntoPyObject` (PyO3 0.23+) instead of the deprecated `into_py`.
#[cfg(feature = "python")]
fn to_json_pyobject<T: serde::Serialize>(py: Python<'_>, value: &T) -> PyResult<PyObject> {
    let json = serde_json::to_string(value).map_err(json_err)?;
    Ok(json.into_pyobject(py)?.into_any().unbind())
}

// ── KnowledgeGraph ──────────────────────────────────────────────────

/// Python-accessible knowledge graph backed by SCS's Rust engine.
///
/// This class provides the same API surface as the Python `KnowledgeGraph`
/// class, enabling a seamless migration path from Python to Rust. All
/// methods that return complex data use JSON strings as the interchange
/// format — the Python wrapper converts these to Pydantic models.
#[cfg(feature = "python")]
#[pyclass(name = "KnowledgeGraph")]
pub struct PyKnowledgeGraph {
    inner: RustKnowledgeGraph,
}

#[cfg(feature = "python")]
#[pymethods]
impl PyKnowledgeGraph {
    /// Create a new KnowledgeGraph instance.
    ///
    /// Opens the database, initializes the current schema, and returns a fully ready graph.
    ///
    /// # Arguments
    /// * `db_path` — Path to the SQLite database file.
    /// * `embedding_dim` — Embedding vector dimension (default: 768).
    #[new]
    #[pyo3(signature = (db_path, embedding_dim=768))]
    fn new(py: Python<'_>, db_path: &str, embedding_dim: usize) -> PyResult<Self> {
        let db_path_buf: std::path::PathBuf = db_path.into();
        let config = SCSConfig {
            index_path: db_path_buf.with_extension("usearch"),
            db_path: db_path_buf,
            embedding_dim,
            // 8 connections: leaves headroom for wire server health/status
            // polls while indexing and embedding queries run in background.
            pool_size: 8,
        };

        let graph = py
            .allow_threads(|| RustKnowledgeGraph::open(config))
            .map_err(scs_err)?;
        Ok(Self { inner: graph })
    }

    // ── Repo Management ──────────────────────────────────────────

    /// Get or create a repo record, returning its integer ID.
    ///
    /// Idempotent: calling with the same path always returns the same ID.
    /// Used by the ingestion pipeline to obtain the FK for new nodes.
    fn get_or_create_repo(&self, py: Python<'_>, path: &str) -> PyResult<i64> {
        let repo = py
            .allow_threads(|| self.inner.get_or_create_repo(path))
            .map_err(scs_err)?;
        Ok(repo.id)
    }

    /// Look up a repo by path, returning its ID or None if unknown.
    ///
    /// Read-only — doesn't create a new repo record. Used by search
    /// methods to resolve a user-provided path to a `repo_id` filter.
    fn resolve_repo_id(&self, py: Python<'_>, path: &str) -> PyResult<Option<i64>> {
        py.allow_threads(|| self.inner.resolve_repo_id(path))
            .map_err(scs_err)
    }

    /// Look up a repo by integer ID, returning its path or None if unknown.
    ///
    /// Reverse of `resolve_repo_id` — used to surface repo paths in the
    /// frontend from the integer FK stored on nodes.
    fn resolve_repo_path(&self, py: Python<'_>, repo_id: i64) -> PyResult<Option<String>> {
        py.allow_threads(|| self.inner.resolve_repo_path(repo_id))
            .map_err(scs_err)
    }

    /// Return `{name: id}` for all File-type nodes in a repo.
    ///
    /// Used by the git history ingester to resolve MODIFIES edges without
    /// relying on hash-based ID generation (which historically mismatched
    /// the code pipeline's scheme). A single query replaces N per-file lookups.
    fn get_file_node_map(&self, py: Python<'_>, repo_id: i64) -> PyResult<PyObject> {
        let map = py
            .allow_threads(|| self.inner.get_file_node_map(repo_id))
            .map_err(scs_err)?;
        let json = serde_json::to_string(&map).map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("JSON serialization error: {e}"))
        })?;
        Ok(json.into_pyobject(py)?.into_any().unbind())
    }

    // ── Node CRUD ────────────────────────────────────────────────

    /// Insert or update a node, optionally with an embedding vector.
    #[pyo3(signature = (node_id, node_type, name, content="", metadata=None, embedding=None, repo_id=None))]
    #[allow(clippy::too_many_arguments)] // Mirrors the typed Python graph contract.
    fn upsert_node(
        &self,
        py: Python<'_>,
        node_id: &str,
        node_type: &str,
        name: &str,
        content: &str,
        metadata: Option<&Bound<'_, PyDict>>,
        embedding: Option<Vec<f32>>,
        repo_id: Option<i64>,
    ) -> PyResult<PyObject> {
        let nt: NodeType = node_type.parse().map_err(|_| {
            pyo3::exceptions::PyValueError::new_err(format!("invalid node type: {node_type}"))
        })?;

        let meta = metadata.map(pydict_to_hashmap);
        let emb_ref = embedding.as_deref();

        let node = py
            .allow_threads(|| {
                self.inner
                    .upsert_node(node_id, nt, name, content, meta.as_ref(), emb_ref, repo_id)
            })
            .map_err(scs_err)?;

        to_json_pyobject(py, &node)
    }

    /// Get a node by ID. Returns JSON string or None.
    ///
    /// GIL released during the query.
    fn get_node(&self, py: Python<'_>, node_id: &str) -> PyResult<Option<PyObject>> {
        let node = py
            .allow_threads(|| self.inner.get_node(node_id))
            .map_err(scs_err)?;
        match node {
            Some(n) => Ok(Some(to_json_pyobject(py, &n)?)),
            None => Ok(None),
        }
    }

    /// Delete a node and its edges/embedding. Returns whether it existed.
    fn delete_node(&self, py: Python<'_>, node_id: &str) -> PyResult<bool> {
        py.allow_threads(|| self.inner.delete_node(node_id))
            .map_err(scs_err)
    }

    /// Bulk-delete nodes matching a type + metadata key/value filter.
    ///
    /// Far faster than iterating in Python for large node sets — everything
    /// stays in SQLite as two bulk DELETE statements.
    fn delete_nodes_by_metadata(
        &self,
        py: Python<'_>,
        node_type: &str,
        metadata_key: &str,
        metadata_value: &str,
    ) -> PyResult<usize> {
        let nt: NodeType = node_type.parse().map_err(|_| {
            pyo3::exceptions::PyValueError::new_err(format!("unknown node type: {node_type}"))
        })?;
        py.allow_threads(|| {
            self.inner
                .delete_nodes_by_metadata(&nt, metadata_key, metadata_value)
        })
        .map_err(scs_err)
    }

    /// List nodes, optionally filtered by type and/or repo. Returns JSON array string.
    ///
    /// GIL released during the query so concurrent `asyncio.to_thread` callers
    /// (stats, status polls) aren't blocked during heavy background work.
    #[pyo3(signature = (node_type=None, limit=100, offset=0, repo_id=None))]
    fn list_nodes(
        &self,
        py: Python<'_>,
        node_type: Option<&str>,
        limit: i64,
        offset: i64,
        repo_id: Option<i64>,
    ) -> PyResult<PyObject> {
        let nt: Option<NodeType> = node_type.and_then(|t| t.parse().ok());
        let nodes = py
            .allow_threads(|| self.inner.list_nodes(nt, limit, offset, repo_id))
            .map_err(scs_err)?;
        to_json_pyobject(py, &nodes)
    }

    /// Count nodes, optionally filtered by type and/or repo.
    ///
    /// When `repo_id` is provided, the count is scoped to that repository —
    /// critical for `list_symbols` where the total must match the filtered node set.
    /// GIL released so concurrent threads aren't blocked.
    #[pyo3(signature = (node_type=None, repo_id=None))]
    fn count_nodes(
        &self,
        py: Python<'_>,
        node_type: Option<&str>,
        repo_id: Option<i64>,
    ) -> PyResult<i64> {
        let nt: Option<NodeType> = node_type.and_then(|t| t.parse().ok());
        py.allow_threads(|| self.inner.count_nodes(nt, repo_id))
            .map_err(scs_err)
    }

    /// Count nodes grouped by type in a single SQL query.
    ///
    /// Returns a JSON object mapping node type strings to counts. Types with
    /// zero nodes are omitted. Single GROUP BY query — O(1) vs O(n_types)
    /// sequential `count_nodes` calls. Used by `knowledge.stats` to replace
    /// per-type COUNT queries with one batch query.
    ///
    /// When `repo_id` is provided, only nodes belonging to that repo are
    /// counted, scoping the report to a single repository. Pass `None` (the
    /// default) for a cross-repo count over all nodes.
    ///
    /// The GIL is released via `py.allow_threads` so concurrent
    /// `asyncio.to_thread` callers can run in parallel.
    #[pyo3(signature = (repo_id=None))]
    fn count_nodes_by_type(&self, py: Python<'_>, repo_id: Option<i64>) -> PyResult<String> {
        let counts = py
            .allow_threads(|| self.inner.count_nodes_by_type(repo_id))
            .map_err(scs_err)?;
        serde_json::to_string(&counts).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    /// Count embedding vectors stored in the graph.
    fn count_embeddings(&self, py: Python<'_>) -> PyResult<i64> {
        py.allow_threads(|| self.inner.count_embeddings())
            .map_err(scs_err)
    }

    /// Return recent storage operation observability events and summaries.
    #[pyo3(signature = (limit=200, backend=None, min_duration_ms=None, status=None))]
    fn query_observability_snapshot(
        &self,
        py: Python<'_>,
        limit: usize,
        backend: Option<&str>,
        min_duration_ms: Option<f64>,
        status: Option<&str>,
    ) -> PyResult<PyObject> {
        let filter = scs_store::observability::QuerySnapshotFilter {
            limit: Some(limit),
            backend: backend.and_then(|value| value.parse().ok()),
            min_duration_ms,
            status: status.and_then(|value| value.parse().ok()),
        };
        let snapshot = py.allow_threads(|| scs_store::observability::snapshot(filter));
        to_json_pyobject(py, &snapshot)
    }

    /// Clear retained storage operation observability events.
    fn clear_query_observability(&self, py: Python<'_>) -> PyResult<()> {
        py.allow_threads(scs_store::observability::clear);
        Ok(())
    }

    /// List nodes that have no embedding vector. Returns JSON array string.
    ///
    /// Used by the background embedding generator to discover nodes needing
    /// embedding computation. GIL is released during the query so concurrent
    /// Python threads (e.g., dictation pipeline) aren't blocked.
    #[pyo3(signature = (node_type=None, limit=100, offset=0, repo_id=None))]
    fn list_nodes_without_embeddings(
        &self,
        py: Python<'_>,
        node_type: Option<&str>,
        limit: i64,
        offset: i64,
        repo_id: Option<i64>,
    ) -> PyResult<PyObject> {
        let nt: Option<NodeType> = node_type.and_then(|t| t.parse().ok());
        let nodes = py
            .allow_threads(|| {
                self.inner
                    .list_nodes_without_embeddings(nt, limit, offset, repo_id)
            })
            .map_err(scs_err)?;
        to_json_pyobject(py, &nodes)
    }

    /// Count nodes that have no embedding vector.
    ///
    /// Optionally scoped by type and/or repo. GIL released during the query.
    #[pyo3(signature = (node_type=None, repo_id=None))]
    fn count_nodes_without_embeddings(
        &self,
        py: Python<'_>,
        node_type: Option<&str>,
        repo_id: Option<i64>,
    ) -> PyResult<i64> {
        let nt: Option<NodeType> = node_type.and_then(|t| t.parse().ok());
        py.allow_threads(|| self.inner.count_nodes_without_embeddings(nt, repo_id))
            .map_err(scs_err)
    }

    /// Search nodes by name (case-insensitive substring), optionally scoped to a repo.
    /// Returns JSON array string.
    #[pyo3(signature = (name, node_type=None, limit=20, repo_id=None))]
    fn search_by_name(
        &self,
        py: Python<'_>,
        name: &str,
        node_type: Option<&str>,
        limit: i64,
        repo_id: Option<i64>,
    ) -> PyResult<PyObject> {
        let nt: Option<NodeType> = node_type.and_then(|t| t.parse().ok());
        let nodes = py
            .allow_threads(|| self.inner.search_by_name(name, nt, limit, repo_id))
            .map_err(scs_err)?;
        to_json_pyobject(py, &nodes)
    }

    // ── Edge CRUD ────────────────────────────────────────────────

    /// Insert or update a directed edge. Returns JSON string.
    ///
    /// The relationship must be one of the exported code-only relationship types.
    #[pyo3(signature = (source_id, target_id, relationship, weight=1.0, metadata=None))]
    fn upsert_edge(
        &self,
        py: Python<'_>,
        source_id: &str,
        target_id: &str,
        relationship: &str,
        weight: f64,
        metadata: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<PyObject> {
        let relationship: RelationshipType = relationship.parse().map_err(|_| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "invalid relationship type: {relationship}"
            ))
        })?;
        let relationship = relationship.to_string();
        let meta = metadata.map(pydict_to_hashmap);
        let edge = py
            .allow_threads(|| {
                self.inner
                    .upsert_edge(source_id, target_id, &relationship, weight, meta.as_ref())
            })
            .map_err(scs_err)?;

        to_json_pyobject(py, &edge)
    }

    /// Get edges connected to a node. Returns JSON array string.
    #[pyo3(signature = (node_id, relationship=None, direction="both"))]
    fn get_edges(
        &self,
        py: Python<'_>,
        node_id: &str,
        relationship: Option<&str>,
        direction: &str,
    ) -> PyResult<PyObject> {
        let dir = parse_edge_direction(direction);

        let edges = py
            .allow_threads(|| self.inner.get_edges(node_id, relationship, dir))
            .map_err(scs_err)?;

        to_json_pyobject(py, &edges)
    }

    /// Get edges for multiple nodes in a single query. Returns JSON object
    /// mapping `node_id → [Edge]`.
    ///
    /// Replaces N individual `get_edges` calls with one SQL IN-clause query,
    /// reducing connection pool pressure for large repository traversals.
    #[pyo3(signature = (node_ids, direction="both"))]
    fn batch_get_edges(
        &self,
        py: Python<'_>,
        node_ids: Vec<String>,
        direction: &str,
    ) -> PyResult<PyObject> {
        let dir = parse_edge_direction(direction);

        let result = py
            .allow_threads(|| self.inner.batch_get_edges(&node_ids, dir))
            .map_err(scs_err)?;

        to_json_pyobject(py, &result)
    }

    /// Delete an edge by ID. Returns whether it existed.
    fn delete_edge(&self, py: Python<'_>, edge_id: &str) -> PyResult<bool> {
        py.allow_threads(|| self.inner.delete_edge(edge_id))
            .map_err(scs_err)
    }

    // ── Vector Search ────────────────────────────────────────────

    /// Find nodes most similar to a query vector, optionally scoped to a repo.
    /// Returns JSON array string.
    #[pyo3(signature = (query_embedding, node_type=None, limit=10, repo_id=None))]
    fn search_by_vector(
        &self,
        py: Python<'_>,
        query_embedding: Vec<f32>,
        node_type: Option<&str>,
        limit: i64,
        repo_id: Option<i64>,
    ) -> PyResult<PyObject> {
        let nt: Option<NodeType> = node_type.and_then(|t| t.parse().ok());
        // Release the GIL because ANN work scales with the active index.
        let results = py
            .allow_threads(|| {
                self.inner
                    .search_by_vector(&query_embedding, nt, limit, repo_id)
            })
            .map_err(scs_err)?;

        to_json_pyobject(py, &results)
    }

    // ── Graph Traversal ──────────────────────────────────────────

    /// Get immediate neighbor nodes. Returns JSON array string.
    #[pyo3(signature = (node_id, relationship=None, direction="outgoing", limit=50))]
    fn get_neighbors(
        &self,
        py: Python<'_>,
        node_id: &str,
        relationship: Option<&str>,
        direction: &str,
        limit: i64,
    ) -> PyResult<PyObject> {
        let dir = parse_edge_direction(direction);

        let nodes = py
            .allow_threads(|| self.inner.get_neighbors(node_id, relationship, dir, limit))
            .map_err(scs_err)?;

        to_json_pyobject(py, &nodes)
    }

    /// Recursive graph traversal with cycle detection. Returns JSON array string.
    #[pyo3(signature = (start_node_id, max_depth=2, relationship=None, direction="outgoing"))]
    fn traverse(
        &self,
        py: Python<'_>,
        start_node_id: &str,
        max_depth: i32,
        relationship: Option<&str>,
        direction: &str,
    ) -> PyResult<PyObject> {
        let dir = parse_traversal_direction(direction);
        // Release the GIL: recursive traversal can visit many nodes across
        // multiple SQL queries (depth * fan-out). MCP graph_context and
        // get_related calls hold this for 50–500ms on large graphs, blocking
        // the dictation thread pool.
        let results = py
            .allow_threads(|| {
                self.inner
                    .traverse(start_node_id, max_depth, relationship, dir)
            })
            .map_err(scs_err)?;

        to_json_pyobject(py, &results)
    }

    // ── Graph RAG ────────────────────────────────────────────────

    /// Combined vector search + graph traversal for RAG context, optionally scoped
    /// to a repo for the vector search pass. Returns JSON string.
    #[pyo3(signature = (query_embedding, node_type=None, vector_limit=10, hop_limit=2, relationship=None, repo_id=None))]
    #[allow(clippy::too_many_arguments)] // Keeps semantic query options explicit.
    fn graph_rag_query(
        &self,
        py: Python<'_>,
        query_embedding: Vec<f32>,
        node_type: Option<&str>,
        vector_limit: i64,
        hop_limit: i32,
        relationship: Option<&str>,
        repo_id: Option<i64>,
    ) -> PyResult<PyObject> {
        let nt: Option<NodeType> = node_type.and_then(|t| t.parse().ok());

        // Release the GIL: graph_rag_query combines vector search with
        // recursive graph traversal — the most expensive combined operation
        // in the MCP plugin. Can run 200ms–2s on large graphs while holding the
        // GIL, completely blocking the dictation personalization step.
        let result = py
            .allow_threads(|| {
                self.inner.graph_rag_query(
                    &query_embedding,
                    nt,
                    vector_limit,
                    hop_limit,
                    relationship,
                    repo_id,
                )
            })
            .map_err(scs_err)?;

        to_json_pyobject(py, &result)
    }

    // ── Batch Operations ─────────────────────────────────────────

    /// Get multiple nodes by ID in a single query. Returns JSON array string.
    ///
    /// Replaces N individual `get_node` calls with one SQL IN-clause query,
    /// eliminating per-node FFI round-trips in performance-critical handlers.
    fn batch_get_nodes(&self, py: Python<'_>, node_ids: Vec<String>) -> PyResult<PyObject> {
        let nodes = py
            .allow_threads(|| self.inner.batch_get_nodes(&node_ids))
            .map_err(scs_err)?;
        to_json_pyobject(py, &nodes)
    }

    /// Bulk upsert nodes from a JSON array string. Returns count of rows affected.
    ///
    /// The GIL is released via `py.allow_threads` so concurrent library
    /// ingestion workers (ThreadPoolExecutor) don't serialize on the GIL.
    /// JSON deserialization and SQLite writes both happen outside the GIL.
    fn batch_upsert_nodes(&self, py: Python<'_>, nodes_json: &str) -> PyResult<usize> {
        let nodes: Vec<scs_store::batch::BatchNode> =
            serde_json::from_str(nodes_json).map_err(json_err)?;

        let pool = self.inner.pool();
        py.allow_threads(|| scs_store::batch::batch_upsert_nodes(pool, &nodes))
            .map_err(scs_err)
    }

    /// Bulk upsert embeddings from a JSON array of [node_id, [f32...]] pairs. Returns count.
    ///
    /// The GIL is released via `py.allow_threads` so concurrent library
    /// ingestion workers (ThreadPoolExecutor) don't serialize on the GIL.
    fn batch_upsert_embeddings(&self, py: Python<'_>, embeddings_json: &str) -> PyResult<usize> {
        let pairs: Vec<(String, Vec<f32>)> =
            serde_json::from_str(embeddings_json).map_err(json_err)?;

        let vector_index = self.inner.vector_index();
        py.allow_threads(|| scs_store::batch::batch_upsert_embeddings(vector_index, &pairs))
            .map_err(scs_err)
    }

    /// Persist pending vector-index writes if the USearch sidecar is dirty.
    ///
    /// The GIL is released because a dirty flush rewrites the full sidecar.
    fn flush_vector_index(&self, py: Python<'_>) -> PyResult<bool> {
        py.allow_threads(|| self.inner.flush_vector_index())
            .map_err(scs_err)
    }

    /// Bulk upsert edges from a JSON array string. Returns count of rows affected.
    ///
    /// The GIL is released via `py.allow_threads` so concurrent library
    /// ingestion workers (ThreadPoolExecutor) don't serialize on the GIL.
    fn batch_upsert_edges(&self, py: Python<'_>, edges_json: &str) -> PyResult<usize> {
        let edges: Vec<scs_store::batch::BatchEdge> =
            serde_json::from_str(edges_json).map_err(json_err)?;

        let pool = self.inner.pool();
        py.allow_threads(|| scs_store::batch::batch_upsert_edges(pool, &edges))
            .map_err(scs_err)
    }

    // ── Ingestion File Tracking ──────────────────────────────────

    /// Get the content hash of a previously ingested file. Returns string or None.
    fn get_ingested_file_hash(
        &self,
        py: Python<'_>,
        repo_path: &str,
        rel_path: &str,
    ) -> PyResult<Option<String>> {
        let pool = self.inner.pool();
        py.allow_threads(|| {
            scs_store::ingestion_files::get_ingested_file_hash(pool, repo_path, rel_path)
        })
        .map_err(scs_err)
    }

    /// Record or update a file's ingestion metadata.
    #[allow(clippy::too_many_arguments)] // Preserves the ingestion record contract.
    fn upsert_ingested_file(
        &self,
        py: Python<'_>,
        file_id: &str,
        repo_path: &str,
        rel_path: &str,
        language: &str,
        content_hash: &str,
        byte_size: i64,
    ) -> PyResult<()> {
        let pool = self.inner.pool();
        py.allow_threads(|| {
            scs_store::ingestion_files::upsert_ingested_file(
                pool,
                file_id,
                repo_path,
                rel_path,
                language,
                content_hash,
                byte_size,
            )
        })
        .map_err(scs_err)
    }

    /// Get all ingested file paths and hashes for a repo. Returns JSON object string.
    fn get_all_ingested_files(&self, py: Python<'_>, repo_path: &str) -> PyResult<PyObject> {
        let pool = self.inner.pool();
        let files = py
            .allow_threads(|| scs_store::ingestion_files::get_all_ingested_files(pool, repo_path))
            .map_err(scs_err)?;

        to_json_pyobject(py, &files)
    }

    /// Get per-repo ingestion stats. Returns JSON object string.
    ///
    /// GIL released during the query so stats polling doesn't block during
    /// heavy background embedding generation.
    fn get_ingestion_stats(&self, py: Python<'_>) -> PyResult<PyObject> {
        let pool = self.inner.pool();
        let stats = py
            .allow_threads(|| scs_store::ingestion_files::get_ingestion_stats(pool))
            .map_err(scs_err)?;

        // Convert IngestionStats to a JSON-friendly format matching Python's API.
        let result: HashMap<String, serde_json::Value> = stats
            .into_iter()
            .map(|(repo, s)| {
                let mut map = serde_json::Map::new();
                map.insert(
                    "file_count".into(),
                    serde_json::Value::Number(s.file_count.into()),
                );
                map.insert(
                    "last_indexed".into(),
                    serde_json::Value::String(s.last_indexed),
                );
                (repo, serde_json::Value::Object(map))
            })
            .collect();

        to_json_pyobject(py, &result)
    }

    /// Remove an ingested file record and its associated nodes.
    ///
    /// Releases the GIL because stale cleanup can delete many files and each
    /// delete may trigger SQLite WAL checkpoint work.
    fn delete_ingested_file(
        &self,
        py: Python<'_>,
        repo_path: &str,
        rel_path: &str,
    ) -> PyResult<()> {
        let pool = self.inner.pool();
        let vector_index = self.inner.vector_index();
        py.allow_threads(|| {
            scs_store::ingestion_files::delete_ingested_file(
                pool,
                vector_index,
                repo_path,
                rel_path,
            )
        })
        .map_err(scs_err)
    }

    /// Remove only the ingested_files tracking record, not the nodes.
    ///
    /// This lets the indexing pipeline invalidate a hash before it replaces
    /// the affected file subgraph.
    fn delete_ingestion_record(
        &self,
        py: Python<'_>,
        repo_path: &str,
        rel_path: &str,
    ) -> PyResult<()> {
        let pool = self.inner.pool();
        py.allow_threads(|| {
            scs_store::ingestion_files::delete_ingestion_record(pool, repo_path, rel_path)
        })
        .map_err(scs_err)
    }

    /// Collect all node IDs that were ingested from a specific source file.
    ///
    /// Used by the pipeline to identify stale entities for targeted deletion
    /// instead of the blunt delete-all-for-file approach that wipes summary
    /// metadata.
    fn get_node_ids_for_file(
        &self,
        py: Python<'_>,
        repo_path: &str,
        rel_path: &str,
    ) -> PyResult<Vec<String>> {
        let pool = self.inner.pool();
        py.allow_threads(|| {
            scs_store::ingestion_files::get_node_ids_for_file(pool, repo_path, rel_path)
        })
        .map_err(scs_err)
    }

    /// Return every source file path currently represented by nodes for a repo.
    ///
    /// Reads from graph nodes rather than ingestion tracking so callers can
    /// find orphaned file-scoped nodes after `clear_ingestion_hashes`.
    fn get_file_paths_for_repo(&self, py: Python<'_>, repo_path: &str) -> PyResult<Vec<String>> {
        let pool = self.inner.pool();
        py.allow_threads(|| scs_store::ingestion_files::get_file_paths_for_repo(pool, repo_path))
            .map_err(scs_err)
    }

    /// Drop an entire repo's index — all ingested files, nodes, and embeddings.
    ///
    /// Atomic bulk operation for the "Drop Repo Index" UI action.
    /// Returns a JSON object with `files_removed`, `nodes_removed`, and
    /// `embeddings_removed` counts so the UI can confirm the operation scope.
    fn delete_repo(&self, py: Python<'_>, repo_path: &str) -> PyResult<PyObject> {
        let pool = self.inner.pool();
        let vi = self.inner.vector_index();
        let result = py
            .allow_threads(|| scs_store::ingestion_files::delete_repo(pool, vi, repo_path))
            .map_err(scs_err)?;
        to_json_pyobject(py, &result)
    }

    // ── Maintenance ────────────────────────────────────────────────

    /// Compact the database by running SQLite `VACUUM`.
    ///
    /// Rebuilds the database file, reclaiming disk space from deleted rows.
    /// Returns a JSON object with `size_before` and `size_after` (bytes)
    /// so the UI can report how much space was reclaimed.
    /// GIL released — VACUUM can be slow on large databases.
    fn vacuum(&self, py: Python<'_>) -> PyResult<PyObject> {
        let result = py.allow_threads(|| self.inner.vacuum()).map_err(scs_err)?;
        to_json_pyobject(py, &result)
    }

    /// Clear all data from the knowledge graph while preserving the schema.
    ///
    /// Releases the GIL — safe for asyncio.to_thread callers.
    /// Returns the number of nodes that were deleted.
    fn truncate(&self, py: Python<'_>) -> PyResult<usize> {
        py.allow_threads(|| self.inner.truncate()).map_err(scs_err)
    }

    /// Clear all ingestion hash records so the next ingest re-processes every file.
    ///
    /// Releases the GIL — safe for asyncio.to_thread callers.
    /// Returns the number of records cleared.
    fn clear_ingestion_hashes(&self, py: Python<'_>) -> PyResult<usize> {
        py.allow_threads(|| self.inner.clear_ingestion_hashes())
            .map_err(scs_err)
    }

    /// Clear all embedding vectors from the USearch index.
    ///
    /// Used when the embedding model changes but the dimension stays the same.
    /// Releases the GIL — safe for asyncio.to_thread callers.
    fn clear_embeddings(&self, py: Python<'_>) -> PyResult<()> {
        py.allow_threads(|| self.inner.clear_embeddings())
            .map_err(scs_err)
    }
}

// ── Standalone Parser FFI ────────────────────────────────────────────

/// Parse a source file and return extracted entities and edges as JSON.
///
/// Returns `None` if the file extension is not supported. The JSON format
/// is `{"entities": [...], "edges": [...]}` matching the `ParsedEntity`
/// and `ParsedEdge` structs in `scs-parser`.
///
/// The GIL is released during tree-sitter parsing so concurrent Python
/// threads are not blocked.
#[cfg(feature = "python")]
#[pyfunction]
fn parse_file(py: Python<'_>, source: &str, file_path: &str) -> PyResult<Option<PyObject>> {
    use scs_parser::parser::registry::get_parser;

    // Extract file extension.
    let ext = file_path.rfind('.').map(|i| &file_path[i..]).unwrap_or("");

    let parser = match get_parser(ext) {
        Some(p) => p,
        None => return Ok(None),
    };

    // Release GIL during tree-sitter work.
    let source_owned = source.to_string();
    let file_path_owned = file_path.to_string();
    let (entities, edges) = py.allow_threads(move || parser.parse(&source_owned, &file_path_owned));

    let result = serde_json::json!({
        "entities": entities,
        "edges": edges,
    });
    let json_str = serde_json::to_string(&result).map_err(json_err)?;
    Ok(Some(json_str.into_pyobject(py)?.into_any().unbind()))
}

/// Return the list of file extensions the parser registry supports.
///
/// Used by Python's registry to delegate extension mapping to Rust
/// as the single source of truth.
#[cfg(feature = "python")]
#[pyfunction]
fn parse_file_supported_extensions() -> Vec<String> {
    scs_parser::parser::registry::supported_extensions()
        .into_iter()
        .map(|s| s.to_string())
        .collect()
}

/// Return the exact code-only node discriminator contract.
#[cfg(feature = "python")]
#[pyfunction]
fn node_type_values() -> Vec<String> {
    NodeType::iter().map(|value| value.to_string()).collect()
}

/// Return the exact structural and provenance relationship contract.
#[cfg(feature = "python")]
#[pyfunction]
fn relationship_type_values() -> Vec<String> {
    RelationshipType::iter()
        .map(|value| value.to_string())
        .collect()
}

/// Python module definition.
///
/// Registers the `KnowledgeGraph` class and standalone parser functions
/// in the private `_scs_native` module.
#[cfg(feature = "python")]
#[pymodule]
fn _scs_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyKnowledgeGraph>()?;
    m.add_function(wrap_pyfunction!(parse_file, m)?)?;
    m.add_function(wrap_pyfunction!(parse_file_supported_extensions, m)?)?;
    m.add_function(wrap_pyfunction!(node_type_values, m)?)?;
    m.add_function(wrap_pyfunction!(relationship_type_values, m)?)?;
    Ok(())
}
