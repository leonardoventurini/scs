# Batch-committed ingestion checkpoints

## Context

SCS previously deferred every ingestion hash until the complete repository had
been parsed, embedded, and flushed. A provider failure near the end repeated
successful semantic work and left no durable recovery point.

## Decision

SCS now acknowledges source hashes per deterministic complete-file batch, only
after vectors have been persisted and verified through a fresh USearch handle.
The project-store hash remains the authoritative semantic checkpoint.

Force-full jobs additionally persist a job-bound, hash-only snapshot in the
durable jobs queue. Its mirror is reconciled from authoritative project hashes
before each retry, covering a process stop between the two database commits
without re-embedding a completed batch.

Graph structure is planned globally before semantic batching. When a retry
replaces a target file, inbound relationships from already acknowledged source
files are retained and rebound to deterministic replacement IDs.

## Consequences

- Provider failures retry only unacknowledged batches.
- Sidecar flushes fsync the temporary file and parent directory; a fresh handle
  validates additions and deletions before their ingestion records change.
- Force snapshots never persist source text or vector payloads and reject
  source-hash drift.
- Job progress remains transport-compatible while repository readiness is
  withdrawn before graph mutation and restored only after successful work.
