# OMLX embeddings

SCS uses the local OMLX OpenAI-compatible embeddings endpoint by default. This
keeps parser-derived source representations on the machine while enabling
semantic retrieval alongside the structural code graph.

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

`SCS_OMLX_BASE_URL` accepts only a loopback `http` URL. This is deliberate:
embedding inputs are derived from repository source, and SCS must not send
them to a remote endpoint through configuration drift.

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
scs service restart
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
