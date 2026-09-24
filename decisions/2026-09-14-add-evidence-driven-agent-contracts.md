# Add Evidence-Driven Agent Contracts

Status: accepted

The inventory portion was superseded by the
[unified query decision](2026-09-22-unify-agent-code-queries.md); the underlying
service-route and evidence decisions remain historical implementation context.

> Superseded in part by
> `decisions/2026-09-22-unify-agent-code-queries.md`. Its search, readiness,
> risk, and metrics contracts remain authoritative internally; its fixed MCP
> inventory is replaced after the unified-query retirement gates pass.

## Context

Recent Codex history showed repeated serial searches, readiness polling without
job identity, slow regression-risk outliers, and no durable view of SCS usage.
The existing eleven-tool MCP inventory is intentionally small, and current
callers rely on the existing result fields and defaults.

## Decision

- Extend existing MCP tools with additive fields and optional parameters. Keep
  the eleven-tool inventory and existing defaults.
- Let one search accept up to five total query angles. Retrieve each angle
  concurrently, merge deterministically, and rerank the merged set once.
- Expose `fast`, `balanced`, and `thorough` search modes. Bound reranking in the
  latter two modes and return fused results on timeout or provider failure.
- Make graph statistics the repository readiness and durable-job observation
  contract. Waiting is scoped by repository and exact job ID, read-only, and
  capped at 10 seconds.
- Bound regression-risk output, hydrate dependents in one native batch, and
  attach direct edge evidence to deduplicated test-file targets.
- Record content-free hourly operation aggregates in a separate owner-only
  SQLite database under `SCS_HOME`. HMAC repository identities with a local
  owner-only key, retain 30 days, cap rows, quarantine corrupt databases, and
  fail open when metrics are unavailable.
- Expose metrics through the operational CLI and SCSWire, not MCP.

## Rejected alternatives

- Adding separate search, readiness, or metrics MCP tools would enlarge the
  model-facing inventory and duplicate existing jobs.
- Storing raw arguments, query text, paths, results, or error messages would
  create unnecessary sensitive local history.
- Waiting by globally scanning recent jobs would be slower and could confuse
  work from another repository.
- Treating suggested tests as transitive or sufficient would overstate the
  direct dependency evidence SCS currently has.

## Rationale

Additive contracts preserve compatibility while replacing common multi-call
loops with bounded single calls. Explicit diagnostics let agents distinguish
lexical fallback, enrichment failure, timeout, incomplete risk output, and
active indexing without guessing. Durable aggregates make future improvements
measurable without retaining code or conversation content.

## Consequences

- Responses are larger by a small bounded diagnostics envelope.
- Default search behavior remains thorough and full-detail for compatibility.
- Metrics files can be removed independently without affecting indexes or job
  state; they will be recreated on the next daemon start.
- Search quality and latency still require repository-specific evaluation;
  timings are observations rather than portable service-level guarantees.
