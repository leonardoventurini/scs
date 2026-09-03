# Missing-embedding repair

## Problem

An indexed repository can contain structurally indexed, hash-acknowledged files while its project vector index contains none or only some of their embeddings. Incremental ingestion currently selects work only by source-content hash, so unchanged files never regenerate missing embeddings. Search then has no semantic corpus and may fail behind `internal: SCSWire method failed internally`.

Observed evidence on 2026-09-02: `<external-project>` reports a populated structural index and zero embeddings; `knowledge.nodes.list` succeeds while each `knowledge.search` request fails internally.

## Scope and contracts

- Preserve the existing SCSWire and MCP request/response contracts.
- Generate embeddings whenever indexed nodes lack them, regardless of whether source hashes changed or the vector store is empty, partial, or absent.
- Preserve content-hash incrementality for files whose nodes all have embeddings.
- Do not mutate repository source or persisted index data as part of search.

## Uncertainty

The generic router error erases the search exception, but durable job history independently shows repeated `Reopened vector sidecar is missing an acknowledged batch vector` failures. A regression test will isolate the unchanged-hash/missing-vector scheduling defect before implementation.

## Risks and recovery

The main risk is re-embedding more files than necessary. Select only files owning at least one missing vector, and retain the existing complete-file durability boundary. Recovery is a normal revert of the task commit; there is no migration or persisted-data rollback.

## Executable checklist

- [x] Add a pipeline regression test with an acknowledged, unchanged file whose nodes have no vectors.
- [x] Confirm the current pipeline incorrectly reports zero changed files and generates no embeddings.
- [x] Include files with missing node embeddings in incremental ingestion work.
- [x] Run targeted Python tests and strict type checking.
- [x] Run `just verify`.
- [ ] Restart `gui/501/com.mentagen.scs.daemon` and confirm readiness.
- [ ] Repeat the previously failing semantic searches successfully.

## Direct rollout and verification

Deploy through the repository's existing editable virtual environment and restart only the explicit per-user daemon LaunchAgent. Verify the LaunchAgent is running, allow the durable repair job to repair its semantic index, confirm nonzero embedding coverage, and repeat the original semantic queries successfully.
