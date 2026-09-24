# Embedding and reranking configuration

Persistent configuration lives at `~/.scs/config.toml`. Explicit Python
settings take precedence over environment variables, environment variables
take precedence over TOML, and TOML takes precedence over defaults. The
standard `OPENAI_API_KEY` environment variable overrides `openai_api_key` in
the file. Storing the key in the owner-only configuration file makes it
available to lazily spawned daemon processes without placing it in MCP config.

## OpenAI embeddings

The default provider settings are below. Supply your own API key:

```toml
embedding_provider = "openai"
embedding_model = "text-embedding-3-large"
embedding_dimension = 3072
openai_base_url = "https://api.openai.com/v1"
openai_api_key = "replace-with-your-key"
```

The OpenAI provider sends source-derived entity text to the configured API to
generate the semantic index. Keep the config file owner-readable only:
`chmod 600 ~/.scs/config.toml`.

## Local OpenAI-compatible embeddings

To use a local OpenAI-compatible server without an API key:

```toml
embedding_provider = "openai_compatible"
embedding_model = "Qwen3-Embedding-8B-4bit-DWQ"
embedding_dimension = 4096
openai_compatible_base_url = "http://127.0.0.1:10001/v1"
```

The compatible provider defaults to loopback HTTP URLs. To use a server on an
explicitly trusted network host, configure both its URL and exact hostname:

```toml
openai_compatible_base_url = "http://m3:10001/v1"
openai_compatible_trusted_hosts = ["m3"]
```

The trust list is empty by default and matches hostnames case-insensitively;
it does not match subdomains or wildcards. Only opt in to a host and network
that you trust with source-derived embedding text. SCS continues to run locally.
The environment equivalent is
`SCS_OPENAI_COMPATIBLE_TRUSTED_HOSTS='["m3"]'`. In compatible-provider mode,
SCS ignores OpenAI credentials and sends no authorization header. Changing the
provider, model, or dimension quarantines incompatible vectors; the next
indexing pass regenerates embeddings while preserving the structural graph.

## In-process MLX embeddings

On Apple Silicon, SCS can load an embedding model in its own process when the
`mlx_embedding_models` package and the selected model are installed separately:

```toml
embedding_provider = "mlx"
embedding_model = "Qwen3-Embedding-8B-4bit-DWQ"
embedding_dimension = 4096
```

This provider is separate from the optional Laya query classifier. Neither
local provider requires an OpenAI API key.

## Optional query classifier

Laya is disabled by default. On Apple Silicon, after installing its pinned
bundle as described in the [README](../README.md#optional-laya-routing), set
`decision_model = "laya"` in `~/.scs/config.toml`. The optional
`decision_model_path` can select another absolute bundle path; SCS verifies
its files before loading it. The release installer does not include Laya.

## Reranking

Search always fuses bounded semantic and lexical candidates. To rerank that
candidate set through the same local reranking endpoint, opt in explicitly:

```toml
reranking_model = "your-installed-reranker"
```

An absent `reranking_model` disables reranking. A configured model uses the
same validated `openai_compatible_base_url` as local embeddings and sends no
credentials. An unavailable or malformed reranker degrades to deterministic
fused retrieval without making search unavailable.
