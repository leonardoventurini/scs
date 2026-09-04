# Clean Release Installation Validation

Project: `scs`

Project root: `/Users/leonardo/Repositories/mentagen/scs`

## Problem

The local machine has no installed SCS executable, retains 2.2 GB of prior SCS
state, and configures Codex to use the retired loopback HTTP MCP endpoint. The
published `v0.1.1` artifacts need validation through the same path documented
for end users before the README can present a concise quick start.

## Evidence and uncertainty

- `uv tool list` reports no installed tools and `scs` is absent from `PATH`.
- `~/.scs` contains catalog, jobs, project data, backups, and an owner-readable
  `config.toml` whose contents must remain confidential.
- Codex has one enabled `scs` entry using `http://127.0.0.1:28463/mcp`.
- No SCS process, LaunchAgent, or runtime artifact is currently active.
- The retained provider configuration is expected to support a real reindex;
  provider availability and credentials remain to be validated at runtime.

## Contracts

- Preserve `~/.scs/config.toml` byte-for-byte with owner-only permissions.
- Remove all other existing SCS persistent state, logs, and runtime artifacts.
- Replace the old Codex entry with the installed release's stdio command.
- Install only from the public `v0.1.1` installer after checksum verification.
- Explicitly index `/Users/leonardo/Repositories/mentagen/scs`; a fresh root
  must remain empty before that request.
- Validate version, health, index completion, graph statistics, representative
  search, MCP initialization/tool calls, shared-daemon reuse, and final cleanup.
- Do not print or commit configuration contents or credentials.

## Risks

- Removing old derived state is destructive and requires a complete reindex.
- A provider outage or invalid retained credential can block embedding work.
- Starting MCP from this active Codex session cannot hot-reload the host's own
  tool inventory; validation must use an independent SDK client and the next
  Codex session will consume the new entry.
- The release supports only Apple Silicon macOS and x86-64 glibc Linux.

## Recovery

Move prior data and logs into a timestamped owner-only backup directory outside
all SCS-owned paths. Delete that backup only after installation, indexing, and
MCP validation pass. The retained configuration is also copied separately and
verified by SHA-256 before old state is moved.

If installation or validation fails, uninstall the release, remove the new
Codex entry, restore the prior data/log directories, and restore the original
HTTP entry only if the retired service is still intentionally available.

## Direct rollout

1. Preserve and hash the configuration, then move old state/logs aside.
2. Recreate `~/.scs` with only the original owner-only configuration.
3. Remove the obsolete Codex MCP entry.
4. Download and verify the public release installer and execute it.
5. Add the installed stdio MCP entry to Codex.
6. Validate the empty fresh root, explicitly index this repository, and wait
   for durable completion.
7. Exercise graph/search and independent stdio MCP contracts.
8. Remove the recovery copy after all acceptance checks pass.
9. Rewrite the README opening into a verified streamlined quick start.

## Executable checklist

- [ ] Preserve configuration bytes and mode; isolate all prior state.
- [ ] Verify and install the public `v0.1.1` artifact.
- [ ] Replace Codex HTTP configuration with stdio configuration.
- [ ] Confirm fresh-root emptiness before explicit indexing.
- [ ] Reindex this repository and verify graph/search behavior.
- [ ] Exercise stdio MCP and lazy shared-daemon lifecycle behavior.
- [ ] Remove isolated prior state only after successful validation.
- [ ] Update, verify, commit, and push the streamlined quick start.

## Verification

- Compare configuration SHA-256 and permission mode before and after cleanup.
- Verify release checksums and GitHub provenance, then assert `scs version`.
- Inspect `codex mcp get scs` for stdio transport and the release executable.
- Use CLI status/jobs/stats/search commands according to their actual interface.
- Run the release-installed command through an MCP SDK stdio client and call a
  representative tool.
- Run repository tests affected by any defect fix, followed by `just verify`.
- Confirm final Git/Codex/SCS state and GitHub CI after documentation changes.
