//! Ingested file tracking for incremental code ingestion.
//!
//! Records which files have been indexed, their content hashes, and
//! timestamps. Used by the ingestion pipeline to skip unchanged files
//! and clean up stale entries when files are deleted.

use std::collections::HashMap;

use rusqlite::params;
use serde::Serialize;

use scs_core::error::SCSResult;

use crate::connection::ConnectionPool;
use crate::observability::{observe_result, QueryBackend};
use crate::vector_index::VectorIndex;
use crate::PoolResultExt;

/// Per-repo ingestion statistics.
#[derive(Debug, Clone)]
pub struct IngestionStats {
    pub file_count: i64,
    pub last_indexed: String,
}

/// Result of dropping an entire repo's index from the knowledge graph.
///
/// Returned by `delete_repo` to report how many artifacts were cleaned up.
/// The counts help the UI confirm the operation scope to the user.
#[derive(Debug, Clone, Serialize)]
pub struct DeleteRepoResult {
    pub files_removed: i64,
    pub nodes_removed: i64,
    pub embeddings_removed: i64,
}

/// Get the content hash of a previously ingested file.
///
/// Returns `None` if the file hasn't been ingested yet.
pub fn get_ingested_file_hash(
    pool: &ConnectionPool,
    repo_path: &str,
    rel_path: &str,
) -> SCSResult<Option<String>> {
    let conn = pool.get().pool_err()?;
    let result = conn.query_row(
        "SELECT content_hash FROM ingested_files WHERE repo_path = ?1 AND rel_path = ?2",
        params![repo_path, rel_path],
        |row| row.get(0),
    );

    match result {
        Ok(hash) => Ok(Some(hash)),
        Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
        Err(e) => Err(e.into()),
    }
}

/// Record or update a file's ingestion metadata.
pub fn upsert_ingested_file(
    pool: &ConnectionPool,
    file_id: &str,
    repo_path: &str,
    rel_path: &str,
    language: &str,
    content_hash: &str,
    byte_size: i64,
) -> SCSResult<()> {
    let conn = pool.get().pool_err()?;
    conn.execute(
        "INSERT INTO ingested_files (id, repo_path, rel_path, language, content_hash, byte_size)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)
         ON CONFLICT(repo_path, rel_path) DO UPDATE SET
             content_hash = excluded.content_hash,
             byte_size = excluded.byte_size,
             indexed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
        params![
            file_id,
            repo_path,
            rel_path,
            language,
            content_hash,
            byte_size
        ],
    )?;
    Ok(())
}

/// Get all ingested file paths and their hashes for a repo.
///
/// Returns a map of `rel_path → content_hash`.
pub fn get_all_ingested_files(
    pool: &ConnectionPool,
    repo_path: &str,
) -> SCSResult<HashMap<String, String>> {
    let conn = pool.get().pool_err()?;
    let mut stmt =
        conn.prepare("SELECT rel_path, content_hash FROM ingested_files WHERE repo_path = ?1")?;

    let map = stmt
        .query_map(params![repo_path], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?
        .collect::<Result<HashMap<_, _>, _>>()?;

    Ok(map)
}

/// Get per-repo ingestion stats: file count and last indexed timestamp.
///
/// Merges data from both `ingested_files` (file-level tracking) and the
/// `repos` table (canonical repo registry). This ensures repos still
/// appear in the UI after `clear_ingestion_hashes` empties the tracking
/// table — the nodes and edges survive, only the incremental-ingest
/// metadata is gone.
pub fn get_ingestion_stats(pool: &ConnectionPool) -> SCSResult<HashMap<String, IngestionStats>> {
    observe_result(
        QueryBackend::Sqlite,
        "get_ingestion_stats",
        "ingested_files",
        String::new(),
        |obs| {
            let wait_started = std::time::Instant::now();
            let conn = pool.get().pool_err()?;
            obs.set_wait(wait_started.elapsed());

            // LEFT JOIN ensures repos with 0 tracked files still appear.
            let mut stmt = conn.prepare(
                "SELECT r.path, COUNT(f.repo_path) AS file_count, MAX(f.indexed_at) AS last_indexed
                 FROM repos r
                 LEFT JOIN ingested_files f ON f.repo_path = r.path
                 GROUP BY r.path",
            )?;

            let map = stmt
                .query_map([], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        IngestionStats {
                            file_count: row.get(1)?,
                            // NULL when repo has no tracked files (e.g., after clear_ingestion_hashes).
                            last_indexed: row.get::<_, Option<String>>(2)?.unwrap_or_default(),
                        },
                    ))
                })?
                .collect::<Result<HashMap<_, _>, _>>()?;

            obs.set_rows(map.len());
            Ok(map)
        },
    )
}

/// Collect all node IDs associated with a given file path.
///
/// Queries node metadata for the `file_path` key to find entities
/// that were ingested from this source file.  Used by the pipeline
/// to identify stale entities that should be deleted after re-parsing
/// (i.e., entities that existed before but no longer appear in the file).
pub fn get_node_ids_for_file(
    pool: &ConnectionPool,
    repo_path: &str,
    rel_path: &str,
) -> SCSResult<Vec<String>> {
    let conn = pool.get().pool_err()?;
    let mut stmt = conn.prepare(
        "SELECT n.id
         FROM nodes n
         LEFT JOIN repos r ON r.id = n.repo_id
         WHERE json_extract(n.metadata, '$.file_path') = ?2
           AND (r.path = ?1 OR json_extract(n.metadata, '$.repo_path') = ?1)",
    )?;
    let ids = stmt
        .query_map(params![repo_path, rel_path], |row| row.get::<_, String>(0))?
        .collect::<Result<Vec<_>, _>>()?;
    Ok(ids)
}

/// Return every file path currently represented by nodes for a repo.
///
/// This intentionally reads from `nodes`, not `ingested_files`, so a full
/// ingest can clean up orphaned nodes left behind after ingestion tracking was
/// cleared or after ignore rules changed.
pub fn get_file_paths_for_repo(pool: &ConnectionPool, repo_path: &str) -> SCSResult<Vec<String>> {
    let conn = pool.get().pool_err()?;
    let mut stmt = conn.prepare(
        "SELECT DISTINCT json_extract(n.metadata, '$.file_path')
         FROM nodes n
         LEFT JOIN repos r ON r.id = n.repo_id
         WHERE json_extract(n.metadata, '$.file_path') IS NOT NULL
           AND json_extract(n.metadata, '$.file_path') != ''
           AND (r.path = ?1 OR json_extract(n.metadata, '$.repo_path') = ?1)",
    )?;
    let paths = stmt
        .query_map(params![repo_path], |row| row.get::<_, String>(0))?
        .collect::<Result<Vec<_>, _>>()?;
    Ok(paths)
}

/// Remove only the ingested_files tracking record for a file.
///
/// Unlike `delete_ingested_file`, this does NOT delete the associated
/// nodes or embeddings. Used when the pipeline will replace the affected
/// subgraph after invalidating its prior ingestion hash.
pub fn delete_ingestion_record(
    pool: &ConnectionPool,
    repo_path: &str,
    rel_path: &str,
) -> SCSResult<()> {
    let conn = pool.get().pool_err()?;
    conn.execute(
        "DELETE FROM ingested_files WHERE repo_path = ?1 AND rel_path = ?2",
        params![repo_path, rel_path],
    )?;
    Ok(())
}

/// Remove an ingested file record and its associated nodes.
///
/// Looks up all nodes that were created from this file (stored in
/// node metadata as `file_path`) and deletes them, which CASCADEs
/// to their edges. Embeddings are removed from the USearch vector index.
pub fn delete_ingested_file(
    pool: &ConnectionPool,
    vector_index: &VectorIndex,
    repo_path: &str,
    rel_path: &str,
) -> SCSResult<()> {
    observe_result(
        QueryBackend::Maintenance,
        "delete_ingested_file",
        "ingested_files",
        "rel_path=<redacted>".to_string(),
        |obs| {
            let wait_started = std::time::Instant::now();
            let conn = pool.get().pool_err()?;
            obs.set_wait(wait_started.elapsed());

            // Collect node IDs to remove from USearch.
            let ids: Vec<String> = {
                let mut stmt = conn.prepare(
                    "SELECT n.id
                     FROM nodes n
                     LEFT JOIN repos r ON r.id = n.repo_id
                     WHERE json_extract(n.metadata, '$.file_path') = ?2
                       AND (r.path = ?1 OR json_extract(n.metadata, '$.repo_path') = ?1)",
                )?;
                let result = stmt
                    .query_map(params![repo_path, rel_path], |row| row.get(0))?
                    .collect::<Result<Vec<_>, _>>()?;
                result
            };

            // Remove vectors from USearch.
            vector_index.remove_batch(&ids)?;

            // Delete nodes associated with this file (edges CASCADE).
            let nodes_removed = conn.execute(
                "DELETE FROM nodes
                 WHERE json_extract(metadata, '$.file_path') = ?2
                   AND (
                     repo_id = (SELECT id FROM repos WHERE path = ?1)
                     OR json_extract(metadata, '$.repo_path') = ?1
                   )",
                params![repo_path, rel_path],
            )?;

            // Delete the ingestion record.
            conn.execute(
                "DELETE FROM ingested_files WHERE repo_path = ?1 AND rel_path = ?2",
                params![repo_path, rel_path],
            )?;

            obs.set_rows(nodes_removed);
            obs.set_vectors(ids.len());
            Ok(())
        },
    )
}

/// Drop an entire repo's index — all ingested files, their nodes, and embeddings.
///
/// Scans `ingested_files` for every file belonging to `repo_path`, then bulk-deletes
/// the associated embeddings, nodes (which CASCADEs to edges), and ingestion records.
/// Uses a single transaction for atomicity — either everything is cleaned up or nothing.
///
/// Returns `DeleteRepoResult` with counts so the UI can confirm the operation scope.
pub fn delete_repo(
    pool: &ConnectionPool,
    vector_index: &VectorIndex,
    repo_path: &str,
) -> SCSResult<DeleteRepoResult> {
    observe_result(
        QueryBackend::Maintenance,
        "delete_repo",
        "repos",
        "repo_path=<redacted>".to_string(),
        |obs| {
            let wait_started = std::time::Instant::now();
            let conn = pool.get().pool_err()?;
            obs.set_wait(wait_started.elapsed());

            // Collect all relative paths for this repo before mutating.
            let rel_paths: Vec<String> = {
                let mut stmt =
                    conn.prepare("SELECT rel_path FROM ingested_files WHERE repo_path = ?1")?;
                let rows = stmt
                    .query_map(params![repo_path], |row| row.get::<_, String>(0))?
                    .collect::<Result<Vec<_>, _>>()?;
                rows
            };

            let files_removed = rel_paths.len() as i64;

            // Look up the repo's row ID — needed for FK-safe deletion of nodes
            // that reference the repo via the `repo_id` FK column.
            let repo_row_id: Option<i64> = {
                let result = conn.query_row(
                    "SELECT id FROM repos WHERE path = ?1",
                    params![repo_path],
                    |row| row.get(0),
                );
                match result {
                    Ok(id) => Some(id),
                    Err(rusqlite::Error::QueryReturnedNoRows) => None,
                    Err(e) => return Err(e.into()),
                }
            };

            // Collect ALL node IDs for this repo — using repo_id FK (covers all
            // nodes including file-scoped ones) plus metadata.repo_path fallback
            // for nodes that may not have repo_id set.
            let all_node_ids: Vec<String> = {
                let mut ids = Vec::new();

                // Nodes linked via repo_id FK (the authoritative source).
                if let Some(rid) = repo_row_id {
                    let mut stmt = conn.prepare("SELECT id FROM nodes WHERE repo_id = ?1")?;
                    let rows = stmt
                        .query_map(params![rid], |row| row.get::<_, String>(0))?
                        .collect::<Result<Vec<_>, _>>()?;
                    ids.extend(rows);
                }

                // Nodes linked only via metadata.repo_path (COMMIT, watermark, etc.).
                let mut stmt = conn.prepare(
                    "SELECT id FROM nodes WHERE json_extract(metadata, '$.repo_path') = ?1",
                )?;
                let rows = stmt
                    .query_map(params![repo_path], |row| row.get::<_, String>(0))?
                    .collect::<Result<Vec<_>, _>>()?;

                // Deduplicate — some nodes may match both conditions.
                let existing: std::collections::HashSet<String> = ids.iter().cloned().collect();
                for id in rows {
                    if !existing.contains(&id) {
                        ids.push(id);
                    }
                }

                ids
            };

            // Remove all vectors from USearch.
            vector_index.remove_batch(&all_node_ids)?;

            let tx = conn.unchecked_transaction()?;

            // Delete all nodes for this repo (edges CASCADE-delete automatically).
            // Use repo_id FK for the bulk delete — it's indexed and covers all
            // file-scoped nodes. Then mop up metadata.repo_path-only nodes.
            let mut nodes_removed: i64 = 0;
            if let Some(rid) = repo_row_id {
                nodes_removed +=
                    tx.execute("DELETE FROM nodes WHERE repo_id = ?1", params![rid])? as i64;
            }
            nodes_removed += tx.execute(
                "DELETE FROM nodes WHERE json_extract(metadata, '$.repo_path') = ?1",
                params![repo_path],
            )? as i64;

            // Delete ingestion tracking records.
            tx.execute(
                "DELETE FROM ingested_files WHERE repo_path = ?1",
                params![repo_path],
            )?;

            // Delete the repo registry entry — safe now that all FK references are gone.
            tx.execute("DELETE FROM repos WHERE path = ?1", params![repo_path])?;

            tx.commit()?;

            obs.set_rows(nodes_removed as usize);
            obs.set_vectors(all_node_ids.len());
            Ok(DeleteRepoResult {
                files_removed,
                nodes_removed,
                embeddings_removed: all_node_ids.len() as i64,
            })
        },
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::connection::create_test_pool;
    use crate::schema::initialize_schema;

    fn setup() -> (tempfile::TempDir, ConnectionPool, VectorIndex) {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("test.db");
        let pool = create_test_pool(&db_path).unwrap();
        let conn = pool.get().unwrap();
        initialize_schema(&conn).unwrap();
        let index = VectorIndex::open(db_path.with_extension("usearch"), 384).unwrap();
        (dir, pool, index)
    }

    /// Register a repo in the `repos` table (mirrors `get_or_create_repo`).
    fn ensure_repo(pool: &ConnectionPool, path: &str) {
        let conn = pool.get().unwrap();
        conn.execute(
            "INSERT OR IGNORE INTO repos (path) VALUES (?1)",
            params![path],
        )
        .unwrap();
    }

    fn repo_id(pool: &ConnectionPool, path: &str) -> i64 {
        let conn = pool.get().unwrap();
        conn.query_row(
            "SELECT id FROM repos WHERE path = ?1",
            params![path],
            |row| row.get(0),
        )
        .unwrap()
    }

    #[test]
    fn ingestion_stats_empty() {
        let (_dir, pool, _vi) = setup();
        let stats = get_ingestion_stats(&pool).unwrap();
        assert!(stats.is_empty());
    }

    /// A repo registered in the repos table but with no ingested_files
    /// entries should still appear in stats (file_count = 0). This happens
    /// after clear_ingestion_hashes — nodes survive but tracking is gone.
    #[test]
    fn ingestion_stats_repo_without_files() {
        let (_dir, pool, _vi) = setup();
        let repo = "/Users/me/Repos/SCS";
        ensure_repo(&pool, repo);

        let stats = get_ingestion_stats(&pool).unwrap();
        assert!(stats.contains_key(repo));
        assert_eq!(stats[repo].file_count, 0);
    }

    #[test]
    fn ingestion_stats_single_repo() {
        let (_dir, pool, _vi) = setup();
        let repo = "/Users/me/Repos/SCS";
        ensure_repo(&pool, repo);

        for i in 0..3 {
            upsert_ingested_file(
                &pool,
                &format!("file-{i}"),
                repo,
                &format!("src/module_{i}.py"),
                "python",
                &format!("hash{i}"),
                100 * (i + 1),
            )
            .unwrap();
        }

        let stats = get_ingestion_stats(&pool).unwrap();
        assert!(stats.contains_key(repo));
        assert_eq!(stats[repo].file_count, 3);
        assert!(!stats[repo].last_indexed.is_empty());
    }

    #[test]
    fn ingestion_stats_multiple_repos() {
        let (_dir, pool, _vi) = setup();
        let repo_a = "/Users/me/Repos/Alpha";
        let repo_b = "/Users/me/Repos/Beta";
        ensure_repo(&pool, repo_a);
        ensure_repo(&pool, repo_b);

        upsert_ingested_file(&pool, "a-1", repo_a, "main.py", "python", "h1", 200).unwrap();
        for i in 0..5 {
            upsert_ingested_file(
                &pool,
                &format!("b-{i}"),
                repo_b,
                &format!("src/{i}.rs"),
                "rust",
                &format!("h{i}"),
                300,
            )
            .unwrap();
        }

        let stats = get_ingestion_stats(&pool).unwrap();
        assert_eq!(stats[repo_a].file_count, 1);
        assert_eq!(stats[repo_b].file_count, 5);
    }

    #[test]
    fn hash_lookup_returns_none_for_unknown() {
        let (_dir, pool, _vi) = setup();
        let hash = get_ingested_file_hash(&pool, "/repo", "unknown.py").unwrap();
        assert!(hash.is_none());
    }

    #[test]
    fn hash_lookup_returns_stored_hash() {
        let (_dir, pool, _vi) = setup();
        upsert_ingested_file(&pool, "f1", "/repo", "main.py", "python", "abc123", 100).unwrap();
        let hash = get_ingested_file_hash(&pool, "/repo", "main.py").unwrap();
        assert_eq!(hash, Some("abc123".to_string()));
    }

    /// get_node_ids_for_file returns node IDs whose metadata.file_path
    /// matches the given relative path.
    #[test]
    fn get_node_ids_for_file_finds_matching_nodes() {
        let (_dir, pool, _vi) = setup();
        let repo = "/repo/alpha";
        ensure_repo(&pool, repo);
        let repo_id = repo_id(&pool, repo);

        // Insert nodes with file_path metadata via raw SQL since we're
        // testing the ingestion_files module, not the graph upsert.
        let conn = pool.get().unwrap();
        conn.execute(
            "INSERT INTO nodes (id, type, name, content, metadata, repo_id) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            rusqlite::params!["n1", "function", "foo", "", r#"{"file_path":"src/lib.py"}"#, repo_id],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO nodes (id, type, name, content, metadata, repo_id) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            rusqlite::params!["n2", "class", "Bar", "", r#"{"file_path":"src/lib.py"}"#, repo_id],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO nodes (id, type, name, content, metadata, repo_id) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            rusqlite::params![
                "n3",
                "function",
                "baz",
                "",
                r#"{"file_path":"src/other.py"}"#,
                repo_id
            ],
        )
        .unwrap();

        let ids = get_node_ids_for_file(&pool, repo, "src/lib.py").unwrap();
        assert_eq!(ids.len(), 2);
        assert!(ids.contains(&"n1".to_string()));
        assert!(ids.contains(&"n2".to_string()));

        // Different file should not return the first file's nodes.
        let ids = get_node_ids_for_file(&pool, repo, "src/other.py").unwrap();
        assert_eq!(ids.len(), 1);
        assert!(ids.contains(&"n3".to_string()));

        // Non-existent file returns empty.
        let ids = get_node_ids_for_file(&pool, repo, "src/missing.py").unwrap();
        assert!(ids.is_empty());
    }

    #[test]
    fn file_node_helpers_are_repo_scoped() {
        let (_dir, pool, vi) = setup();
        let repo_a = "/repo/alpha";
        let repo_b = "/repo/beta";
        ensure_repo(&pool, repo_a);
        ensure_repo(&pool, repo_b);

        insert_node_with_embedding(&pool, &vi, "a1", "function", "foo", "src/shared.py", repo_a);
        insert_node_with_embedding(&pool, &vi, "b1", "function", "foo", "src/shared.py", repo_b);
        insert_node_with_embedding(
            &pool,
            &vi,
            "a2",
            "class",
            "OnlyAlpha",
            "src/alpha.py",
            repo_a,
        );

        let ids = get_node_ids_for_file(&pool, repo_a, "src/shared.py").unwrap();
        assert_eq!(ids, vec!["a1".to_string()]);

        let paths = get_file_paths_for_repo(&pool, repo_a).unwrap();
        assert_eq!(
            paths.into_iter().collect::<std::collections::HashSet<_>>(),
            ["src/shared.py".to_string(), "src/alpha.py".to_string()]
                .into_iter()
                .collect()
        );

        delete_ingested_file(&pool, &vi, repo_a, "src/shared.py").unwrap();
        let conn = pool.get().unwrap();
        let remaining: Vec<String> = conn
            .prepare("SELECT id FROM nodes ORDER BY id")
            .unwrap()
            .query_map([], |row| row.get::<_, String>(0))
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert_eq!(remaining, vec!["a2".to_string(), "b1".to_string()]);
    }

    /// delete_ingestion_record removes only the tracking record, leaving
    /// the associated nodes intact.
    #[test]
    fn delete_ingestion_record_preserves_nodes() {
        let (_dir, pool, _vi) = setup();

        // Create an ingestion record and a node with matching file_path.
        upsert_ingested_file(&pool, "f1", "/repo", "src/main.py", "python", "hash1", 100).unwrap();
        let conn = pool.get().unwrap();
        conn.execute(
            "INSERT INTO nodes (id, type, name, content, metadata) VALUES (?1, ?2, ?3, ?4, ?5)",
            rusqlite::params![
                "n1",
                "function",
                "main",
                "",
                r#"{"file_path":"src/main.py"}"#
            ],
        )
        .unwrap();

        // Delete the ingestion record only.
        delete_ingestion_record(&pool, "/repo", "src/main.py").unwrap();

        // Ingestion record is gone.
        let hash = get_ingested_file_hash(&pool, "/repo", "src/main.py").unwrap();
        assert!(hash.is_none());

        // Node still exists.
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM nodes WHERE id = 'n1'", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(count, 1);
    }

    // ── delete_repo tests ───────────────────────────────────────────

    /// Helper: insert a node with file_path metadata, repo_id FK, and a fake embedding.
    fn insert_node_with_embedding(
        pool: &ConnectionPool,
        vi: &VectorIndex,
        id: &str,
        node_type: &str,
        name: &str,
        file_path: &str,
        repo_path: &str,
    ) {
        let conn = pool.get().unwrap();
        let repo_id = conn
            .query_row(
                "SELECT id FROM repos WHERE path = ?1",
                params![repo_path],
                |row| row.get::<_, i64>(0),
            )
            .unwrap();

        conn.execute(
            "INSERT INTO nodes (id, type, name, content, metadata, repo_id) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            rusqlite::params![
                id,
                node_type,
                name,
                "",
                format!(r#"{{"file_path":"{}"}}"#, file_path),
                repo_id,
            ],
        )
        .unwrap();

        let zeros = vec![0.0f32; 384];
        vi.add(id, &zeros).unwrap();
    }

    /// delete_repo on a repo with no indexed files returns zero counts.
    #[test]
    fn delete_repo_empty() {
        let (_dir, pool, vi) = setup();
        let result = delete_repo(&pool, &vi, "/nonexistent/repo").unwrap();
        assert_eq!(result.files_removed, 0);
        assert_eq!(result.nodes_removed, 0);
        assert_eq!(result.embeddings_removed, 0);
    }

    /// delete_repo removes all files, nodes, and embeddings for a single repo.
    #[test]
    fn delete_repo_single_repo() {
        let (_dir, pool, vi) = setup();
        let repo = "/Users/me/Repos/Alpha";
        ensure_repo(&pool, repo);

        // Ingest two files with nodes.
        upsert_ingested_file(&pool, "f1", repo, "src/a.py", "python", "h1", 100).unwrap();
        upsert_ingested_file(&pool, "f2", repo, "src/b.py", "python", "h2", 200).unwrap();

        insert_node_with_embedding(&pool, &vi, "n1", "function", "foo", "src/a.py", repo);
        insert_node_with_embedding(&pool, &vi, "n2", "class", "Bar", "src/a.py", repo);
        insert_node_with_embedding(&pool, &vi, "n3", "function", "baz", "src/b.py", repo);

        let result = delete_repo(&pool, &vi, repo).unwrap();
        assert_eq!(result.files_removed, 2);
        assert_eq!(result.nodes_removed, 3);
        assert_eq!(result.embeddings_removed, 3);

        // Verify ingestion records are gone.
        let stats = get_ingestion_stats(&pool).unwrap();
        assert!(!stats.contains_key(repo));

        // Verify nodes are gone.
        let conn = pool.get().unwrap();
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM nodes", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 0);

        // Verify embeddings are gone.
        assert_eq!(vi.size(), 0);
    }

    /// delete_repo only removes the target repo — other repos remain intact.
    #[test]
    fn delete_repo_multi_repo_isolation() {
        let (_dir, pool, vi) = setup();
        let repo_a = "/Users/me/Repos/Alpha";
        let repo_b = "/Users/me/Repos/Beta";
        ensure_repo(&pool, repo_a);
        ensure_repo(&pool, repo_b);

        // Ingest files for both repos.
        upsert_ingested_file(&pool, "a1", repo_a, "src/a.py", "python", "h1", 100).unwrap();
        upsert_ingested_file(&pool, "b1", repo_b, "src/b.rs", "rust", "h2", 200).unwrap();
        upsert_ingested_file(&pool, "b2", repo_b, "src/c.rs", "rust", "h3", 300).unwrap();

        insert_node_with_embedding(&pool, &vi, "na", "function", "alpha_fn", "src/a.py", repo_a);
        insert_node_with_embedding(&pool, &vi, "nb1", "function", "beta_fn", "src/b.rs", repo_b);
        insert_node_with_embedding(&pool, &vi, "nb2", "class", "BetaClass", "src/c.rs", repo_b);

        // Delete repo_a — repo_b should be untouched.
        let result = delete_repo(&pool, &vi, repo_a).unwrap();
        assert_eq!(result.files_removed, 1);
        assert_eq!(result.nodes_removed, 1);
        assert_eq!(result.embeddings_removed, 1);

        // repo_b still has its ingestion records.
        let stats = get_ingestion_stats(&pool).unwrap();
        assert!(!stats.contains_key(repo_a));
        assert_eq!(stats[repo_b].file_count, 2);

        // repo_b nodes still exist.
        let conn = pool.get().unwrap();
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM nodes", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 2);

        // repo_b embeddings still exist.
        assert_eq!(vi.size(), 2);
    }

    /// Helper: insert a node with repo_path metadata (for COMMIT / watermark nodes).
    fn insert_repo_path_node(
        conn: &rusqlite::Connection,
        vi: &VectorIndex,
        id: &str,
        node_type: &str,
        name: &str,
        repo_path: &str,
    ) {
        conn.execute(
            "INSERT INTO nodes (id, type, name, content, metadata) VALUES (?1, ?2, ?3, ?4, ?5)",
            rusqlite::params![
                id,
                node_type,
                name,
                "",
                format!(r#"{{"repo_path":"{}"}}"#, repo_path)
            ],
        )
        .unwrap();

        // Insert a zero embedding into USearch for cleanup verification.
        let zeros = vec![0.0f32; 384];
        vi.add(id, &zeros).unwrap();
    }

    /// delete_repo cleans up all nodes (both repo_id-linked and repo_path-scoped)
    /// and the repos table entry even when ingested_files is empty (e.g., after
    /// clear_ingestion_hashes). This is the scenario that caused the FK constraint
    /// failure — nodes with repo_id FK still reference the repos row.
    #[test]
    fn delete_repo_after_clear_hashes() {
        let (_dir, pool, vi) = setup();
        let repo = "/Users/me/Repos/Alpha";
        ensure_repo(&pool, repo);

        // Simulate post-ingestion state after clear_ingestion_hashes:
        // - File-scoped nodes with repo_id FK still exist (ingested code entities).
        // - repo_path-scoped nodes exist (commits, watermarks).
        // - ingested_files table is empty.
        insert_node_with_embedding(&pool, &vi, "n1", "function", "foo", "src/a.py", repo);
        insert_node_with_embedding(&pool, &vi, "n2", "class", "Bar", "src/a.py", repo);
        let conn = pool.get().unwrap();
        insert_repo_path_node(&conn, &vi, "commit1", "commit", "feat: add foo", repo);

        // Verify repo shows up in stats before deletion.
        let stats = get_ingestion_stats(&pool).unwrap();
        assert!(stats.contains_key(repo));
        assert_eq!(stats[repo].file_count, 0);

        // Delete should succeed despite no ingested_files entries — must not
        // hit FOREIGN KEY constraint failed.
        let result = delete_repo(&pool, &vi, repo).unwrap();
        assert_eq!(result.files_removed, 0);
        assert_eq!(result.nodes_removed, 3, "2 file nodes + 1 commit node");
        assert_eq!(result.embeddings_removed, 3);

        // Repo no longer in stats.
        let stats = get_ingestion_stats(&pool).unwrap();
        assert!(!stats.contains_key(repo));

        // All nodes gone.
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM nodes", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 0);
    }

    /// delete_repo removes repo_path-scoped nodes (COMMIT, watermark) in
    /// addition to file_path-scoped code entity nodes.
    #[test]
    fn delete_repo_removes_repo_path_scoped_nodes() {
        let (_dir, pool, vi) = setup();
        let repo = "/Users/me/Repos/Alpha";
        ensure_repo(&pool, repo);

        // Ingest one file with a code entity node.
        upsert_ingested_file(&pool, "f1", repo, "src/a.py", "python", "h1", 100).unwrap();
        insert_node_with_embedding(&pool, &vi, "n1", "function", "foo", "src/a.py", repo);

        // Add two provenance nodes that use metadata.repo_path instead of
        // metadata.file_path.
        let conn = pool.get().unwrap();
        insert_repo_path_node(&conn, &vi, "commit1", "commit", "feat: add foo", repo);
        insert_repo_path_node(
            &conn,
            &vi,
            "watermark1",
            "commit",
            "git_history_watermark",
            repo,
        );

        let result = delete_repo(&pool, &vi, repo).unwrap();

        // 1 file + 1 code node + 2 repo_path-scoped nodes.
        assert_eq!(result.files_removed, 1);
        assert_eq!(result.nodes_removed, 3, "code entity + commit + watermark");
        assert_eq!(result.embeddings_removed, 3);

        // All nodes gone.
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM nodes", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 0);

        // All embeddings gone.
        assert_eq!(vi.size(), 0);
    }

    /// repo_path-scoped node deletion respects repo isolation — nodes for
    /// other repos are not affected.
    #[test]
    fn delete_repo_repo_path_isolation() {
        let (_dir, pool, vi) = setup();
        let repo_a = "/Users/me/Repos/Alpha";
        let repo_b = "/Users/me/Repos/Beta";
        ensure_repo(&pool, repo_a);
        ensure_repo(&pool, repo_b);

        // Both repos have ingested files.
        upsert_ingested_file(&pool, "a1", repo_a, "src/a.py", "python", "h1", 100).unwrap();
        upsert_ingested_file(&pool, "b1", repo_b, "src/b.py", "python", "h2", 200).unwrap();

        insert_node_with_embedding(&pool, &vi, "na", "function", "alpha_fn", "src/a.py", repo_a);
        insert_node_with_embedding(&pool, &vi, "nb", "function", "beta_fn", "src/b.py", repo_b);

        // COMMIT nodes for each repo.
        let conn = pool.get().unwrap();
        insert_repo_path_node(&conn, &vi, "ca", "commit", "feat: alpha", repo_a);
        insert_repo_path_node(&conn, &vi, "cb", "commit", "feat: beta", repo_b);

        // Delete repo_a.
        let result = delete_repo(&pool, &vi, repo_a).unwrap();
        assert_eq!(result.files_removed, 1);
        assert_eq!(result.nodes_removed, 2, "code entity + commit for alpha");
        assert_eq!(result.embeddings_removed, 2);

        // repo_b nodes and commits still exist.
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM nodes", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 2, "beta code entity + beta commit");

        assert_eq!(vi.size(), 2);
    }
}
