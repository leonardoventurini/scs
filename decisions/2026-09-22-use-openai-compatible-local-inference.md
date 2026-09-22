---
status: accepted
project: scs
project-root: /Users/leonardo/Repositories/scs
created: 2026-09-22
updated: 2026-09-22
supersedes:
  - decisions/2026-09-05-trust-explicit-omlx-hosts.md
  - decisions/2026-09-08-activate-reranking-by-configured-model.md
superseded-by:
related-specs:
  - /Users/leonardo/Repositories/mes/specs/2026-09-22-mes-retrieval-model-serving.md
implementation:
  commits: []
  pull-request:
---

# Use One OpenAI-Compatible Local Inference Transport

## Context

SCS's local embedding and reranking clients implement portable HTTP contracts,
but their configuration, reranker module, metadata, and documentation name the
previous oMLX runtime. MES now owns the same embedding and reranking models
through its loopback-only `mlx-serve` child. Retaining the previous product name
would misrepresent ownership and couple SCS to an implementation it no longer
uses.

## Decision

Use `openai_compatible` as the sole local HTTP embedding provider identity.
Configure it with `openai_compatible_base_url` and
`openai_compatible_trusted_hosts`. A configured `reranking_model` uses the same
endpoint. Name the adapters and durable provider metadata
`openai-compatible`.

Remove the former provider value, settings, class, module, and environment
names without a compatibility alias. This deliberate breaking change ships in
a new SCS release and the local configuration is migrated during the coordinated
MES rollout.

## Rejected alternatives

- Keeping the old name as an alias was rejected because the operator explicitly
  chose a complete naming cutover.
- Naming MES in the SCS provider was rejected because the transport contract is
  portable and SCS should not depend on another product's identity.
- Giving reranking a separate endpoint was rejected because both operations use
  the same validated local model server.

## Rationale

The generalized name describes the actual interface, keeps SCS independent of
the serving implementation, and makes embedding and reranking configuration
coherent. Removing aliases prevents stale settings from appearing valid after
the ownership transfer.

## Consequences

- Existing local configurations must be migrated before running the new
  release.
- The durable provider identity changes, so incompatible vector generations
  are quarantined and an explicit reindex regenerates embeddings.
- Local endpoints remain credential-free and loopback-only unless an exact host
  is explicitly trusted.
- Provider outages continue to degrade search without disabling structural or
  lexical retrieval.
