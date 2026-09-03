# Configurable Embedding Providers

## Problem

SCS currently defaults to a local OMLX endpoint and cannot authenticate to the
OpenAI embeddings API. Users need a persistent configuration file at
`~/.scs/config.toml`, with OpenAI as the default for unconfigured installations
and an explicit OMLX mode that never requires or transmits an API key.

## Evidence and uncertainty

- `SCSSettings` currently reads only `SCS_` environment variables.
- `OpenAICompatibleEmbeddingProvider` implements the common `/embeddings`
  request shape but has no bearer-token support and identifies every endpoint
  as OMLX.
- `NativeGraph` already quarantines vectors when provider, model, or dimension
  changes; the ingestion pipeline already repairs nodes with missing vectors.
- Existing installations must opt into their current OMLX identity explicitly
  before adopting the new application default.

## Contract

- The persistent user configuration path is `~/.scs/config.toml`.
- Explicit constructor values override environment values; environment values
  override TOML values; TOML values override application defaults.
- The standard `OPENAI_API_KEY` environment variable overrides
  `openai_api_key` in TOML.
- The unconfigured default is OpenAI at `https://api.openai.com/v1`, using
  `text-embedding-3-small` with 1,536 dimensions.
- `embedding_provider = "omlx"` uses only the configured loopback OMLX URL,
  ignores every OpenAI credential, and sends no authorization header.
- `embedding_provider = "mlx"` remains available.
- OpenAI requests use bearer authentication. Secrets never enter provider
  metadata, vector metadata, logs, errors, or launchd service definitions.
- Provider, model, or dimension changes quarantine incompatible vector data.
  The next indexing pass regenerates embeddings without discarding structural
  graph data.

## Risks and recovery

- Switching to OpenAI can incur API charges. Existing local installations are
  protected by writing an explicit OMLX configuration before deploying this
  default change.
- A missing OpenAI key leaves semantic enrichment unavailable with a typed,
  non-secret reason; structural indexing remains usable.
- Rollback consists of restoring `embedding_provider = "omlx"` and the OMLX
  model/dimension. Quarantined vector sidecars remain available for manual
  recovery and are never silently overwritten.

## Executable checklist

- [x] Add failing tests for defaults, TOML loading, precedence, and OMLX key
      suppression.
- [x] Add failing provider tests for authenticated OpenAI and unauthenticated
      OMLX HTTP requests.
- [x] Extend provider-identity tests across provider, model, and dimension.
- [x] Implement typed TOML/environment configuration and provider construction.
- [x] Preserve this workstation's current OMLX selection in
      `~/.scs/config.toml` with owner-only permissions.
- [x] Update user-facing configuration documentation.
- [x] Run targeted tests, strict type checking, and `just verify`.

## Direct rollout

The change ships directly. Before restarting the current daemon, create its
explicit OMLX configuration. Fresh installations without configuration use
OpenAI and must supply `OPENAI_API_KEY` in the daemon process environment or an
`openai_api_key` value in the owner-protected TOML file.

## Verification

Automated tests must observe configuration precedence, exact HTTP headers,
provider metadata identity, vector quarantine for every identity component,
and missing-vector regeneration. Full repository verification must pass before
commit.
