# Manual acceptance: project-scoped storage

## Goal and scope

Exercise the live SCS daemon and public MCP tools after the project-store
cutover. This is a read-mostly acceptance round; it may queue a normal explicit
reindex of this checkout, but it must not modify repository source or destroy
the verified legacy archive.

## Evidence, risk, and recovery

- Project: `scs`
- Project root: `/Users/leonardo/Repositories/mentagen/scs`
- Risk tier: medium, because testing crosses the live daemon, MCP proxy, OMLX,
  and persistent job/catalog state.
- Main uncertainty: route-level isolation can look correct in unit tests while
  a live request accidentally opens a shared or newly-created store.
- Abort condition: daemon unavailability, a failed durable job, any result from
  a second repository in this project's search, or any repository-source diff.
- Recovery: stop testing; preserve `~/.scs` evidence and restore the verified
  archive only if active data has been damaged. Normal reindex jobs are safe to
  let finish or to cancel through their durable job record.

## Contracts and verification ledger

- Project readiness — a scoped stats request reports ready nodes, equal
  embedding/vector counts, and `vector_index_scope: project`; verify through
  `get_graph_stats` and a semantic `search_code` request.
- Store containment — an unindexed root returns empty/read-only status and
  creates no catalog/store directory; verify project-directory count before and
  after the MCP request. This is the negative control for the highest-risk
  oracle.
- Durable lifecycle — an explicit reindex is accepted, carries the exact store
  binding, reaches `completed`, and its catalog state becomes `semantic_ready`;
  verify `jobs.db` and `catalog.db` read-only queries.
- Service resilience — daemon health succeeds after the tests; verify
  `uv run --all-groups scs doctor`.
- Source immutability — test activity leaves the checkout clean; verify
  `git status --short`.

## Execution evidence

- Live MCP graph stats reported `semantic_ready`, 2,531 nodes, 2,531
  embeddings, 2,531 vector entries, and project-scoped vectors. A natural
  language semantic search returned `StoreBinding` as its best match.
- `list_symbols`, `graph_context`, `get_related`, `inspect_file`,
  `regression_risk_report`, and `find_references` all returned valid public
  MCP responses. Inspection correctly advertised node truncation rather than
  silently dropping content.
- Explicit `ingest_files` job `ingest_db7b2d9d27f7` completed with the active
  `store_id` and generation. Its no-op result recorded one discovered file and
  zero structural or embedding changes.
- A scoped stats request for `/private/tmp/scs-manual-unindexed.Yz8GPJ`
  returned `empty` / `repository is not indexed`; project and catalog counts
  both remained one before and after. The temporary directory was removed.
- Final `scs doctor` reported a ready daemon, generation
  `0eef28b874a743e1b4be76338e0ecca4`.

## Execution checklist

- [x] Confirm the live daemon and scoped semantic retrieval — verify: doctor,
  MCP graph statistics, and semantic search; done when readiness is true.
- [x] Prove a read of an unindexed root creates no project store — verify:
  project-directory listing before/after a scoped stats request; done when
  listings are identical.
- [x] Confirm the completed reindex binding and catalog lifecycle — verify:
  read-only SQLite inspection; done when job and active generation agree and
  catalog state is `semantic_ready`.
- [x] Preserve operational safety — verify: final doctor and `git status
  --short`; done when daemon is ready and no source changes exist.
