# Local SCS with explicitly trusted OMLX hosts

## Problem and evidence

The local SCS executable and Codex registration were absent. The old checkout
predates stdio MCP. Verified release 0.1.8 is now installed. The user requested
local SCS with m3 as its OMLX host, copying m3's existing configuration, then
explicitly requested a direct commit, patch release, and reinstall.
http://m3:10000/v1/models serves the configured Qwen embedding model, but SCS
rejects its non-loopback URL.

## Scope and contracts

Keep loopback-only OMLX access by default. Add an empty-by-default typed
omlx_trusted_hosts setting; accept a non-loopback host only on an exact,
case-insensitive match to that explicit list. Configure only m3 locally.
Retain HTTP scheme and hostname checks, slash normalization, and suppression
of OpenAI credentials. No tunnel or remote SCS deployment is needed.

## Acceptance and test strategy

First extend tests to demonstrate m3 is accepted only with explicit trust,
unlisted hosts remain rejected, and existing loopback behavior works. Cover
TOML loading and credential suppression. Run targeted tests, full just verify,
and release-specific checks before publishing. Verify released installation,
real OMLX embeddings, ten-tool MCP inventory, daemon readiness, and completed
Meteor ingestion with usable graph/search results.

## Risks, recovery, and uncertainty

Source-derived text is sent over HTTP to the explicitly trusted network host.
Default behavior remains local. Automatic approval review rejected unrestricted
remote HTTP access; the scoped opt-in trust list addresses that objection.
Back up existing SCS state before starting the newer daemon. Preserve old local
checkout history with an isolated branch. No source fix changes data formats.
The release installation may use newer existing storage behavior.

Rollback: stop SCS, reinstall verified 0.1.8, restore prior configuration and
state backup, and remove only the added Codex registration. Never delete old
index backups. No unrelated repository files are part of this change.

## Direct rollout checklist

- [x] Observe focused tests fail before implementation: three expected failures.
- [x] Implement scoped host trust and update documentation.
- [x] Complete required tests: 260 Python tests, 84.69% coverage, strict typing, lint, and Rust tests pass. Release version validation follows the version bump.
- [ ] Commit, push, release the patch, and verify build assets.
- [ ] Reinstall the verified release; copy m3 settings with explicit host trust.
- [ ] Register Codex and verify MCP, provider, daemon, and Meteor ingestion.
- [ ] Record the decision and verification results.
