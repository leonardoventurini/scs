//! SCS compatibility adapter over the generic TSG storage engine.

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::str::FromStr;
use std::sync::{Mutex, MutexGuard};

use scs_core::error::{SCSError, SCSResult};
use scs_core::models::{Edge, GraphRagResult, Node, Repo, SearchResult, TraversalResult};
use scs_core::node_types::{NodeType, RelationshipType};
use scs_core::SCSConfig;
use tsg::{
    AttributeFilter, CatalogKey, CatalogRecord, Direction, Embedding, NodeFilter, SearchBackend,
    SearchFilter, Store, WriteBatch,
};
use uuid::Uuid;

use crate::batch::{BatchEdge, BatchNode};
use crate::ingestion_files::{DeleteRepoResult, IngestedFileRecord, IngestionStats};

const EDGE_NAMESPACE: Uuid = Uuid::from_bytes([
    0xa1, 0xb2, 0xc3, 0xd4, 0xe5, 0xf6, 0x78, 0x90, 0xab, 0xcd, 0xef, 0x12, 0x34, 0x56, 0x78, 0x90,
]);
const QUALIFIED_NAME_PATH: &str = "$.qualified_name";
const FILE_PATH_PATH: &str = "$.file_path";
const PAGE_SIZE: usize = 10_000;
const INGESTION_NAMESPACE: &str = "scs.ingested-files";
const REPOSITORY_NAMESPACE: &str = "scs.repositories";

/// Generate the stable edge ID used by SCS callers.
pub fn make_edge_id(source_id: &str, target_id: &str, relationship: &str) -> String {
    Uuid::new_v5(
        &EDGE_NAMESPACE,
        format!("{source_id}:{target_id}:{relationship}").as_bytes(),
    )
    .to_string()
}

/// Result of compacting the authoritative database.
#[derive(Debug, Clone, serde::Serialize)]
pub struct VacuumResult {
    pub size_before: u64,
    pub size_after: u64,
}

/// Direction for edge reads.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EdgeDirection {
    Outgoing,
    Incoming,
    Both,
}

/// Direction for recursive traversal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TraversalDirection {
    Outgoing,
    Incoming,
}

/// SCS-facing graph service with TSG as its persistence authority.
pub struct KnowledgeGraph {
    store: Mutex<Store>,
    config: SCSConfig,
}

impl KnowledgeGraph {
    /// Open or create the TSG store.
    pub fn open(config: SCSConfig) -> SCSResult<Self> {
        let store = match open_tsg(&config) {
            Ok(store) => store,
            Err(tsg::Error::UnsupportedSchema { .. } | tsg::Error::ReindexRequired { .. })
                if config.db_path.exists() =>
            {
                preserve_legacy_store(&config)?;
                open_tsg(&config).map_err(tsg_error)?
            }
            Err(error) => return Err(tsg_error(error)),
        };
        Ok(Self {
            store: Mutex::new(store),
            config,
        })
    }

    pub fn embedding_dim(&self) -> usize {
        self.config.embedding_dim
    }
    pub fn config(&self) -> &SCSConfig {
        &self.config
    }
    pub fn flush_vector_index(&self) -> SCSResult<bool> {
        Ok(self.lock()?.stats().map_err(tsg_error)?.accelerator_ready)
    }

    pub fn reopened_vectors_contain(&self, ids: &[String]) -> SCSResult<bool> {
        let store = self.lock()?;
        let missing: HashSet<_> = missing_nodes(&store, NodeFilter::default())?
            .into_iter()
            .map(|n| n.id)
            .collect();
        Ok(ids
            .iter()
            .all(|id| store.get_node(id).ok().flatten().is_some() && !missing.contains(id)))
    }

    pub fn reopened_vectors_absent(&self, ids: &[String]) -> SCSResult<bool> {
        let store = self.lock()?;
        let missing: HashSet<_> = missing_nodes(&store, NodeFilter::default())?
            .into_iter()
            .map(|n| n.id)
            .collect();
        Ok(ids
            .iter()
            .all(|id| store.get_node(id).ok().flatten().is_none() || missing.contains(id)))
    }

    pub fn get_or_create_repo(&self, path: &str) -> SCSResult<Repo> {
        let mut store = self.lock()?;
        let scope = store.get_or_create_scope(path).map_err(tsg_error)?;
        store
            .apply_batch(&WriteBatch {
                catalog_records: vec![CatalogRecord {
                    namespace: REPOSITORY_NAMESPACE.into(),
                    key: path.into(),
                    value: serde_json::json!({"scope_id": scope.id}),
                }],
                ..WriteBatch::default()
            })
            .map_err(tsg_error)?;
        Ok(Repo {
            id: scope.id,
            path: scope.key,
        })
    }

    pub fn resolve_repo_id(&self, path: &str) -> SCSResult<Option<i64>> {
        Ok(self
            .lock()?
            .scope_by_key(path)
            .map_err(tsg_error)?
            .map(|s| s.id))
    }

    pub fn resolve_repo_path(&self, id: i64) -> SCSResult<Option<String>> {
        Ok(self
            .lock()?
            .scope_by_id(id)
            .map_err(tsg_error)?
            .map(|s| s.key))
    }

    pub fn resolve_node_id_by_qualified_name(
        &self,
        repo_path: &str,
        qualified_name: &str,
    ) -> SCSResult<Option<String>> {
        let Some(scope_id) = self.resolve_repo_id(repo_path)? else {
            return Ok(None);
        };
        let value = serde_json::json!(qualified_name);
        Ok(self
            .lock()?
            .find_nodes_by_attribute(
                Some(scope_id),
                AttributeFilter {
                    path: QUALIFIED_NAME_PATH,
                    value: &value,
                },
                1,
                0,
            )
            .map_err(tsg_error)?
            .into_iter()
            .next()
            .map(|n| n.id))
    }

    pub fn get_file_node_map(&self, repo_id: i64) -> SCSResult<HashMap<String, String>> {
        let store = self.lock()?;
        Ok(all_nodes(
            &store,
            NodeFilter {
                scope_id: Some(repo_id),
                kind: Some("file"),
            },
        )?
        .into_iter()
        .map(|n| (n.name, n.id))
        .collect())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn upsert_node(
        &self,
        id: &str,
        node_type: NodeType,
        name: &str,
        content: &str,
        metadata: Option<&HashMap<String, serde_json::Value>>,
        embedding: Option<&[f32]>,
        repo_id: Option<i64>,
    ) -> SCSResult<Node> {
        let mut batch = WriteBatch::default();
        batch.nodes.push(tsg::Node {
            id: id.into(),
            scope_id: repo_id,
            kind: node_type.to_string(),
            name: name.into(),
            content: content.into(),
            attributes: object(metadata.cloned().unwrap_or_default()),
        });
        if let Some(vector) = embedding {
            batch.embeddings.push(Embedding {
                node_id: id.into(),
                vector: vector.to_vec(),
            });
        }
        let mut store = self.lock()?;
        store.apply_batch(&batch).map_err(tsg_error)?;
        convert_node(
            store
                .get_node(id)
                .map_err(tsg_error)?
                .ok_or_else(|| SCSError::NotFound(id.into()))?,
        )
    }

    pub fn get_node(&self, id: &str) -> SCSResult<Option<Node>> {
        self.lock()?
            .get_node(id)
            .map_err(tsg_error)?
            .map(convert_node)
            .transpose()
    }

    pub fn delete_node(&self, id: &str) -> SCSResult<bool> {
        Ok(self
            .lock()?
            .delete_nodes(&[id.to_string()])
            .map_err(tsg_error)?
            .nodes_deleted
            > 0)
    }

    pub fn delete_nodes_by_metadata(
        &self,
        node_type: &NodeType,
        key: &str,
        value: &str,
    ) -> SCSResult<usize> {
        let json_value = serde_json::json!(value);
        let path = format!("$.{key}");
        let mut store = self.lock()?;
        let ids: Vec<_> = store
            .find_nodes_by_attribute(
                None,
                AttributeFilter {
                    path: &path,
                    value: &json_value,
                },
                PAGE_SIZE * 100,
                0,
            )
            .map_err(tsg_error)?
            .into_iter()
            .filter(|n| n.kind == node_type.to_string())
            .map(|n| n.id)
            .collect();
        if ids.is_empty() {
            return Ok(0);
        }
        Ok(store.delete_nodes(&ids).map_err(tsg_error)?.nodes_deleted)
    }

    pub fn list_nodes(
        &self,
        node_type: Option<NodeType>,
        limit: i64,
        offset: i64,
        repo_id: Option<i64>,
    ) -> SCSResult<Vec<Node>> {
        let (limit, offset) = page(limit, offset)?;
        let kind = node_type.map(|v| v.to_string());
        convert_nodes(
            self.lock()?
                .list_nodes(
                    NodeFilter {
                        scope_id: repo_id,
                        kind: kind.as_deref(),
                    },
                    limit,
                    offset,
                )
                .map_err(tsg_error)?,
        )
    }

    pub fn count_nodes(&self, node_type: Option<NodeType>, repo_id: Option<i64>) -> SCSResult<i64> {
        let kind = node_type.map(|v| v.to_string());
        count(
            self.lock()?
                .count_nodes(NodeFilter {
                    scope_id: repo_id,
                    kind: kind.as_deref(),
                })
                .map_err(tsg_error)?,
        )
    }

    pub fn list_nodes_without_embeddings(
        &self,
        node_type: Option<NodeType>,
        limit: i64,
        offset: i64,
        repo_id: Option<i64>,
    ) -> SCSResult<Vec<Node>> {
        let (limit, offset) = page(limit, offset)?;
        let kind = node_type.map(|v| v.to_string());
        convert_nodes(
            self.lock()?
                .list_nodes_without_embeddings(
                    NodeFilter {
                        scope_id: repo_id,
                        kind: kind.as_deref(),
                    },
                    limit,
                    offset,
                )
                .map_err(tsg_error)?,
        )
    }

    pub fn count_nodes_without_embeddings(
        &self,
        node_type: Option<NodeType>,
        repo_id: Option<i64>,
    ) -> SCSResult<i64> {
        let kind = node_type.map(|v| v.to_string());
        count(
            self.lock()?
                .count_nodes_without_embeddings(NodeFilter {
                    scope_id: repo_id,
                    kind: kind.as_deref(),
                })
                .map_err(tsg_error)?,
        )
    }

    pub fn count_nodes_by_type(&self, repo_id: Option<i64>) -> SCSResult<HashMap<String, i64>> {
        let mut result = HashMap::new();
        let store = self.lock()?;
        for node in all_nodes(
            &store,
            NodeFilter {
                scope_id: repo_id,
                kind: None,
            },
        )? {
            *result.entry(node.kind).or_insert(0) += 1;
        }
        Ok(result)
    }

    pub fn count_embeddings(&self) -> SCSResult<i64> {
        count(self.lock()?.embedding_count().map_err(tsg_error)?)
    }

    pub fn search_by_name(
        &self,
        name: &str,
        node_type: Option<NodeType>,
        limit: i64,
        repo_id: Option<i64>,
    ) -> SCSResult<Vec<Node>> {
        let (limit, _) = page(limit, 0)?;
        let kind = node_type.map(|v| v.to_string());
        convert_nodes(
            self.lock()?
                .find_nodes_by_name(
                    name,
                    NodeFilter {
                        scope_id: repo_id,
                        kind: kind.as_deref(),
                    },
                    limit,
                    0,
                )
                .map_err(tsg_error)?,
        )
    }

    pub fn upsert_edge(
        &self,
        source: &str,
        target: &str,
        relationship: &str,
        weight: f64,
        metadata: Option<&HashMap<String, serde_json::Value>>,
    ) -> SCSResult<Edge> {
        validate_relationship(relationship)?;
        let edge = tsg::Edge {
            id: make_edge_id(source, target, relationship),
            source_id: source.into(),
            target_id: target.into(),
            relationship: relationship.into(),
            weight,
            attributes: object(metadata.cloned().unwrap_or_default()),
        };
        self.lock()?
            .apply_batch(&WriteBatch {
                edges: vec![edge.clone()],
                ..WriteBatch::default()
            })
            .map_err(tsg_error)?;
        convert_edge(edge)
    }

    pub fn get_edges(
        &self,
        id: &str,
        relationship: Option<&str>,
        direction: EdgeDirection,
    ) -> SCSResult<Vec<Edge>> {
        self.lock()?
            .get_edges(id, direction.into(), relationship)
            .map_err(tsg_error)?
            .into_iter()
            .map(convert_edge)
            .collect()
    }

    pub fn delete_edge(&self, id: &str) -> SCSResult<bool> {
        self.lock()?.delete_edge(id).map_err(tsg_error)
    }

    pub fn search_by_vector(
        &self,
        query: &[f32],
        node_type: Option<NodeType>,
        limit: i64,
        repo_id: Option<i64>,
    ) -> SCSResult<Vec<SearchResult>> {
        let (limit, _) = page(limit, 0)?;
        let kind = node_type.map(|v| v.to_string());
        self.lock()?
            .search(
                query,
                limit,
                SearchFilter {
                    scope_id: repo_id,
                    kind: kind.as_deref(),
                },
                SearchBackend::Adaptive,
            )
            .map_err(tsg_error)?
            .hits
            .into_iter()
            .map(|hit| {
                Ok(SearchResult {
                    node: convert_node(hit.node)?,
                    distance: f64::from(hit.distance),
                })
            })
            .collect()
    }

    pub fn get_neighbors(
        &self,
        id: &str,
        relationship: Option<&str>,
        direction: EdgeDirection,
        limit: i64,
    ) -> SCSResult<Vec<Node>> {
        let (limit, _) = page(limit, 0)?;
        let store = self.lock()?;
        let ids: Vec<_> = store
            .get_edges(id, direction.into(), relationship)
            .map_err(tsg_error)?
            .into_iter()
            .map(|e| {
                if direction == EdgeDirection::Incoming
                    || (direction == EdgeDirection::Both && e.target_id == id)
                {
                    e.source_id
                } else {
                    e.target_id
                }
            })
            .take(limit)
            .collect();
        convert_nodes(store.get_nodes(&ids).map_err(tsg_error)?)
    }

    pub fn traverse(
        &self,
        start: &str,
        max_depth: i32,
        relationship: Option<&str>,
        direction: TraversalDirection,
    ) -> SCSResult<Vec<TraversalResult>> {
        if max_depth < 0 {
            return Err(SCSError::Config(
                "traversal depth must be non-negative".into(),
            ));
        }
        let mut results = Vec::new();
        let mut visited = HashSet::from([start.to_string()]);
        let mut frontier = vec![(start.to_string(), 0_i32, vec![start.to_string()])];
        while let Some((id, depth, path)) = frontier.pop() {
            let node = self
                .get_node(&id)?
                .ok_or_else(|| SCSError::NotFound(id.clone()))?;
            results.push(TraversalResult {
                node,
                depth,
                path: path.clone(),
            });
            if depth == max_depth {
                continue;
            }
            for neighbor in self.get_neighbors(&id, relationship, direction.into(), i64::MAX)? {
                if visited.insert(neighbor.id.clone()) {
                    let mut next = path.clone();
                    next.push(neighbor.id.clone());
                    frontier.push((neighbor.id, depth + 1, next));
                }
            }
        }
        Ok(results)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn graph_rag_query(
        &self,
        query: &[f32],
        node_type: Option<NodeType>,
        vector_limit: i64,
        hop_limit: i32,
        relationship: Option<&str>,
        repo_id: Option<i64>,
    ) -> SCSResult<GraphRagResult> {
        let similar_nodes = self.search_by_vector(query, node_type, vector_limit, repo_id)?;
        let mut seen: HashSet<_> = similar_nodes.iter().map(|r| r.node.id.clone()).collect();
        let mut graph_context = Vec::new();
        for hit in &similar_nodes {
            for item in self.traverse(
                &hit.node.id,
                hop_limit,
                relationship,
                TraversalDirection::Outgoing,
            )? {
                if seen.insert(item.node.id.clone()) {
                    graph_context.push(item.node);
                }
            }
        }
        Ok(GraphRagResult {
            similar_nodes,
            graph_context,
        })
    }

    pub fn vacuum(&self) -> SCSResult<VacuumResult> {
        let size_before = file_size(&self.config.db_path);
        self.lock()?.vacuum().map_err(tsg_error)?;
        Ok(VacuumResult {
            size_before,
            size_after: file_size(&self.config.db_path),
        })
    }
    pub fn truncate(&self) -> SCSResult<usize> {
        self.lock()?.truncate().map_err(tsg_error)
    }
    pub fn clear_embeddings(&self) -> SCSResult<()> {
        self.lock()?
            .clear_embeddings()
            .map_err(tsg_error)
            .map(|_| ())
    }
    pub fn batch_get_nodes(&self, ids: &[String]) -> SCSResult<Vec<Node>> {
        convert_nodes(self.lock()?.get_nodes(ids).map_err(tsg_error)?)
    }
    pub fn batch_get_edges(
        &self,
        ids: &[String],
        direction: EdgeDirection,
    ) -> SCSResult<HashMap<String, Vec<Edge>>> {
        ids.iter()
            .map(|id| Ok((id.clone(), self.get_edges(id, None, direction)?)))
            .collect()
    }

    pub fn batch_upsert_nodes(&self, nodes: &[BatchNode]) -> SCSResult<usize> {
        if nodes.is_empty() {
            return Ok(0);
        }
        let values = nodes
            .iter()
            .map(|n| tsg::Node {
                id: n.id.clone(),
                scope_id: n.repo_id,
                kind: n.node_type.to_string(),
                name: n.name.clone(),
                content: n.content.clone(),
                attributes: object(n.metadata.clone()),
            })
            .collect();
        self.lock()?
            .apply_batch(&WriteBatch {
                nodes: values,
                ..WriteBatch::default()
            })
            .map_err(tsg_error)?;
        Ok(nodes.len())
    }
    pub fn batch_upsert_embeddings(&self, values: &[(String, Vec<f32>)]) -> SCSResult<usize> {
        if values.is_empty() {
            return Ok(0);
        }
        let embeddings = values
            .iter()
            .map(|(id, vector)| Embedding {
                node_id: id.clone(),
                vector: vector.clone(),
            })
            .collect();
        self.lock()?
            .apply_batch(&WriteBatch {
                embeddings,
                ..WriteBatch::default()
            })
            .map_err(tsg_error)?;
        Ok(values.len())
    }
    pub fn batch_upsert_edges(&self, edges: &[BatchEdge]) -> SCSResult<usize> {
        let store = self.lock()?;
        let values: Vec<_> = edges
            .iter()
            .filter(|e| {
                store.get_node(&e.source_id).ok().flatten().is_some()
                    && store.get_node(&e.target_id).ok().flatten().is_some()
            })
            .map(|e| {
                validate_relationship(&e.relationship)?;
                Ok(tsg::Edge {
                    id: make_edge_id(&e.source_id, &e.target_id, &e.relationship),
                    source_id: e.source_id.clone(),
                    target_id: e.target_id.clone(),
                    relationship: e.relationship.clone(),
                    weight: e.weight,
                    attributes: object(e.metadata.clone()),
                })
            })
            .collect::<SCSResult<_>>()?;
        drop(store);
        if values.is_empty() {
            return Ok(0);
        }
        self.lock()?
            .apply_batch(&WriteBatch {
                edges: values.clone(),
                ..WriteBatch::default()
            })
            .map_err(tsg_error)?;
        Ok(values.len())
    }

    pub fn get_ingested_file_hash(
        &self,
        repo_path: &str,
        rel_path: &str,
    ) -> SCSResult<Option<String>> {
        let key = ingestion_key(repo_path, rel_path)?;
        Ok(self
            .lock()?
            .catalog_get(INGESTION_NAMESPACE, &key)
            .map_err(tsg_error)?
            .and_then(|record| {
                record
                    .value
                    .get("content_hash")
                    .and_then(|v| v.as_str())
                    .map(str::to_string)
            }))
    }

    #[allow(clippy::too_many_arguments)]
    pub fn upsert_ingested_file(
        &self,
        file_id: &str,
        repo_path: &str,
        rel_path: &str,
        language: &str,
        content_hash: &str,
        byte_size: i64,
    ) -> SCSResult<()> {
        let record = IngestedFileRecord {
            file_id: file_id.into(),
            repo_path: repo_path.into(),
            rel_path: rel_path.into(),
            language: language.into(),
            content_hash: content_hash.into(),
            byte_size,
        };
        self.acknowledge_ingested_files_batch(&[record]).map(|_| ())
    }

    pub fn acknowledge_ingested_files_batch(
        &self,
        records: &[IngestedFileRecord],
    ) -> SCSResult<usize> {
        if records.is_empty() {
            return Ok(0);
        }
        let now = chrono::Utc::now().to_rfc3339();
        let catalog_records = records
            .iter()
            .map(|record| {
                let mut value = serde_json::to_value(record)?;
                value
                    .as_object_mut()
                    .expect("serialized record is an object")
                    .insert("indexed_at".into(), serde_json::json!(now));
                Ok(CatalogRecord {
                    namespace: INGESTION_NAMESPACE.into(),
                    key: ingestion_key(&record.repo_path, &record.rel_path)?,
                    value,
                })
            })
            .collect::<SCSResult<Vec<_>>>()?;
        self.lock()?
            .apply_batch(&WriteBatch {
                catalog_records,
                ..WriteBatch::default()
            })
            .map_err(tsg_error)?;
        Ok(records.len())
    }

    pub fn get_all_ingested_files(&self, repo_path: &str) -> SCSResult<HashMap<String, String>> {
        Ok(self
            .ingestion_records()?
            .into_iter()
            .filter(|r| r.repo_path == repo_path)
            .map(|r| (r.rel_path, r.content_hash))
            .collect())
    }

    pub fn get_ingestion_stats(&self) -> SCSResult<HashMap<String, IngestionStats>> {
        let mut result = HashMap::new();
        for record in self
            .lock()?
            .catalog_list(INGESTION_NAMESPACE, PAGE_SIZE * 100, 0)
            .map_err(tsg_error)?
        {
            let parsed: IngestedFileRecord = serde_json::from_value(record.value.clone())?;
            let indexed_at = record
                .value
                .get("indexed_at")
                .and_then(|v| v.as_str())
                .unwrap_or_default();
            let entry = result
                .entry(parsed.repo_path)
                .or_insert_with(|| IngestionStats {
                    file_count: 0,
                    last_indexed: String::new(),
                });
            entry.file_count += 1;
            if indexed_at > entry.last_indexed.as_str() {
                entry.last_indexed = indexed_at.to_string();
            }
        }
        for scope in self.scope_paths()? {
            result.entry(scope).or_insert_with(|| IngestionStats {
                file_count: 0,
                last_indexed: String::new(),
            });
        }
        Ok(result)
    }

    pub fn delete_ingestion_record(&self, repo_path: &str, rel_path: &str) -> SCSResult<()> {
        self.delete_ingestion_records_batch(repo_path, &[rel_path.to_string()])
            .map(|_| ())
    }

    pub fn delete_ingestion_records_batch(
        &self,
        repo_path: &str,
        rel_paths: &[String],
    ) -> SCSResult<usize> {
        if rel_paths.is_empty() {
            return Ok(0);
        }
        let store = self.lock()?;
        let existing = rel_paths
            .iter()
            .filter(|path| {
                ingestion_key(repo_path, path)
                    .ok()
                    .and_then(|key| store.catalog_get(INGESTION_NAMESPACE, &key).ok().flatten())
                    .is_some()
            })
            .count();
        drop(store);
        let catalog_deletes = rel_paths
            .iter()
            .map(|path| {
                Ok(CatalogKey {
                    namespace: INGESTION_NAMESPACE.into(),
                    key: ingestion_key(repo_path, path)?,
                })
            })
            .collect::<SCSResult<Vec<_>>>()?;
        self.lock()?
            .apply_batch(&WriteBatch {
                catalog_deletes,
                ..WriteBatch::default()
            })
            .map_err(tsg_error)?;
        Ok(existing)
    }

    pub fn get_node_ids_for_file(&self, repo_path: &str, rel_path: &str) -> SCSResult<Vec<String>> {
        let Some(scope_id) = self.resolve_repo_id(repo_path)? else {
            return Ok(Vec::new());
        };
        let value = serde_json::json!(rel_path);
        Ok(self
            .lock()?
            .find_nodes_by_attribute(
                Some(scope_id),
                AttributeFilter {
                    path: FILE_PATH_PATH,
                    value: &value,
                },
                PAGE_SIZE * 100,
                0,
            )
            .map_err(tsg_error)?
            .into_iter()
            .map(|n| n.id)
            .collect())
    }

    pub fn remove_file_graph_and_vector(
        &self,
        repo_path: &str,
        rel_path: &str,
    ) -> SCSResult<usize> {
        let ids = self.get_node_ids_for_file(repo_path, rel_path)?;
        if ids.is_empty() {
            return Ok(0);
        }
        Ok(self
            .lock()?
            .delete_nodes(&ids)
            .map_err(tsg_error)?
            .nodes_deleted)
    }

    pub fn delete_ingested_file(&self, repo_path: &str, rel_path: &str) -> SCSResult<()> {
        self.remove_file_graph_and_vector(repo_path, rel_path)?;
        self.delete_ingestion_record(repo_path, rel_path)
    }

    pub fn get_file_paths_for_repo(&self, repo_path: &str) -> SCSResult<Vec<String>> {
        let Some(scope_id) = self.resolve_repo_id(repo_path)? else {
            return Ok(Vec::new());
        };
        let store = self.lock()?;
        let mut paths: Vec<_> = all_nodes(
            &store,
            NodeFilter {
                scope_id: Some(scope_id),
                kind: None,
            },
        )?
        .into_iter()
        .filter_map(|n| {
            n.attributes
                .get("file_path")
                .and_then(|v| v.as_str())
                .map(str::to_string)
        })
        .collect();
        paths.sort();
        paths.dedup();
        Ok(paths)
    }

    pub fn delete_repo(&self, repo_path: &str) -> SCSResult<DeleteRepoResult> {
        let files_removed = i64::try_from(self.get_all_ingested_files(repo_path)?.len())
            .map_err(|_| SCSError::Storage("file count overflow".into()))?;
        let Some(scope_id) = self.resolve_repo_id(repo_path)? else {
            return Ok(DeleteRepoResult {
                files_removed: 0,
                nodes_removed: 0,
                embeddings_removed: 0,
            });
        };
        let mut store = self.lock()?;
        let nodes = all_nodes(
            &store,
            NodeFilter {
                scope_id: Some(scope_id),
                kind: None,
            },
        )?;
        let missing = store
            .count_nodes_without_embeddings(NodeFilter {
                scope_id: Some(scope_id),
                kind: None,
            })
            .map_err(tsg_error)?;
        let ids: Vec<_> = nodes.into_iter().map(|n| n.id).collect();
        let nodes_removed = ids.len();
        if !ids.is_empty() {
            store.delete_nodes(&ids).map_err(tsg_error)?;
        }
        drop(store);
        let paths: Vec<_> = self
            .get_all_ingested_files(repo_path)?
            .into_keys()
            .collect();
        self.delete_ingestion_records_batch(repo_path, &paths)?;
        Ok(DeleteRepoResult {
            files_removed,
            nodes_removed: count(nodes_removed)?,
            embeddings_removed: count(nodes_removed.saturating_sub(missing))?,
        })
    }

    pub fn clear_ingestion_hashes(&self) -> SCSResult<usize> {
        let records = self
            .lock()?
            .catalog_list(INGESTION_NAMESPACE, PAGE_SIZE * 100, 0)
            .map_err(tsg_error)?;
        if records.is_empty() {
            return Ok(0);
        }
        let count = records.len();
        let catalog_deletes = records
            .into_iter()
            .map(|r| CatalogKey {
                namespace: r.namespace,
                key: r.key,
            })
            .collect();
        self.lock()?
            .apply_batch(&WriteBatch {
                catalog_deletes,
                ..WriteBatch::default()
            })
            .map_err(tsg_error)?;
        Ok(count)
    }

    fn ingestion_records(&self) -> SCSResult<Vec<IngestedFileRecord>> {
        self.lock()?
            .catalog_list(INGESTION_NAMESPACE, PAGE_SIZE * 100, 0)
            .map_err(tsg_error)?
            .into_iter()
            .map(|record| serde_json::from_value(record.value).map_err(Into::into))
            .collect()
    }

    fn scope_paths(&self) -> SCSResult<Vec<String>> {
        Ok(self
            .lock()?
            .catalog_list(REPOSITORY_NAMESPACE, PAGE_SIZE * 100, 0)
            .map_err(tsg_error)?
            .into_iter()
            .map(|record| record.key)
            .collect())
    }

    fn lock(&self) -> SCSResult<MutexGuard<'_, Store>> {
        self.store
            .lock()
            .map_err(|_| SCSError::Storage("TSG store lock is poisoned".into()))
    }
}

impl From<EdgeDirection> for Direction {
    fn from(v: EdgeDirection) -> Self {
        match v {
            EdgeDirection::Outgoing => Self::Outgoing,
            EdgeDirection::Incoming => Self::Incoming,
            EdgeDirection::Both => Self::Both,
        }
    }
}
impl From<TraversalDirection> for EdgeDirection {
    fn from(v: TraversalDirection) -> Self {
        match v {
            TraversalDirection::Outgoing => Self::Outgoing,
            TraversalDirection::Incoming => Self::Incoming,
        }
    }
}

fn open_tsg(config: &SCSConfig) -> tsg::Result<Store> {
    Store::builder(&config.db_path, config.embedding_dim)
        .node_attribute_indexes([QUALIFIED_NAME_PATH, FILE_PATH_PATH])
        .build()
}
fn preserve_legacy_store(config: &SCSConfig) -> SCSResult<()> {
    for path in [&config.db_path, &config.index_path] {
        if path.exists() {
            let backup = unique_backup(path);
            std::fs::rename(path, &backup).map_err(|e| {
                SCSError::Storage(format!(
                    "preserve {} as {}: {e}",
                    path.display(),
                    backup.display()
                ))
            })?;
        }
    }
    for suffix in ["-wal", "-shm"] {
        let path = std::path::PathBuf::from(format!("{}{suffix}", config.db_path.display()));
        if path.exists() {
            let backup = unique_backup(&path);
            std::fs::rename(&path, &backup)
                .map_err(|e| SCSError::Storage(format!("preserve {}: {e}", path.display())))?;
        }
    }
    Ok(())
}
fn unique_backup(path: &Path) -> std::path::PathBuf {
    let first = std::path::PathBuf::from(format!("{}.pre-tsg.backup", path.display()));
    if !first.exists() {
        return first;
    }
    (1_u32..)
        .map(|n| std::path::PathBuf::from(format!("{}.pre-tsg.{n}.backup", path.display())))
        .find(|p| !p.exists())
        .expect("backup names exhausted")
}
fn all_nodes(store: &Store, filter: NodeFilter<'_>) -> SCSResult<Vec<tsg::Node>> {
    let total = store.count_nodes(filter).map_err(tsg_error)?;
    if total == 0 {
        Ok(Vec::new())
    } else {
        store.list_nodes(filter, total, 0).map_err(tsg_error)
    }
}
fn missing_nodes(store: &Store, filter: NodeFilter<'_>) -> SCSResult<Vec<tsg::Node>> {
    let total = store
        .count_nodes_without_embeddings(filter)
        .map_err(tsg_error)?;
    if total == 0 {
        Ok(Vec::new())
    } else {
        store
            .list_nodes_without_embeddings(filter, total, 0)
            .map_err(tsg_error)
    }
}
fn convert_nodes(nodes: Vec<tsg::Node>) -> SCSResult<Vec<Node>> {
    nodes.into_iter().map(convert_node).collect()
}
fn convert_node(n: tsg::Node) -> SCSResult<Node> {
    Ok(Node {
        id: n.id,
        node_type: NodeType::from_str(&n.kind)
            .map_err(|_| SCSError::Storage(format!("invalid node type {}", n.kind)))?,
        name: n.name,
        content: n.content,
        metadata: attributes(n.attributes)?,
        repo_id: n.scope_id,
        created_at: None,
        updated_at: None,
    })
}
fn convert_edge(e: tsg::Edge) -> SCSResult<Edge> {
    Ok(Edge {
        id: e.id,
        source_id: e.source_id,
        target_id: e.target_id,
        relationship: e.relationship,
        weight: e.weight,
        metadata: attributes(e.attributes)?,
        created_at: None,
    })
}
fn object(map: HashMap<String, serde_json::Value>) -> serde_json::Value {
    serde_json::Value::Object(map.into_iter().collect())
}
fn attributes(value: serde_json::Value) -> SCSResult<HashMap<String, serde_json::Value>> {
    match value {
        serde_json::Value::Object(map) => Ok(map.into_iter().collect()),
        _ => Err(SCSError::Storage("attributes are not an object".into())),
    }
}
fn page(limit: i64, offset: i64) -> SCSResult<(usize, usize)> {
    if limit <= 0 || offset < 0 {
        return Err(SCSError::Config("invalid pagination".into()));
    }
    Ok((
        usize::try_from(limit).map_err(|_| SCSError::Config("limit overflow".into()))?,
        usize::try_from(offset).map_err(|_| SCSError::Config("offset overflow".into()))?,
    ))
}
fn count(value: usize) -> SCSResult<i64> {
    i64::try_from(value).map_err(|_| SCSError::Storage("count overflow".into()))
}
fn validate_relationship(value: &str) -> SCSResult<()> {
    RelationshipType::from_str(value)
        .map(|_| ())
        .map_err(|_| SCSError::Config(format!("unsupported relationship: {value}")))
}
fn tsg_error(error: tsg::Error) -> SCSError {
    if let tsg::Error::InvalidInput(message) = &error {
        if let Some(dimensions) = message.strip_prefix("embedding dimension mismatch: expected ") {
            if let Some((expected, actual)) = dimensions.split_once(", received ") {
                if let (Ok(expected), Ok(actual)) = (expected.parse(), actual.parse()) {
                    return SCSError::DimensionMismatch { expected, actual };
                }
            }
        }
    }
    SCSError::Storage(error.to_string())
}
fn file_size(path: &Path) -> u64 {
    std::fs::metadata(path).map_or(0, |m| m.len())
}

fn ingestion_key(repo_path: &str, rel_path: &str) -> SCSResult<String> {
    serde_json::to_string(&(repo_path, rel_path)).map_err(Into::into)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn graph() -> (tempfile::TempDir, KnowledgeGraph) {
        let directory = tempfile::tempdir().unwrap();
        let mut config = SCSConfig::for_testing(directory.path());
        config.embedding_dim = 2;
        let graph = KnowledgeGraph::open(config).unwrap();
        (directory, graph)
    }

    #[test]
    fn tsg_adapter_preserves_graph_vector_and_catalog_contracts() {
        let (_directory, graph) = graph();
        let repository = graph.get_or_create_repo("/repo").unwrap();
        let metadata = HashMap::from([
            ("qualified_name".to_string(), serde_json::json!("pkg::run")),
            ("file_path".to_string(), serde_json::json!("src/lib.rs")),
        ]);
        graph
            .upsert_node(
                "file",
                NodeType::File,
                "src/lib.rs",
                "",
                Some(&metadata),
                Some(&[1.0, 0.0]),
                Some(repository.id),
            )
            .unwrap();
        graph
            .upsert_node(
                "run",
                NodeType::Function,
                "run",
                "fn run() {}",
                Some(&metadata),
                Some(&[0.9, 0.1]),
                Some(repository.id),
            )
            .unwrap();
        graph
            .upsert_edge("file", "run", "contains", 1.0, None)
            .unwrap();
        graph
            .upsert_ingested_file("file", "/repo", "src/lib.rs", "rust", "digest", 12)
            .unwrap();

        assert_eq!(
            graph
                .resolve_node_id_by_qualified_name("/repo", "pkg::run")
                .unwrap(),
            Some("file".to_string())
        );
        assert_eq!(
            graph
                .search_by_vector(&[1.0, 0.0], None, 1, Some(repository.id))
                .unwrap()[0]
                .node
                .id,
            "file"
        );
        assert_eq!(
            graph
                .get_neighbors("file", Some("contains"), EdgeDirection::Outgoing, 10)
                .unwrap()[0]
                .id,
            "run"
        );
        assert_eq!(
            graph.get_ingested_file_hash("/repo", "src/lib.rs").unwrap(),
            Some("digest".to_string())
        );
    }

    #[test]
    fn incompatible_legacy_database_is_preserved_before_clean_reindex() {
        let directory = tempfile::tempdir().unwrap();
        let config = SCSConfig::for_testing(directory.path());
        let connection = rusqlite::Connection::open(&config.db_path).unwrap();
        connection.execute_batch("PRAGMA user_version = 3; CREATE TABLE legacy(value TEXT); INSERT INTO legacy VALUES ('sentinel');").unwrap();
        drop(connection);

        let graph = KnowledgeGraph::open(config.clone()).unwrap();
        assert_eq!(graph.count_nodes(None, None).unwrap(), 0);
        let backup_path =
            std::path::PathBuf::from(format!("{}.pre-tsg.backup", config.db_path.display()));
        let backup = rusqlite::Connection::open(backup_path).unwrap();
        let sentinel: String = backup
            .query_row("SELECT value FROM legacy", [], |row| row.get(0))
            .unwrap();
        assert_eq!(sentinel, "sentinel");
        let active = rusqlite::Connection::open(config.db_path).unwrap();
        let catalog_exists: i64 = active
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='catalog'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(catalog_exists, 1);
    }
}
