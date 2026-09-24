# Explicit trust for remote OMLX hosts

Status: superseded

The exact-host trust principle remains, but the OMLX-specific setting names
were replaced by the [OpenAI-compatible transport decision](2026-09-22-use-openai-compatible-local-inference.md).

## Context

The user runs SCS locally and OMLX on a separately reachable machine, m3.
SCS 0.1.8 rejects this topology at settings validation despite provider HTTP
compatibility. The user requested the fix be committed, released, and installed.

## Decision

Add omlx_trusted_hosts as a typed list, empty by default. Remote OMLX URLs
require an exact case-insensitive hostname match. Loopback hosts retain their
existing behavior. Locally select only m3 and copy its provider/model/dimension.
Continue suppressing OpenAI credentials for OMLX requests. Publish as a patch
release and use the installed local stdio bridge in Codex.

## Alternatives and rationale

Unrestricted remote HTTP acceptance was rejected by automatic approval review:
the user authorized m3, not arbitrary destinations. A loopback SSH tunnel adds
an unnecessary service because m3 is reachable directly. Running SCS on m3
would move source indexing away from the local repository and was explicitly
rejected by the user. Explicit host trust supports this setup without changing
the default network boundary.

## Consequences

Operators opting in trust the named host and its network with source-derived
entity text. Trust is lexical, with no wildcard expansion or automatic host
selection. Host availability remains operationally required. This adds an
optional configuration field, without changing existing defaults, dependencies,
or persisted index formats.
