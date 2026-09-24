# Local SCS with explicitly trusted OMLX hosts

Status: shipped

Historical release and installation evidence. OMLX-specific settings and the
ten-tool inventory below are no longer current; use the
[embedding configuration](../docs/configuration.md) and
[MCP reference](../docs/mcp-tools.md).

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
- [x] Commit, push, release the patch, and verify build assets: fix 094cf1a, release d868595, tag v0.1.9.
- [x] Reinstall the verified release; copy m3 settings with explicit host trust.
- [x] Register Codex and verify MCP, provider, daemon, and Meteor ingestion.
- [x] Record the decision and all verification results.

## Installed release verification

- GitHub release run 34004147444 and CI run 34004146435 succeeded on
  d8685950076ef54971cfce9544a25d6530234df4. Both supported platform wheels,
  source, constraints, installer, SBOM, and checksum assets are published.
- The downloaded installer matched SHA256SUMS and the exact tagged source;
  it verified wheel/constraints and installed the standalone 0.1.9 release.
- Installed runtime: /Users/leonardo/.local/bin/scs. Codex has an enabled stdio
  registration using that absolute command with args ["mcp"]. No SSH tunnel,
  LaunchAgent, or remote SCS registration was created.
- Copied leonardo@m3's OMLX settings, changing only the endpoint hostname to
  m3 and adding omlx_trusted_hosts = ["m3"]. Local config mode is 600.
- A real embedding through the installed provider returned 4096 components
  for Qwen3-Embedding-8B-4bit-DWQ. MCP initialization, exact ten-tool inventory,
  and graph stats passed. Both scs status and scs doctor reported version
  0.1.9 ready with available local storage.
- Prior SCS state and Codex configuration are backed up under
  /Users/leonardo/.local/state/scs-backups/20260906T012900Z.
- Meteor's actual checkout is /Users/leonardo/Repositories/meteor/meteor;
  its parent directory is only the workspace container. MCP accepted job
  ingest_8aad29c543d4, which completed all 133 file batches with no errors or
  retries. All 4,193 discovered files were indexed, with zero failed files.
- This conversation's tool catalog predates registration. The independent
  installed MCP client was verified; reopen Codex to refresh its tool catalog.

Review order: src/scs/config.py, tests/unit/test_config.py, README.md/OMLX.md,
then the decision and this rollout evidence. Unrestricted remote-host access
was replaced with explicit trust following automatic approval review.

## Completed ingestion and final handoff

The installed MCP client verified 13,897 nodes, 13,897 embeddings, and 13,897
vectors for Meteor. Graph status is ready and semantic_search_ready is true.
A repository-scoped publication/subscription query returned three results with
retrieval_mode = semantic. The job reported no semantic degradation.

The pipeline retained 9,559 edges and filtered 12,975 candidate edges through
its existing duplicate/unresolved-endpoint policy. This verification establishes
successful indexing and usable semantic retrieval, not exhaustive resolution
of every source relationship.

After the smoke client finished, scs daemon start and scs doctor confirmed
0.1.9 ready; Codex still reports the enabled local stdio registration. Legacy
index.db, WAL, SHM, and provider.json match the backup byte-for-byte. Unrelated
Codex settings are semantically unchanged; its serializer omitted an empty
node_repl args list, which has the same default behavior.

No required implementation checks were skipped. This conversation's original
MCP tool catalog still requires a Codex restart to expose the new registration;
the actual installed command was verified independently over MCP. Rollback
instructions and the backup location above remain applicable. The old divergent
source checkout was preserved, and all repair/release records were pushed to
SCS main from the isolated repair branch.
