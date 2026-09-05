# GitHub Releases distribution

SCS ships directly from public GitHub Releases. It is not published to PyPI or
another package registry. A release tag builds the complete Python/Rust product
for Apple Silicon macOS and x86-64 Linux.

## Release contents

Every `vX.Y.Z` release contains:

- one CPython 3.14 wheel for each supported platform;
- a source distribution;
- `scs-X.Y.Z-constraints.txt` with the exact locked runtime solution;
- `scs-installer-X.Y.Z.sh` bound to that release;
- an SPDX JSON software bill of materials;
- `SHA256SUMS` covering all assets;
- GitHub build-provenance attestations.

Published versions and assets are immutable. A correction receives a new patch
version. The tag, Python metadata, Rust workspace, runtime version, wheel, and
installer must agree; `scripts/check-release-version.py` blocks mismatches.

## Supported hosts

| Host | Architecture | Python ABI | Service model |
| --- | --- | --- | --- |
| macOS 11+ | Apple Silicon | CPython 3.14 | lazy shared daemon |
| Linux with glibc 2.28+ | x86-64 | CPython 3.14 | lazy shared daemon |

Intel macOS, Linux ARM, musl Linux, Windows, and offline installation are not
yet supported. macOS artifacts are currently unsigned and not notarized. Users
must verify the checksum; GitHub attestations bind artifacts to the workflow.

## Installation

Download the versioned installer and `SHA256SUMS` from the same release. Verify
before execution:

```bash
VERSION=0.1.7
curl -fsSLO "https://github.com/leonardoventurini/scs/releases/download/v${VERSION}/scs-installer-${VERSION}.sh"
curl -fsSLO "https://github.com/leonardoventurini/scs/releases/download/v${VERSION}/SHA256SUMS"
shasum -a 256 -c SHA256SUMS --ignore-missing
sh "scs-installer-${VERSION}.sh"
```

Linux uses `sha256sum` in place of `shasum -a 256`. The installer:

1. validates the OS, architecture, and prerequisite utilities before mutation;
2. downloads only exact-version assets over TLS;
3. verifies the wheel and constraints against `SHA256SUMS`;
4. uses an existing `uv`, or downloads pinned `uv` 0.12.9 and verifies its
   upstream checksum;
5. stops a running prior daemon without touching data;
6. installs the local wheel under exact constraints with CPython 3.14;
7. verifies the installed runtime version;
8. prints the MCP harness command.

The installer accepts `--version VERSION` for the source-tree copy and
`--check` for a mutation-free prerequisite check. A release installer embeds
its version and requires no argument. It accepts no credentials in argv and
does not use `sudo`.

## Runtime lifecycle

Configure each harness to execute `scs mcp`. The process speaks MCP over stdio,
lazily starts the shared daemon, acquires a connection-owned lease, and routes
tools through SCSWire. Multiple bridges converge on the same daemon generation.
When the final bridge disconnects, the daemon reaches its durable shutdown
boundary, flushes TSG, removes its owned socket and identity, and exits.

There are no launchd/systemd definitions or TCP MCP ports. Diagnostic commands:

```bash
scs status
scs doctor
scs daemon start
scs daemon status
scs daemon restart
scs daemon stop
```

`status` is read-only. Other operational commands start the daemon lazily and
hold a temporary lease while executing.

### Codex

Codex CLI, the IDE extension, and the ChatGPT desktop app share the MCP
configuration stored in `~/.codex/config.toml`. Register the installed SCS
stdio command with:

```bash
codex mcp add scs -- "$HOME/.local/bin/scs" mcp
codex mcp get scs
```

If an earlier `scs` entry points to a local HTTP URL, remove it first with
`codex mcp remove scs`. Restart open Codex clients after changing the entry.
The equivalent manual configuration is:

```toml
[mcp_servers.scs]
command = "/Users/you/.local/bin/scs"
args = ["mcp"]
```

Use the corresponding absolute home path on Linux. Shell variables are not
expanded inside the TOML `command` value.

## Upgrade, rollback, and uninstall

Running a newer versioned installer performs an in-place `uv tool` replacement.
It stops the prior daemon first and preserves `SCS_HOME`, project stores,
configuration, durable jobs, and logs.

Rollback installs an earlier compatible release in the same way. If a future
release changes a persistent schema incompatibly, follow that release's data
rollback note before starting an older binary. The TSG cutover's retained
`*.pre-tsg.backup` files remain an independent recovery path.

Uninstall code with:

```bash
scs daemon stop
uv tool uninstall scs
```

This intentionally preserves user data and logs. There is no automatic purge
command.

## Maintainer release procedure

1. Update the Python, Rust workspace, and runtime versions together.
2. Move relevant `CHANGELOG.md` entries under the release version.
3. Run `scripts/check-release-version.py vX.Y.Z` and `just verify`.
4. Commit and push `main`; require CI success on macOS and Linux.
5. Create and push an annotated `vX.Y.Z` tag reachable from `main`.
6. The release workflow repeats the quality gate, builds and smoke-tests both
   wheels without checkout imports, inspects native linkage, creates source and
   constraints assets, generates the installer/SBOM/checksums, attests them,
   and publishes the stable release.
7. Verify an installation from the release page on disposable hosts.

The workflow uses least-privilege job permissions. Only the final publish job
receives `contents: write`, `id-token: write`, and `attestations: write`. Every
third-party action is pinned to a full commit SHA.

Both SCS and its immutable Git dependency TSG must remain publicly readable so
clean GitHub-hosted runners and source consumers can resolve the release.

## Incident response

Do not replace a published asset. Stop recommending the affected version,
install the last compatible release, restore data only if its release notes
require that step, and publish a corrected patch release. Revoke GitHub or
signing credentials immediately if compromise is suspected.
