# Use Hybrid Reranking and Versioned Search Evaluations

> Superseded in part by
> `decisions/2026-09-08-activate-reranking-by-configured-model.md`: reranking is
> now activated by a configured model rather than a separate provider switch,
> and SCS no longer compiles a default reranker model.

## Context

The public SCS search route previously chose semantic results whenever any
existed and used lexical search only as an empty-result fallback. A separate,
unused service concatenated semantic and lexical results without learned
ranking. Consumer harnesses therefore received avoidable low-value matches and
had no standardized evidence for comparing retrieval changes.

oMLX is already an explicitly configurable loopback embedding boundary and
exposes a compatible document-reranking endpoint. The selected
Qwen3-Reranker-0.6B model is available on the target workstation, but any local
model adds latency and can be absent or malformed.

## Decision

- Always generate bounded semantic and lexical candidates and combine their
  ranks using deterministic reciprocal rank fusion.
- Keep one typed `CodeSearchService` authoritative for public retrieval.
- Add a typed oMLX reranking provider that is disabled by default, activated
  only by `reranking_provider = "omlx"`, constrained to loopback HTTP, and
  fail-open to fused retrieval.
- Keep the MCP tool inventory and full search response as the defaults. Add
  `result_detail = "compact"` as an opt-in projection on the existing tool.
- Standardize search evaluation with versioned, graded relevance suites and
  Recall@k, MRR, nDCG@k, response-byte, retrieval-mode, and repeated-latency
  reporting. Preserve raw latency samples and wait for indexing to become idle
  before measurement.
- Treat live model timings as comparison evidence rather than portable CI
  thresholds.

## Rejected alternatives

- Semantic-first concatenation was rejected because it does not combine
  independent retrieval evidence and leaves lexical signals unused.
- Automatic oMLX model discovery was rejected because a fresh SCS installation
  must not read external product state and implicit activation is difficult to
  reproduce.
- An in-process MLX reranker was rejected because it adds a production
  dependency and model-lifecycle responsibility to SCS.
- Default-on reranking was rejected because live validation measured roughly
  144 ms additional mean latency even on a tiny candidate set.
- LLM-generated result summaries were rejected from the default path because
  compact structural projection is deterministic, cheaper, and preserves
  inspectable evidence.
- A new MCP search tool was rejected because the existing tool can evolve with
  an additive optional parameter without expanding model-facing inventory.

## Rationale

Reciprocal rank fusion provides a deterministic baseline with no new runtime
dependency. Optional reranking can improve ordering where the evaluation suite
demonstrates value, while fail-open behavior preserves search availability.
Opt-in compact projection reduces harness context without removing the
established full record contract.

Versioned relevance judgments turn model selection and retrieval tuning into a
measurable comparison. Separating deterministic metric tests from live model
timings keeps CI reproducible across macOS and Linux while retaining useful
hardware-specific evidence.

## Consequences

- Default result ordering changes from semantic-only to fused retrieval when
  lexical candidates exist.
- Operators must configure and host the reranker explicitly; no model is
  downloaded or discovered by SCS.
- Reranker failures are visible through the non-reranked retrieval mode but do
  not currently add a new top-level diagnostic field to the stable MCP output.
- Repository maintainers own the quality of their relevance judgments and
  should version suites when queries or expected symbols change.
- Model comparisons must use the same suite, cutoff, result detail, repository
  index state, and repeat policy.
- No persisted index format, security boundary, or model-facing tool inventory
  changes.
