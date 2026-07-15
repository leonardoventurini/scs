//! HNSW vector index backed by USearch.
//!
//! Replaces sqlite-vec's brute-force linear scan with an HNSW graph for
//! sub-millisecond approximate nearest neighbor search at 100k+ vectors.
//!
//! The index file (`index.usearch`) lives alongside the SQLite database.
//! USearch keys are `u64` — we deterministically map UUID node IDs to u64
//! by taking the lower 64 bits of the parsed UUID value.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use rand::random;
use usearch::ffi::{IndexOptions, MetricKind, ScalarKind};
use uuid::Uuid;

use scs_core::error::{SCSError, SCSResult};

use crate::observability::{observe_result, QueryBackend};

/// Thread-safe HNSW vector index wrapper.
///
/// USearch's `Index` is `Send + Sync` for reads, but concurrent writes
/// require external synchronization. We use a `Mutex` to serialize
/// mutations (add, remove) while allowing the index to be shared across
/// the r2d2 pool's threads.
pub struct VectorIndex {
    index: Mutex<usearch::Index>,
    path: PathBuf,
    dimensions: usize,
    dirty: AtomicBool,
}

/// Convert a UUID node ID string to a deterministic u64 key.
///
/// Parses the UUID, extracts its 128-bit value, and takes the lower 64 bits.
/// UUIDs are uniformly random, so collision probability at our scale
/// (birthday bound: ~4 billion entries for 50% collision) is negligible.
///
/// Falls back to hashing non-UUID strings via a simple FNV-1a-like scheme
/// so the system doesn't break on legacy or synthetic IDs.
pub fn node_id_to_key(id: &str) -> u64 {
    match Uuid::parse_str(id) {
        Ok(uuid) => uuid.as_u128() as u64,
        Err(_) => {
            // FNV-1a hash for non-UUID strings (legacy/synthetic IDs).
            let mut hash: u64 = 0xcbf29ce484222325;
            for byte in id.as_bytes() {
                hash ^= *byte as u64;
                hash = hash.wrapping_mul(0x100000001b3);
            }
            hash
        }
    }
}

/// Convert a u64 USearch key back to a UUID string lookup candidate.
///
/// Since we only store the lower 64 bits, this is a one-way mapping —
/// we can't reconstruct the full UUID from the key alone. Instead,
/// callers maintain a key→id lookup table (usually a HashMap loaded
/// from SQLite) for reverse mapping.
impl VectorIndex {
    /// Open an existing index or create a new one at the given path.
    ///
    /// If the file exists and is non-empty, loads the index from disk.
    /// Otherwise, creates a fresh HNSW index with the given dimensions.
    pub fn open(path: impl Into<PathBuf>, dimensions: usize) -> SCSResult<Self> {
        let path = path.into();
        Self::cleanup_stale_temp_files(&path);
        observe_result(
            QueryBackend::Vector,
            "VectorIndex::open",
            path.display().to_string(),
            format!("dims={dimensions}"),
            |obs| {
                let opts = IndexOptions {
                    dimensions,
                    metric: MetricKind::Cos,
                    quantization: ScalarKind::F32,
                    connectivity: 16,     // HNSW M parameter — default sweet spot
                    expansion_add: 128,   // ef_construction — higher = better recall on insert
                    expansion_search: 64, // ef — higher = better recall on search
                    multi: false,         // one vector per key
                };

                let index = usearch::Index::new(&opts).map_err(|e| {
                    SCSError::Storage(format!("failed to create USearch index: {e}"))
                })?;

                // Load from disk if the file exists and is non-empty.
                if path.exists()
                    && std::fs::metadata(&path)
                        .map(|m| m.len() > 0)
                        .unwrap_or(false)
                {
                    index.load(path.to_str().unwrap_or_default()).map_err(|e| {
                        SCSError::Storage(format!(
                            "failed to load USearch index from {}: {e}",
                            path.display()
                        ))
                    })?;
                    obs.set_vectors(index.size());
                    log::info!(
                        "Loaded USearch index from {} ({} vectors, {} dims)",
                        path.display(),
                        index.size(),
                        dimensions,
                    );
                } else {
                    // Reserve initial capacity — USearch grows dynamically but
                    // pre-reserving avoids repeated reallocations during bulk import.
                    index.reserve(10_000).map_err(|e| {
                        SCSError::Storage(format!("failed to reserve USearch capacity: {e}"))
                    })?;
                    obs.set_vectors(0);
                    log::info!(
                        "Created new USearch index at {} ({} dims)",
                        path.display(),
                        dimensions,
                    );
                }

                Ok(Self {
                    index: Mutex::new(index),
                    path,
                    dimensions,
                    dirty: AtomicBool::new(false),
                })
            },
        )
    }

    fn lock_index(&self) -> SCSResult<(std::sync::MutexGuard<'_, usearch::Index>, Duration)> {
        let wait_started = std::time::Instant::now();
        let index = self
            .index
            .lock()
            .map_err(|e| SCSError::Storage(format!("index lock poisoned: {e}")))?;
        Ok((index, wait_started.elapsed()))
    }

    fn target(&self) -> String {
        self.path.display().to_string()
    }

    /// Add or update a vector for the given node ID.
    ///
    /// If the key already exists, the old vector is replaced.
    pub fn add(&self, node_id: &str, embedding: &[f32]) -> SCSResult<()> {
        observe_result(
            QueryBackend::Vector,
            "VectorIndex::add",
            self.target(),
            format!("dims={}", embedding.len()),
            |obs| {
                let key = node_id_to_key(node_id);
                let (index, wait) = self.lock_index()?;
                obs.set_wait(wait);

                // USearch doesn't have a native "upsert" — remove first if exists.
                if index.contains(key) {
                    let _ = index.remove(key);
                }

                // Ensure capacity — USearch panics on overflow without reserve.
                if index.size() >= index.capacity() {
                    let new_cap = (index.capacity() * 2).max(1024);
                    index.reserve(new_cap).map_err(|e| {
                        SCSError::Storage(format!("failed to grow USearch index: {e}"))
                    })?;
                }

                index.add(key, embedding).map_err(|e| {
                    SCSError::Storage(format!("failed to add vector for {node_id}: {e}"))
                })?;
                obs.set_vectors(1);
                self.mark_dirty();
                Ok(())
            },
        )
    }

    /// Batch-add vectors without intermediate saves.
    ///
    /// More efficient than calling `add()` in a loop because we pre-reserve
    /// capacity once. Caller should call `save_if_dirty()` after the owning
    /// ingestion job completes.
    pub fn add_batch(&self, pairs: &[(String, Vec<f32>)]) -> SCSResult<usize> {
        observe_result(
            QueryBackend::Vector,
            "VectorIndex::add_batch",
            self.target(),
            format!("count={}", pairs.len()),
            |obs| {
                let (index, wait) = self.lock_index()?;
                obs.set_wait(wait);

                // Pre-reserve capacity for the entire batch.
                let needed = index.size() + pairs.len();
                if needed > index.capacity() {
                    let new_cap = (needed * 2).max(1024);
                    index.reserve(new_cap).map_err(|e| {
                        SCSError::Storage(format!("failed to reserve capacity for batch: {e}"))
                    })?;
                }

                let mut count = 0;
                for (node_id, embedding) in pairs {
                    let key = node_id_to_key(node_id);

                    // Remove existing vector if present (upsert semantics).
                    if index.contains(key) {
                        let _ = index.remove(key);
                    }

                    index.add(key, embedding).map_err(|e| {
                        SCSError::Storage(format!("failed to add vector for {node_id}: {e}"))
                    })?;
                    count += 1;
                }

                if count > 0 {
                    self.mark_dirty();
                }
                obs.set_vectors(count);
                Ok(count)
            },
        )
    }

    /// Search for the nearest neighbors of a query vector.
    ///
    /// Returns `(key, distance)` pairs sorted by ascending distance.
    /// Use `key_to_node_id` mapping from SQLite to resolve back to node IDs.
    pub fn search(&self, query: &[f32], limit: usize) -> SCSResult<Vec<(u64, f32)>> {
        observe_result(
            QueryBackend::Vector,
            "VectorIndex::search",
            self.target(),
            format!("limit={limit} dims={}", query.len()),
            |obs| {
                let (index, wait) = self.lock_index()?;
                obs.set_wait(wait);

                if index.size() == 0 {
                    obs.set_vectors(0);
                    return Ok(Vec::new());
                }

                let results = index
                    .search(query, limit)
                    .map_err(|e| SCSError::Storage(format!("USearch search failed: {e}")))?;
                obs.set_vectors(results.keys.len());

                Ok(results.keys.into_iter().zip(results.distances).collect())
            },
        )
    }

    /// Search with a predicate filter on candidate keys.
    ///
    /// Pre-loads a set of valid u64 keys (e.g., from a SQLite query filtered
    /// by node_type/repo_id), then over-fetches from USearch and post-filters.
    /// This replaces the old sqlite-vec 10× over-fetch hack with a cleaner
    /// approach: HNSW returns candidates fast, and we filter in memory.
    pub fn filtered_search(
        &self,
        query: &[f32],
        limit: usize,
        valid_keys: &HashSet<u64>,
    ) -> SCSResult<Vec<(u64, f32)>> {
        observe_result(
            QueryBackend::Vector,
            "VectorIndex::filtered_search",
            self.target(),
            format!(
                "limit={limit} dims={} valid_keys={}",
                query.len(),
                valid_keys.len()
            ),
            |obs| {
                let (index, wait) = self.lock_index()?;
                obs.set_wait(wait);

                if index.size() == 0 || valid_keys.is_empty() {
                    obs.set_vectors(0);
                    return Ok(Vec::new());
                }

                // Over-fetch by 10× to account for filtering, capped at total index size.
                let internal_k = (limit * 10).min(index.size());

                let results = index.search(query, internal_k).map_err(|e| {
                    SCSError::Storage(format!("USearch filtered search failed: {e}"))
                })?;

                let filtered: Vec<(u64, f32)> = results
                    .keys
                    .into_iter()
                    .zip(results.distances)
                    .filter(|(key, _)| valid_keys.contains(key))
                    .take(limit)
                    .collect();
                obs.set_vectors(filtered.len());
                Ok(filtered)
            },
        )
    }

    /// Remove a vector by node ID.
    pub fn remove(&self, node_id: &str) -> SCSResult<()> {
        observe_result(
            QueryBackend::Vector,
            "VectorIndex::remove",
            self.target(),
            String::new(),
            |obs| {
                let key = node_id_to_key(node_id);
                let (index, wait) = self.lock_index()?;
                obs.set_wait(wait);

                if index.contains(key) {
                    index.remove(key).map_err(|e| {
                        SCSError::Storage(format!("failed to remove vector for {node_id}: {e}"))
                    })?;
                    obs.set_vectors(1);
                    self.mark_dirty();
                    self.save_locked(&index)?;
                    self.dirty.store(false, Ordering::Release);
                } else {
                    obs.set_vectors(0);
                }
                Ok(())
            },
        )
    }

    /// Batch-remove vectors by node ID.
    pub fn remove_batch(&self, node_ids: &[String]) -> SCSResult<()> {
        observe_result(
            QueryBackend::Vector,
            "VectorIndex::remove_batch",
            self.target(),
            format!("count={}", node_ids.len()),
            |obs| {
                let (index, wait) = self.lock_index()?;
                obs.set_wait(wait);
                let mut removed = 0;

                for node_id in node_ids {
                    let key = node_id_to_key(node_id);
                    if index.contains(key) {
                        let _ = index.remove(key);
                        removed += 1;
                    }
                }
                if removed > 0 {
                    self.mark_dirty();
                    self.save_locked(&index)?;
                    self.dirty.store(false, Ordering::Release);
                }
                obs.set_vectors(removed);
                Ok(())
            },
        )
    }

    /// Flush the index to disk.
    pub fn save(&self) -> SCSResult<()> {
        observe_result(
            QueryBackend::Vector,
            "VectorIndex::save",
            self.target(),
            String::new(),
            |obs| {
                let (index, wait) = self.lock_index()?;
                obs.set_wait(wait);

                self.save_locked(&index)?;
                self.dirty.store(false, Ordering::Release);
                obs.set_vectors(index.size());

                log::debug!(
                    "Saved USearch index to {} ({} vectors)",
                    self.path.display(),
                    index.size(),
                );
                Ok(())
            },
        )
    }

    /// Flush the index only when vectors changed since the last save.
    ///
    /// Returns `true` when the sidecar was rewritten and `false` when the
    /// index was already clean. This avoids rewriting large USearch files at
    /// every ingestion boundary that happens not to change embeddings.
    pub fn save_if_dirty(&self) -> SCSResult<bool> {
        observe_result(
            QueryBackend::Vector,
            "VectorIndex::save_if_dirty",
            self.target(),
            String::new(),
            |obs| {
                if !self.dirty.load(Ordering::Acquire) {
                    obs.set_vectors(0);
                    return Ok(false);
                }

                let (index, wait) = self.lock_index()?;
                obs.set_wait(wait);

                if !self.dirty.load(Ordering::Acquire) {
                    obs.set_vectors(0);
                    return Ok(false);
                }

                self.save_locked(&index)?;
                self.dirty.store(false, Ordering::Release);
                obs.set_vectors(index.size());

                log::debug!(
                    "Saved dirty USearch index to {} ({} vectors)",
                    self.path.display(),
                    index.size(),
                );
                Ok(true)
            },
        )
    }

    /// Number of vectors currently in the index.
    pub fn size(&self) -> usize {
        self.index.lock().map(|idx| idx.size()).unwrap_or(0)
    }

    /// Check if a node ID has a vector in the index.
    pub fn contains(&self, node_id: &str) -> bool {
        let key = node_id_to_key(node_id);
        self.index
            .lock()
            .map(|idx| idx.contains(key))
            .unwrap_or(false)
    }

    /// Get the configured embedding dimensions.
    pub fn dimensions(&self) -> usize {
        self.dimensions
    }

    /// Get the index file path.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Clear all vectors from the index and remove the file.
    ///
    /// Used by `reset_all_data` to wipe the knowledge graph clean.
    pub fn clear(&self) -> SCSResult<()> {
        observe_result(
            QueryBackend::Vector,
            "VectorIndex::clear",
            self.target(),
            String::new(),
            |obs| {
                let (index, wait) = self.lock_index()?;
                obs.set_wait(wait);
                let count = index.size();

                // USearch doesn't have a "clear" method — we reset by removing all keys.
                // For a full reset, it's faster to just delete the file and create a fresh index.
                index.reset().map_err(|e| {
                    SCSError::Storage(format!("failed to reset USearch index: {e}"))
                })?;

                // Remove the file on disk if it exists.
                if self.path.exists() {
                    std::fs::remove_file(&self.path)?;
                }
                self.dirty.store(false, Ordering::Release);
                obs.set_vectors(count);

                log::info!("Cleared USearch index at {}", self.path.display());
                Ok(())
            },
        )
    }

    fn mark_dirty(&self) {
        self.dirty.store(true, Ordering::Release);
    }

    /// Remove leftover `*.tmp-<random>` sidecar files from interrupted saves.
    ///
    /// `save_locked` writes to a random temp sibling and renames it into
    /// place; a process killed mid-save leaves the temp file behind. These
    /// are full index copies (potentially GBs each), so they accumulate into
    /// serious disk waste. Safe at open time: the single-instance engine
    /// guard ensures no other process is mid-save on this index.
    fn cleanup_stale_temp_files(path: &Path) {
        let Some(parent) = path.parent() else {
            return;
        };
        let Some(file_name) = path.file_name().and_then(|name| name.to_str()) else {
            return;
        };
        let temp_prefix = format!("{file_name}.tmp-");

        let Ok(entries) = std::fs::read_dir(parent) else {
            return;
        };

        let mut removed = 0usize;
        for entry in entries.flatten() {
            let entry_path = entry.path();
            let is_stale_temp = entry_path
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.starts_with(&temp_prefix));
            if !is_stale_temp {
                continue;
            }
            match std::fs::remove_file(&entry_path) {
                Ok(()) => removed += 1,
                Err(error) => log::warn!(
                    "Failed to remove stale USearch temp file {}: {error}",
                    entry_path.display()
                ),
            }
        }

        if removed > 0 {
            log::info!(
                "Removed {removed} stale USearch temp file(s) next to {}",
                path.display()
            );
        }
    }

    fn temp_path(&self) -> PathBuf {
        let file_name = self
            .path
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("index.usearch");
        self.path
            .with_file_name(format!("{file_name}.tmp-{}", random::<u64>()))
    }

    fn save_locked(&self, index: &usearch::Index) -> SCSResult<()> {
        if let Some(parent) = self.path.parent() {
            std::fs::create_dir_all(parent)?;
        }

        let temp_path = self.temp_path();
        let temp_str = temp_path.to_string_lossy();

        if temp_path.exists() {
            std::fs::remove_file(&temp_path)?;
        }

        index.save(&temp_str).map_err(|e| {
            SCSError::Storage(format!(
                "failed to save USearch index to temporary path {}: {e}",
                temp_path.display()
            ))
        })?;

        if let Err(error) = std::fs::rename(&temp_path, &self.path) {
            let _ = std::fs::remove_file(&temp_path);
            return Err(SCSError::Storage(format!(
                "failed to replace USearch index at {}: {error}",
                self.path.display()
            )));
        }

        log::debug!(
            "Atomically replaced USearch index at {} from {}",
            self.path.display(),
            temp_path.display(),
        );
        Ok(())
    }
}

impl Drop for VectorIndex {
    /// Persist the index to disk on drop as a safety net.
    ///
    /// Best effort — errors are logged but not propagated since Drop can't
    /// return errors. Callers should explicitly call `save()` for reliable
    /// persistence.
    ///
    /// Skips the save if the parent directory no longer exists (the database
    /// was deleted) or if the sidecar was explicitly removed during an
    /// embedding model dimension change — writing stale vectors back would
    /// corrupt the index for the new dimension.
    fn drop(&mut self) {
        // Guard: don't recreate a sidecar that was intentionally deleted.
        if !self.path.parent().is_some_and(|p| p.exists()) {
            return;
        }
        if !self.dirty.load(Ordering::Acquire) {
            return;
        }
        if let Ok(index) = self.index.lock() {
            if index.size() > 0 {
                if let Err(e) = self.save_locked(&index) {
                    crate::observability::record(crate::observability::QueryOperationEventInput {
                        backend: QueryBackend::Vector,
                        operation: "VectorIndex::drop_save".to_string(),
                        target: self.path.display().to_string(),
                        detail: String::new(),
                        started_at: std::time::SystemTime::now(),
                        duration: std::time::Duration::ZERO,
                        wait: None,
                        row_count: None,
                        vector_count: Some(index.size() as u64),
                        error: Some(e.to_string()),
                    });
                    log::error!(
                        "Failed to save USearch index on drop at {}: {e}",
                        self.path.display()
                    );
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_embedding(seed: u64, dim: usize) -> Vec<f32> {
        (0..dim)
            .map(|i| (seed as f32 * 0.1 + i as f32 * 0.01) % 1.0)
            .collect()
    }

    #[test]
    fn node_id_to_key_deterministic() {
        let id = "550e8400-e29b-41d4-a716-446655440000";
        let k1 = node_id_to_key(id);
        let k2 = node_id_to_key(id);
        assert_eq!(k1, k2);
        assert_ne!(k1, 0);
    }

    #[test]
    fn node_id_to_key_non_uuid_fallback() {
        let k1 = node_id_to_key("not-a-uuid");
        let k2 = node_id_to_key("also-not-a-uuid");
        assert_ne!(k1, k2);
    }

    #[test]
    fn create_and_search() {
        crate::observability::clear();
        let dir = tempfile::tempdir().unwrap();
        let idx = VectorIndex::open(dir.path().join("test.usearch"), 4).unwrap();

        let e1 = vec![1.0, 0.0, 0.0, 0.0];
        let e2 = vec![0.0, 1.0, 0.0, 0.0];
        let e3 = vec![0.9, 0.1, 0.0, 0.0]; // close to e1

        idx.add("id-1", &e1).unwrap();
        idx.add("id-2", &e2).unwrap();
        idx.add("id-3", &e3).unwrap();

        assert_eq!(idx.size(), 3);

        let results = idx.search(&[1.0, 0.0, 0.0, 0.0], 2).unwrap();
        assert_eq!(results.len(), 2);

        // Nearest should be id-1 (exact match) then id-3 (close).
        let keys: Vec<u64> = results.iter().map(|(k, _)| *k).collect();
        assert_eq!(keys[0], node_id_to_key("id-1"));
        assert_eq!(keys[1], node_id_to_key("id-3"));

        let snapshot = crate::observability::snapshot(crate::observability::QuerySnapshotFilter {
            backend: Some(QueryBackend::Vector),
            ..crate::observability::QuerySnapshotFilter::default()
        });
        assert!(snapshot.events.iter().any(|event| {
            event.operation == "VectorIndex::search" && event.vector_count == Some(2)
        }));
    }

    #[test]
    fn add_and_remove() {
        let dir = tempfile::tempdir().unwrap();
        let idx = VectorIndex::open(dir.path().join("test.usearch"), 4).unwrap();

        idx.add("id-1", &[1.0, 0.0, 0.0, 0.0]).unwrap();
        assert!(idx.contains("id-1"));
        assert_eq!(idx.size(), 1);

        idx.remove("id-1").unwrap();
        assert!(!idx.contains("id-1"));
    }

    #[test]
    fn remove_persists_before_drop() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.usearch");

        let idx = VectorIndex::open(&path, 4).unwrap();
        idx.add("id-1", &[1.0, 0.0, 0.0, 0.0]).unwrap();
        idx.add("id-2", &[0.0, 1.0, 0.0, 0.0]).unwrap();
        idx.save().unwrap();

        idx.remove("id-1").unwrap();

        let reloaded = VectorIndex::open(&path, 4).unwrap();
        assert!(!reloaded.contains("id-1"));
        assert!(reloaded.contains("id-2"));
        assert_eq!(reloaded.size(), 1);
    }

    #[test]
    fn remove_batch_persists_before_drop() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.usearch");

        let idx = VectorIndex::open(&path, 4).unwrap();
        idx.add("id-1", &[1.0, 0.0, 0.0, 0.0]).unwrap();
        idx.add("id-2", &[0.0, 1.0, 0.0, 0.0]).unwrap();
        idx.add("id-3", &[0.0, 0.0, 1.0, 0.0]).unwrap();
        idx.save().unwrap();

        idx.remove_batch(&["id-1".to_string(), "id-3".to_string()])
            .unwrap();

        let reloaded = VectorIndex::open(&path, 4).unwrap();
        assert!(!reloaded.contains("id-1"));
        assert!(reloaded.contains("id-2"));
        assert!(!reloaded.contains("id-3"));
        assert_eq!(reloaded.size(), 1);
    }

    #[test]
    fn save_and_reload() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.usearch");

        {
            let idx = VectorIndex::open(&path, 4).unwrap();
            idx.add("id-1", &[1.0, 0.0, 0.0, 0.0]).unwrap();
            idx.add("id-2", &[0.0, 1.0, 0.0, 0.0]).unwrap();
            idx.save().unwrap();
        }

        // Reload from disk.
        let idx = VectorIndex::open(&path, 4).unwrap();
        assert_eq!(idx.size(), 2);
        assert!(idx.contains("id-1"));
        assert!(idx.contains("id-2"));
    }

    #[test]
    fn filtered_search_respects_valid_keys() {
        let dir = tempfile::tempdir().unwrap();
        let idx = VectorIndex::open(dir.path().join("test.usearch"), 4).unwrap();

        idx.add("id-1", &[1.0, 0.0, 0.0, 0.0]).unwrap();
        idx.add("id-2", &[0.9, 0.1, 0.0, 0.0]).unwrap();
        idx.add("id-3", &[0.0, 1.0, 0.0, 0.0]).unwrap();

        // Only allow id-2 and id-3.
        let valid: HashSet<u64> = [node_id_to_key("id-2"), node_id_to_key("id-3")]
            .into_iter()
            .collect();

        let results = idx
            .filtered_search(&[1.0, 0.0, 0.0, 0.0], 2, &valid)
            .unwrap();

        // id-1 (exact match) should be excluded, id-2 (close) returned first.
        assert!(!results.is_empty());
        assert_eq!(results[0].0, node_id_to_key("id-2"));
    }

    #[test]
    fn upsert_replaces_vector() {
        let dir = tempfile::tempdir().unwrap();
        let idx = VectorIndex::open(dir.path().join("test.usearch"), 4).unwrap();

        idx.add("id-1", &[1.0, 0.0, 0.0, 0.0]).unwrap();
        assert_eq!(idx.size(), 1);

        // Re-add with different vector — should replace, not duplicate.
        idx.add("id-1", &[0.0, 1.0, 0.0, 0.0]).unwrap();
        assert_eq!(idx.size(), 1);

        // Search should find the updated vector.
        let results = idx.search(&[0.0, 1.0, 0.0, 0.0], 1).unwrap();
        assert_eq!(results[0].0, node_id_to_key("id-1"));
    }

    #[test]
    fn batch_add_and_save() {
        let dir = tempfile::tempdir().unwrap();
        let idx = VectorIndex::open(dir.path().join("test.usearch"), 768).unwrap();

        let pairs: Vec<(String, Vec<f32>)> = (0..100)
            .map(|i| (format!("node-{i}"), make_embedding(i, 768)))
            .collect();

        let count = idx.add_batch(&pairs).unwrap();
        assert_eq!(count, 100);
        assert_eq!(idx.size(), 100);

        idx.save().unwrap();
    }

    #[test]
    fn clear_resets_index() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.usearch");
        let idx = VectorIndex::open(&path, 4).unwrap();

        idx.add("id-1", &[1.0, 0.0, 0.0, 0.0]).unwrap();
        idx.save().unwrap();
        assert!(path.exists());

        idx.clear().unwrap();
        assert_eq!(idx.size(), 0);
        assert!(!path.exists());
    }

    #[test]
    fn open_removes_stale_temp_sidecars() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.usearch");

        // Persist a real index so open() loads it back.
        {
            let idx = VectorIndex::open(&path, 4).unwrap();
            idx.add("id-1", &[1.0, 0.0, 0.0, 0.0]).unwrap();
            idx.save().unwrap();
        }

        // Simulate interrupted saves leaving temp copies behind.
        let stale_a = dir.path().join("test.usearch.tmp-123");
        let stale_b = dir.path().join("test.usearch.tmp-456");
        std::fs::write(&stale_a, b"partial").unwrap();
        std::fs::write(&stale_b, b"partial").unwrap();
        // Unrelated files must survive cleanup.
        let unrelated = dir.path().join("other.usearch.tmp-789");
        std::fs::write(&unrelated, b"keep").unwrap();

        let reloaded = VectorIndex::open(&path, 4).unwrap();
        assert!(reloaded.contains("id-1"));
        assert!(!stale_a.exists());
        assert!(!stale_b.exists());
        assert!(unrelated.exists());
    }

    #[test]
    fn save_does_not_leave_temporary_sidecars() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.usearch");
        let idx = VectorIndex::open(&path, 4).unwrap();

        idx.add("id-1", &[1.0, 0.0, 0.0, 0.0]).unwrap();
        idx.save().unwrap();

        let temp_files: Vec<PathBuf> = std::fs::read_dir(dir.path())
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .filter(|entry_path| {
                entry_path
                    .file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| name.contains(".tmp-"))
            })
            .collect();

        assert!(
            temp_files.is_empty(),
            "unexpected temp files: {temp_files:?}"
        );
        let reloaded = VectorIndex::open(&path, 4).unwrap();
        assert!(reloaded.contains("id-1"));
    }
}
