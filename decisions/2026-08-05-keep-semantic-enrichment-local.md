# Keep semantic enrichment local

## Context

SCS generated vectors locally with MLX but optionally called OpenAI to summarize
files before embedding their entities. This made vector input depend on remote
model output, added an otherwise unnecessary API-key/configuration boundary,
and left summary-specific filters and native storage machinery in the product.
The optional path also made successful indexing report degraded semantic state
when only remote summarization was unavailable.

## Decision

SCS semantic enrichment is local-only. The indexing pipeline embeds the exact
`ParsedEntity.embed_text()` output and has no file-summarization provider port.
Configuration, daemon composition, ingestion results, MCP filtering, native
scan APIs, bindings, and the summarizable-node partial index no longer expose
that capability.

Schema initialization idempotently drops the retired partial index. It does not
rewrite existing node or edge metadata: old summary keys are inert historical
data, and destructive cleanup requires a separately authorized operation.

The unrelated `QueryOperationSummary` observability type remains because it
aggregates storage metrics and has no relationship to code-file summarization.

## Rejected alternatives

- Keeping remote summarization behind an optional API key preserves the
  dependency and non-deterministic embedding-input boundary the retirement is
  intended to remove.
- Retaining dormant scan/config surfaces would imply supported behavior and
  allow accidental reactivation without a new architectural decision.
- Automatically deleting historical summary metadata would make startup a
  destructive, irreversible data migration without improving active behavior.

## Consequences

- Indexing and semantic search need no remote-model credential.
- Embedding inputs are deterministic functions of parser output.
- Existing databases lose one unused partial index on the next startup; SQLite
  recreates all active indexes idempotently.
- Historical summary metadata may remain visible in raw node metadata until a
  separately requested cleanup, but SCS neither reads nor updates it.
