# Keep graph reads available during vector deletion

Status: accepted

Project: SCS (`/Users/leonardo/Repositories/scs`)

## Context

TSG rebuilt the complete vector accelerator after every node deletion. On a
shared daemon, one deletion could therefore keep a CPU busy and block graph
search/traversal long enough for MCP calls to time out with only a generic
error.

## Decision

TSG 0.2.4 removes deleted vectors incrementally and persists the same sidecar
generation. It still rebuilds when the accelerator is missing, stale, or
poisoned.

SCS adds a per-graph mutation gate around structural deletes and the graph reads
that traverse or hydrate structure. A read arriving during an admitted mutation
returns a typed `unavailable`, retryable error instead of blocking. Statistics
and durable job progress remain ungated so ingestion stays observable.

`dependencies` and `dependents` are accepted traversal aliases for `outgoing`
and `incoming`; responses use normalized traversal directions.

## Rejected alternatives

- Returning a successful empty result would hide unavailable evidence.
- Waiting indefinitely preserves availability for neither reads nor requests.
- Blocking every read, including stats, would make background ingestion harder
  to observe.

## Consequences

Normal deletions no longer trigger a full vector rebuild. During rare structural
rebuilds, affected graph reads fail immediately with a clear retry signal while
operational reads remain responsive.
