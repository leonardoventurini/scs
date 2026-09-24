# Consume incremental TSG vector updates

Status: accepted

## Context

Real Mentagen ingestion used oMLX correctly, but process sampling showed full
USearch reconstruction in TSG after each embedding and checkpoint write. Eight
model requests took 34.67 seconds across an interval of 676.228 seconds. An earlier
explanation attributing the delay to local model throughput was incorrect.

## Decision

Fix the owning TSG mutation implementation and consume its immutable 0.2.3 release
in SCS 0.1.8. Keep SCS's complete-file acknowledgement sequence and bounded model
requests. Reuse a current accelerator for changed vectors; preserve SQLite
commit authority and the existing sidecar persistence/recovery protocol. TSG
regressions prove repeated small batches retain the accelerator, replacements
remove old vectors, and failures cannot expose partial accelerator state as ready.

## Alternatives and consequences

Increasing model concurrency cannot remove CPU time spent reconstructing an
already indexed corpus. Deferring all index persistence until ingestion ends
would change durable acknowledgement semantics. A downstream compiled patch
would obscure ownership and release reproducibility. The upstream change preserves
public interfaces and stored formats, so no forced reindex or model change is
needed for the upgrade. Sidecar serialization still runs at commit boundaries;
this change removes repeated ANN construction, not all index I/O.

A generated debug-build benchmark using 512 dense 4,096-dimensional vectors in
sixteen 32-vector commits improved from 4.589 seconds to 1.038 seconds. This is a
bounded synthetic comparison, not a promised end-to-end ingestion speedup.

Install the published SCS wheel and verify the actual project through a fresh MCP
connection. Preserve active durable recovery work. Rollback uses a prior compatible
installer and restores the known full-rebuild performance penalty.

## Executed rollout

TSG 0.2.3 and SCS 0.1.8 published with all CI/release checks passing. The released
macOS SCS wheel was checksum-verified and installed. Recovery completed, followed
by a fresh MCP ingestion with zero changed/failed files. Independent MCP checks
confirmed 3,010 files and matching 22,829 nodes/embeddings/vectors, distinct source
aliases, and relevant semantic board-tool searches. A live post-upgrade sample
processed 980 inputs across 103.284 seconds with 98.81 seconds of model time,
removing the previous long gaps between model requests.

Daemon lifecycle timeouts during large recovery remain an observed limitation;
this change does not claim to fix them. Detailed recovery and rollback evidence is
in `specs/2026-09-05-distinct-entity-occurrences.md`.
