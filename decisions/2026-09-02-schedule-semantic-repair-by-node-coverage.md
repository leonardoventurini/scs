# Schedule semantic repair by node coverage

## Context

SCS incremental ingestion treated an acknowledged source hash as sufficient evidence that a file required no work. Vector state is independently recoverable and can be absent or partial while those source hashes remain valid, leaving unchanged files permanently unembedded.

## Decision

At each ingestion pass with an embedding provider configured, SCS will inspect the active project index for nodes without embeddings and add their owning source files to the normal complete-file ingestion plan. Content changes and explicit force mode remain independent reasons to schedule the same file.

The repair reuses parsing, embedding, sidecar flush/reopen verification, source-stability validation, and atomic hash acknowledgement. It introduces no alternate semantic repair path and no public contract or persisted-format change.

## Rejected alternatives

- Falling back to lexical search alone hides incomplete indexing and does not restore semantic behavior.
- Treating an entirely empty vector index as the only repair signal leaves partially missing indexes broken.
- Requiring a destructive or explicit full reindex makes ordinary self-healing depend on operator intervention.

## Rationale

Node-level vector coverage is the authoritative evidence for semantic completeness. Converting missing-node coverage to owning files preserves the pipeline's existing complete-file durability invariant and procedurally regenerates embeddings regardless of current vector-store state.

## Consequences

An incremental pass may reparse an unchanged file when any indexed node for that file lacks a vector. Fully covered files retain hash-based fast-path behavior. Semantic repair remains retryable under the existing durable job policy.
