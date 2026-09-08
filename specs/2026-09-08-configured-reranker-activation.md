# Configure Reranking by Model Presence

Project: `scs`

Project root: `/Users/leonardo/Repositories/mentagen/scs`

Root ID: `ac4266c4`

## Problem

SCS currently defaults `reranking_provider` to `"none"` but also compiles a
specific Qwen reranker name into every installation. This makes the disabled
default ambiguous and couples a general-purpose service release to one
workstation's installed model.

## Evidence

- `SCSSettings` defines both a provider switch and a non-null default model.
- Daemon composition activates oMLX through the provider switch and passes the
  compiled model to `OMLXRerankingProvider`.
- Embeddings already take their endpoint from configuration; the reranker
  already uses the same validated `omlx_base_url` but should likewise require
  an explicitly configured model identity.

## Uncertainty

No unresolved behavioral ambiguity remains. The requested configuration model
is: an absent reranking model disables reranking; a configured model activates
the oMLX reranker. Existing installations that only set
`reranking_provider = "omlx"` will no longer enable reranking.

## Contracts

- `reranking_model` is nullable and defaults to `None`.
- A non-empty configured `reranking_model` is the sole activation mechanism.
- An active reranker uses the existing validated loopback `omlx_base_url` and
  sends the configured model name to `/v1/rerank`.
- `reranking_provider` is removed from the supported settings contract.
- Reranker failures continue to degrade to deterministic fused retrieval.
- Evaluation reports record the nullable reranking model; they do not invent a
  provider setting that operators no longer configure.
- This workstation receives its Qwen reranker setting only in
  `~/.scs/config.toml`; repository defaults and examples remain model-neutral.

## Risks and recovery

- Removing the redundant provider key changes configuration compatibility. An
  old key is ignored under the existing extra-setting policy, and reranking is
  safely disabled until `reranking_model` is configured.
- An empty model string could look configured while being unusable. Validation
  will normalize or reject blank values so activation remains explicit.
- Recovery is configuration-only: remove `reranking_model` to disable
  reranking. No index migration or persisted-data rollback is required.

## Executable checklist

- [ ] Update configuration tests to require a nullable, disabled default.
- [ ] Test TOML and environment activation using only `reranking_model`.
- [ ] Test blank model rejection.
- [ ] Compose the oMLX reranker only when a model is configured.
- [ ] Update evaluation metadata, documentation, and prior decision context.
- [ ] Set the Qwen reranker only in this workstation's owner-local config.
- [ ] Run targeted tests, strict type checking, and `just verify`.
- [ ] Record the resulting configuration decision.

## Direct rollout

Ship the nullable setting directly. The default remains disabled everywhere.
On this workstation, configure:

```toml
reranking_model = "mku64/Qwen3-Reranker-0.6B-mlx-8Bit"
```

The existing `omlx_base_url` supplies the endpoint. Restarting is lazy and does
not require re-indexing.

## Verification

Acceptance requires:

- fresh settings expose no reranking model and create no reranker;
- TOML and environment values select the exact configured model;
- application composition uses `omlx_base_url` for that model;
- search remains fail-open when the configured service is unavailable;
- the local config contains the Qwen model without changing repository state;
- all Python, typing, lint, coverage, and Rust verification gates pass.
