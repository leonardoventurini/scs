# Secure, Reproducible Release Installer

## Context

The GitHub Releases rollout originally described an installer that accepted an
OpenAI key directly on the command line, installed an exact SCS wheel without
pinning transitive dependencies, and did not distinguish an ordinary upgrade
from a provider-identity migration. The implemented provider contract now
makes provider, model, and dimension changes invalidate semantic vectors, and
OpenAI regeneration may be billable.

## Decision

The release installer will never accept an API key value in its argument list.
Interactive entry uses a hidden prompt; automation uses `OPENAI_API_KEY`, an
existing owner-only `~/.scs/config.toml`, or a caller-protected key file.
Credentials never enter launchd plists or installer logs.

Every release includes a constraints file exported from the committed lockfile.
The installer verifies it with the other release assets and supplies it to
`uv tool install`, making both the SCS artifact and its runtime dependency
solution release-specific.

Ordinary reinstall and upgrade preserve the existing embedding identity. An
explicit provider, model, or dimension change against a populated index must
disclose vector invalidation and potential API cost, then require interactive
confirmation or an explicit unattended acknowledgement. Semantic readiness is
withheld until durable regeneration finishes.

Stable releases use GitHub immutable releases after draft-asset validation.
Published assets are never replaced in place; corrections receive a new
version.

## Rejected alternatives

- A `--openai-api-key VALUE` option exposes secrets through process inspection
  and shell history.
- Installing only an exact first-party wheel still permits dependency drift at
  installation time.
- Automatically migrating existing OMLX installations to the new OpenAI
  default creates surprise external data transfer and usage charges.
- Treating a running daemon as ready during regeneration makes semantic search
  availability misleading.

## Rationale

These controls align installer behavior with the application's credential and
vector-identity boundaries, make clean installations repeatable, and keep
provider migrations explicit and recoverable.

## Consequences

Release automation must export, validate, checksum, and upload an additional
constraints asset. Installer tests need fresh OpenAI, preserved OMLX, refused
unattended migration, accepted rebuild, interruption, and resumption cases.
Users changing provider identity receive a slower but observable migration,
while users performing an ordinary upgrade retain their current provider and
vectors.
