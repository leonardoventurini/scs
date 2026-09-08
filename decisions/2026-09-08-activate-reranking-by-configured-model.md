# Activate Reranking by Configured Model

## Context

The initial oMLX integration used two settings: a `reranking_provider` switch
and a compiled default Qwen model. Although the switch defaulted to `"none"`,
shipping a workstation-specific model identity made the general default
ambiguous and required two fields to express one choice.

SCS already owns a validated loopback `omlx_base_url`. The operator's model
selection is sufficient to express both intent and identity without discovery
or another provider selector.

## Decision

- Make `reranking_model` nullable and default it to `None`.
- Enable the oMLX reranker only when `reranking_model` is non-null and nonblank.
- Use the existing configured `omlx_base_url` to form the `/v1/rerank` endpoint.
- Remove `reranking_provider` and the compiled Qwen reranker constant from the
  supported settings contract.
- Record nullable model identity directly in evaluation reports.
- Configure `mku64/Qwen3-Reranker-0.6B-mlx-8Bit` only in this workstation's
  owner-local `~/.scs/config.toml`.

## Rejected alternatives

- Keeping the provider switch was rejected because SCS currently supports one
  reranking transport and model presence already expresses activation.
- Retaining the Qwen model as a disabled default was rejected because it still
  couples new installations and documentation to one local model catalog.
- Discovering an oMLX model was rejected because fresh SCS installations must
  not inspect external product state or activate features implicitly.
- Giving reranking a second base URL was rejected because the established
  loopback oMLX boundary already supplies the correct endpoint and validation.

## Rationale

One optional value makes the disabled state explicit and keeps shipped defaults
portable. It also matches operator intent directly: choosing a model activates
that model at the configured oMLX service. The existing fail-open search path
continues to protect availability.

## Consequences

- Fresh installations do not select or invoke any reranker.
- Existing configurations with both old fields continue to activate because
  `reranking_model` remains recognized and unknown settings are ignored.
- A configuration containing only the old provider switch now leaves
  reranking disabled and must add `reranking_model`.
- Removing `reranking_model` is the complete rollback and disable operation.
- No persisted index format, production dependency, security boundary, MCP
  tool inventory, or search response contract changes.
