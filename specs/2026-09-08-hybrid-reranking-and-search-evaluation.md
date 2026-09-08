# Hybrid Reranking and Search Evaluation

## Problem

SCS exposes semantic code search to consumer harnesses, but the public service
route currently returns semantic matches alone whenever any are available and
uses lexical retrieval only as an empty-result fallback. A separate
`CodeSearchService` merges semantic and lexical matches, but the public route
does not use it. Neither path reranks candidates, and the full node response can
spend substantial harness context on low-value imports and empty content.

The product also lacks a repeatable, model-independent way to evaluate search
quality, response size, and latency before changing retrieval behavior.

## Evidence and uncertainty

- A live query against the indexed SCS repository returned eight import nodes
  with empty content among its first ten results. The serialized ten-result
  response was 5,067 characters; thirty results occupied 19,806 characters.
- `src/scs/services/routes.py` duplicates retrieval instead of consuming
  `src/scs/indexing/search.py`.
- oMLX exposes a Cohere/Jina-compatible `POST /v1/rerank` endpoint accepting a
  query, string documents, `top_n`, and `return_documents`, and returns ranked
  original indexes with finite relevance scores.
- The selected Qwen3 reranker is a causal-language-model reranker. Live oMLX
  compatibility and latency can vary by installed oMLX/model revision, so live
  validation is evidence rather than a deterministic CI gate.
- A useful global quality threshold cannot be selected until representative
  repositories have committed relevance judgments. The first evaluation
  contract therefore standardizes metrics and artifacts without inventing a
  universal pass score.

## Contracts

### Retrieval

- Public search always attempts lexical retrieval and attempts semantic
  retrieval when embeddings are available.
- Candidate generation is bounded by the existing 200-result public ceiling.
- Semantic and lexical rankings are fused deterministically with reciprocal
  rank fusion. Exact ties have a stable node-identity tiebreaker.
- When explicitly configured, reranking receives only the bounded fused
  candidate set and returns at most the caller's requested result limit.
- A missing, unreachable, malformed, incomplete, duplicate, or non-finite
  reranker response never makes code search unavailable. SCS returns the fused
  ordering and reports a non-reranked retrieval mode.
- Source-derived reranking requests may be sent only to the existing validated
  loopback oMLX base URL, without authentication headers.
- The default `reranking_provider` is `"none"`. Setting it to `"omlx"` is the
  only activation mechanism; SCS does not discover or read oMLX state.
- The default configured reranker model is
  `mku64/Qwen3-Reranker-0.6B-mlx-8Bit`.

### MCP response

- The MCP inventory and existing `search_code` default response remain
  compatible.
- `search_code` adds `result_detail`, accepting only `"full"` or `"compact"`;
  omission means `"full"`.
- Compact results retain the stable node ID, node type, symbol name, qualified
  name, source path, line span, signature, bounded content, and semantic
  distance when available. Timestamps, repository IDs, empty metadata, and
  unrelated parser metadata are omitted.
- `graph_context` continues to consume full seed records internally so its
  structural behavior does not depend on the presentation mode.

### Standardized evaluation

- `scripts/evaluate-search.py` consumes a versioned JSON relevance suite and
  queries a running SCS daemon through the public `knowledge.search` route.
- Each case supplies a natural-language query and graded relevant symbol
  identities. Symbol identity is the indexed qualified name, optionally scoped
  by repository-relative file path.
- The evaluator reports per-query and aggregate Recall@k, reciprocal rank,
  nDCG@k, response bytes, and request latency, plus the active graph/provider
  metadata needed to compare runs.
- Repetition supports warmed latency measurement. Machine-readable JSON output
  is stable and suitable for comparing embedding/reranker configurations.
- Metric computation is a pure typed module covered by procedurally generated
  fixtures. Live model availability is never required by `just verify`.

## Risks and recovery

- Reranking adds a second model request and can increase tail latency. Candidate
  counts remain bounded, reranking is opt-in, and the evaluator records latency.
- A reranker can produce plausible but poor ordering. The original candidates
  remain recoverable by disabling `reranking_provider` and evaluation artifacts
  make regressions measurable.
- Reciprocal rank fusion changes default result order. Rollback is a localized
  restoration of the prior semantic-first route; no persisted format changes.
- Compact projection may omit metadata a harness needs. It is opt-in and full
  mode remains the default.
- Qualified names can collide. Evaluation suites can disambiguate with a file
  path; ambiguous unscoped judgments are counted against every exact matching
  identity rather than resolved heuristically.

## Executable checklist

- [x] Add failing tests for configuration defaults, TOML/environment overrides,
      and loopback-only reranker activation.
- [x] Add failing provider tests for exact oMLX requests, ordered responses,
      strict response validation, recovery, and fail-open errors.
- [x] Expand search tests for deterministic semantic/lexical fusion, reranking,
      bounds, stable ties, and degradation.
- [x] Expand service and MCP contract tests for full compatibility and compact
      projection.
- [x] Add failing metric tests for Recall@k, reciprocal rank, nDCG@k, latency,
      payload accounting, and malformed evaluation suites.
- [x] Implement the typed reranking provider and daemon composition.
- [x] Make one hybrid search service authoritative for the public route.
- [x] Implement compact result projection without changing the default shape.
- [x] Implement the versioned evaluation suite reader, runner, JSON report, and
      `just eval-search` entry point.
- [x] Document configuration, compact queries, suite authoring, metrics, and
      result comparison.
- [x] Run targeted tests before each implementation unit.
- [x] Run a live oMLX evaluation when the configured model is reachable.
- [x] Run `just verify` and report every acceptance criterion.

## Direct rollout

Ship fusion and the opt-in compact mode directly. Keep reranking disabled for
all existing and fresh installations. Operators enable it explicitly in
`~/.scs/config.toml`, restart SCS, and compare standardized evaluation reports
before and after activation. No index migration or re-embedding is required.

## Verification

- Unit tests prove metric formulas, candidate fusion, stable ordering, provider
  validation, and fail-open behavior.
- Integration tests prove the public SCSWire and MCP parameter/response
  contracts, including unchanged full-mode fields.
- Performance checks measure response size and warmed request latency without
  making workstation-dependent model timings a CI gate.
- A live local run records whether the selected oMLX reranker loads, its actual
  response schema, quality metrics on a repository suite, and cold/warm latency.
- `just verify` must pass before the final implementation commit.

### Recorded results

- Targeted provider, configuration, fusion, MCP, service-route, and evaluation
  tests passed before their respective implementation commits.
- A live four-file repository was generated under a temporary directory and
  indexed through Qwen3-Embedding-8B via the configured loopback oMLX server.
  Three graded queries achieved Recall@4 1.0, MRR 1.0, and nDCG@4 1.0 both
  before and after reranking.
- The selected Qwen3 reranker successfully handled every live request. Mean
  request latency increased from 62.55 ms without reranking to 206.32 ms with
  reranking; observed p95 increased from 68.45 ms to 213.55 ms. This supports
  the decision to keep reranking explicitly opt-in.
- Compact live responses averaged 1,265 bytes without reranking and 1,332 bytes
  with reranking; the difference is primarily the longer retrieval-mode label.
  Integration coverage separately proves compact responses are smaller than
  full records for identical indexed results.
- `just verify` passed with 273 Python tests, 84.78% branch coverage against
  the 83% floor, strict Basedpyright, Ruff, and 99 Rust tests.
- The existing user catalog exposed an unrelated lifecycle limitation: catalog
  startup exceeded the CLI's 15-second readiness timeout, and a prior daemon
  retained its lock after its socket disappeared. Live feature validation used
  an isolated SCS root so this unrelated state could not distort results. The
  evaluator now continues polling a spawned daemon within its own bounded wait,
  without changing ordinary daemon or MCP startup deadlines.
