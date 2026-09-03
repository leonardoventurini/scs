# Default to Configurable OpenAI Embeddings

## Context

SCS previously selected a loopback OMLX endpoint by default and treated remote
AI use as outside its product direction. Distribution requires a usable
default for people who have not separately installed a local model server,
while existing local users must retain an explicit zero-credential path.

## Decision

OpenAI is the default embedding provider for an installation without explicit
configuration. SCS reads persistent settings from `~/.scs/config.toml`; the
standard `OPENAI_API_KEY` environment variable has precedence over a key in
that file. A file containing a key must have owner-only permissions.

OMLX remains an explicit provider using a loopback-only HTTP endpoint. OMLX
mode ignores OpenAI credentials and emits no authorization header. In-process
MLX remains supported. Provider, model, and dimension continue to form the
durable vector identity, so changing any component quarantines incompatible
vectors and allows indexing to regenerate them from preserved structural data.

The existing development installation is pinned to OMLX in its local,
untracked `~/.scs/config.toml` before the application default changes.

## Rejected alternatives

- Keeping OMLX as the universal default would require an independently
  installed service before semantic search works on a fresh installation.
- Copying `OPENAI_API_KEY` into launchd service definitions would persist a
  secret in an operational artifact with broader accidental-disclosure risk.
- Reusing vectors after a provider identity change would make similarity
  results mathematically invalid.
- Sending an available OpenAI key to a configured OMLX endpoint would violate
  the local provider's credential-free security boundary.

## Rationale

The selected design gives fresh installations a conventional hosted path,
keeps local inference first-class, uses OpenAI's standard environment variable,
and makes migrations deterministic through the already-established vector
metadata boundary.

## Consequences

Fresh users must configure an API key before semantic enrichment becomes
available and may incur OpenAI usage charges. Background launchd users should
store the key in the protected TOML file because interactive shell variables
are not reliably inherited. Switching provider identity triggers a semantic
rebuild, while structural indexes and quarantined prior vectors are retained.
