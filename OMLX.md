# Former OMLX configuration

The OMLX-specific SCS provider and `SCS_OMLX_*` settings were retired. They are
not valid configuration for the current release. SCS now accepts
`openai_compatible` for a local or explicitly trusted OpenAI-compatible
embedding server, regardless of which product serves the model.

Use the current [embedding configuration](docs/configuration.md) for settings
and [indexing guide](docs/indexing.md) for reindexing. This file remains as a
redirect for older links; its former setup commands and USearch recovery advice
do not apply to the current storage engine.

The naming change is recorded in the
[local inference decision](decisions/2026-09-22-use-openai-compatible-local-inference.md).
