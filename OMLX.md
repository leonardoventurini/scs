# OMLX embeddings

SCS supports OMLX as an explicitly selected OpenAI-compatible embedding
provider. Its default endpoint is local; a trusted remote host can be selected
while SCS continues to index repositories on this machine.

## Required local service

Start OMLX before starting or restarting SCS:

```sh
omlx start --timeout 30
curl --fail-with-body http://127.0.0.1:10000/v1/models
```

The default SCS configuration expects the advertised
`Qwen3-Embedding-8B-4bit-DWQ` model at `http://127.0.0.1:10000/v1`. Its
embedding response was verified at 4,096 dimensions.

## Configuration

SCS reads these environment variables when it starts:

```sh
export SCS_EMBEDDING_PROVIDER=omlx
export SCS_OMLX_BASE_URL=http://127.0.0.1:10000/v1
export SCS_EMBEDDING_MODEL=Qwen3-Embedding-8B-4bit-DWQ
export SCS_EMBEDDING_DIMENSION=4096
```

`SCS_OMLX_BASE_URL` accepts loopback `http` URLs by default. A remote host
requires explicit trust because embedding inputs are derived from repository
source. For a trusted OMLX server named `m3`, use these additional settings in
`~/.scs/config.toml`:

```toml
omlx_base_url = "http://m3:10000/v1"
omlx_trusted_hosts = ["m3"]
```

Set `embedding_provider = "omlx"` in the same file. The trust list is empty by
default, matches exact hostnames case-insensitively, and accepts no wildcard or
subdomain expansion. HTTP sends entity text to the selected host over the
configured network. The environment equivalent is
`SCS_OMLX_TRUSTED_HOSTS='["m3"]'`. OpenAI credentials are never sent in OMLX mode.

The legacy in-process adapter remains available for compatible local setups,
but it needs its own model identity and dimension:

```sh
export SCS_EMBEDDING_PROVIDER=mlx
export SCS_EMBEDDING_MODEL=nomic-ai/nomic-embed-text-v1.5
export SCS_EMBEDDING_DIMENSION=768
```

## Verification and indexing

Restart SCS after OMLX is available:

```sh
scs daemon restart
scs doctor
```

SCS starts structurally ready even if OMLX is unavailable. Its graph stats
report whether semantic search is ready; it becomes ready only after an index
contains embeddings.

SCS never automatically reindexes a repository merely because the embedding
provider changes. Request a background reindex explicitly after verifying the
provider:

```sh
scs reindex /absolute/path/to/project
```

## Vector compatibility and recovery

SCS persists the provider, model, and dimension next to each vector index. If
any of those values change, SCS quarantines the old USearch sidecar rather
than mixing incompatible vectors. Do not restore that sidecar manually; start
the intended provider and explicitly reindex the affected repository.

If OMLX is unreachable, malformed, or returns the wrong dimension, SCS keeps
structural indexing available and records the semantic failure instead of
persisting partial vectors. Restore the local OMLX service and request an
explicit reindex to regenerate semantic data.
