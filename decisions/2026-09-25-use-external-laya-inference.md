---
status: accepted
project: scs
project-root: /Users/leonardo/Repositories/scs
created: 2026-09-25
updated: 2026-09-25
supersedes:
  - decisions/2026-09-23-use-native-mlx-for-query-routing.md (runtime ownership only)
  - decisions/2026-09-23-gate-daemon-readiness-on-classifier.md (worker supervision only)
superseded-by:
related-specs:
  - specs/2026-09-25-consume-external-laya-inference.md
implementation:
  commits: [e3238eb]
  pull-request:
---

# Use External Laya Inference

## Context

SCS currently carries a pinned Laya bundle installer, an MLX dependency, and a
private worker supervisor. SCS already supports external local inference for
embeddings and reranking. Keeping Laya in SCS adds model installation and MLX
process ownership to the public code intelligence service.

## Decision

An external service owns Laya's bundle, MLX runtime, warmup, and choice API.
SCS owns its playbook descriptions and strict response
validation. SCS uses a separately configured, trusted decision endpoint and
requires its model-ready response before daemon readiness. During an outage,
SCS gates new queries and requests shutdown after bounded failed recovery.

## Consequences

SCS no longer requires Apple Silicon or Laya weights to install. A configured
Laya daemon now depends on the configured service's availability. Existing MCP and index contracts
do not change. The former local worker and bundle install path are retired.
