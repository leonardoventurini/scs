# Use local OMLX embeddings

## Goal and scope

Replace the unavailable in-process MLX default with SCS's local OMLX
OpenAI-compatible embedding endpoint. The service will use only loopback HTTP,
the advertised `Qwen3-Embedding-8B-4bit-DWQ` model, and its verified 4,096
dimensions. This rollout changes neither repository source nor the public MCP
inventory. It does not start a full reindex; that remains an explicit durable
background request after the provider is healthy.

## Evidence and uncertainty

- Project: `scs`
- Project root: `/Users/leonardo/Repositories/mentagen/scs`
- `omlx start --timeout 30` started the managed local service at port 10000.
  `GET /v1/models` advertises the embedding model, and `POST /v1/embeddings`
  returns one valid 4,096-element vector.
- The current SCS MLX provider cannot import `mlx_embedding_models`; live SCS
  has zero vectors and therefore serves lexical retrieval only.
- Risk tier: medium. Vectors are durable and must never be interpreted by an
  incompatible model or dimension. Main uncertainty: OMLX response failures
  must leave structural indexing usable without persisting partial vectors.

## Contracts and decisions

- Add an `OpenAICompatibleEmbeddingProvider` using `aiohttp`, with a bounded
  local request timeout and strict OpenAI embeddings-response parsing.
- Accept only loopback `http` base URLs. Parser-owned code representations must
  never be sent to a remote endpoint through an accidental configuration.
- Default configuration is `omlx`, `http://127.0.0.1:10000/v1`,
  `Qwen3-Embedding-8B-4bit-DWQ`, and dimension 4096. Keep the existing MLX
  adapter selectable for compatible local deployments.
- Document/query prefixes remain `search_document:` and `search_query:` so the
  embedding model receives symmetric retrieval intent.
- The provider validates response count, unique contiguous indices, numeric
  components, and configured dimension before returning any vector. Any
  failure becomes a typed unavailable error and causes semantic enrichment to
  degrade without blocking structural ingestion.
- Existing provider metadata remains the vector-compatibility boundary. A
  changed provider/model/dimension quarantines the old USearch sidecar rather
  than mixing representations. No automatic reindex is allowed.

## Risks and recovery

- OMLX unavailable or malformed response → structural ingestion continues with
  a durable degraded reason; detect with provider and pipeline tests; recover
  by restoring OMLX then explicitly reindexing.
- Model/dimension mismatch → stale vectors could pollute search; prevent with
  strict response validation and provider metadata; recover through the
  existing sidecar quarantine and explicit reindex.
- Remote endpoint configuration → source-derived code leaves the machine;
  prevent with loopback-only settings validation; reject startup configuration.
- HTTP timeout stalls indexing → use named timeout; the durable runner retries
  the job and never records successful source hashes after a parsing failure.

## Verification gauntlet

- **Hard gate — API contract:** provider tests cover request payload/prefixes,
  index-order reconstruction, malformed responses, HTTP errors, and dimension
  mismatch.
- **Hard gate — local privacy boundary:** configuration tests reject a nonlocal
  OMLX URL.
- **Hard gate — durable compatibility:** native graph tests prove a changed
  provider identity quarantines old vectors; targeted pipeline coverage proves
  failed enrichment leaves structural ingestion available.
- **Hard gate — live endpoint:** call OMLX's models and embeddings endpoints,
  then restart SCS and confirm stats report the OMLX provider available while
  still reporting semantic search not ready until explicit indexing.
- **Hard gate — integration:** run targeted tests, `just verify`, proxy tests,
  and source-boundary Git checks.

## Execution checklist

- [x] Add loopback-only OMLX settings and provider selection — files:
  `src/scs/config.py`, `src/scs/main.py`; verify: config tests; done when the
  default identity matches the live 4096-dimensional model.
- [x] Implement strict OpenAI-compatible embeddings client — files:
  `src/scs/providers/openai_compatible.py`, provider tests; verify: targeted
  tests; done when malformed or unavailable responses cannot yield vectors.
- [x] Preserve durability and observability contracts — files:
  provider metadata/native tests and service-route tests; verify: targeted
  pipeline/graph tests; done when incompatible vectors quarantine and failed
  enrichment remains structural-only.
- [x] Validate local rollout without eager ingestion — files: managed OMLX and
  SCS services; verify: endpoint probes, `scs service restart`, `scs doctor`,
  project-scoped stats; done when the provider is available and no reindex was
  automatically requested.

## Verification and rollout

Ship provider code, configuration, tests, and this specification in one
commit. Restart SCS only after verification. To roll back, revert the commit,
set `SCS_EMBEDDING_PROVIDER=mlx` if needed, and restart SCS. Do not restore a
quarantined vector sidecar or reindex automatically; vectors are regenerated
only through an explicit indexing request.
