# Lazy MCP daemon and GitHub release

Project: `scs`

Project root: `/Users/leonardo/Repositories/mentagen/scs`

## Problem

SCS currently requires two always-on launchd services: a public HTTP MCP proxy
and a daemon that also hosts an internal HTTP MCP server. This is macOS-only,
duplicates transport layers, reserves TCP ports, and complicates installation.
SCS also lacks executable GitHub release automation and a checkout-independent
installer.

## Evidence

- Every SCS daemon already owns a single cross-process `SCSWire` Unix socket and
  a non-blocking root-scoped process lock.
- The MCP implementation already routes tools through an `SCSWireGateway`.
- The proxy currently adds restart queueing and discovery around two fixed
  localhost ports; it owns no storage behavior.
- The release design requires one mixed Python/Rust wheel, exact constraints,
  checksums, an SBOM, attestations, and lifecycle smoke tests.

## Desired outcome

Each agent harness launches a thin `scs mcp` stdio server. Any number of those
bridges share one lazily spawned daemon through `SCSWire`. The first bridge
starts the daemon safely; the daemon exits after its final bridge disconnects.
No launchd registration or TCP MCP listener remains. A tag-driven GitHub
workflow publishes stable, unsigned macOS and Linux releases, and a verified
installer installs them from the public repository without a checkout or Rust.

## Scope and assumptions

- Initial platforms are macOS Apple Silicon and Linux x86-64, both CPython
  3.14. Additional architectures are explicit later commitments.
- The GitHub repository will be public before an end-user release, so release
  downloads require no token.
- Stable releases are unsigned. Documentation must state that Apple Developer
  ID signing and notarization are not provided yet.
- Unix-domain sockets are the supported local control transport on macOS and
  Linux.
- `SCS_HOME`, project stores, configuration, jobs, and logs survive upgrades
  and uninstall.
- A manual `scs daemon start` performs the same bounded lazy bootstrap; without
  a bridge lease, the daemon exits after its startup grace period.

## Contracts

- `scs mcp` speaks MCP over stdio only and never writes protocol data to
  stdout except through the MCP transport.
- Every bridge acquires an opaque daemon lease after readiness and releases it
  on normal exit. Abruptly terminated bridges are detected through an owned
  connection, so leases cannot remain permanently orphaned.
- The daemon starts at most once under concurrent bridge startup. Contenders
  connect to the winner and never unlink a live or foreign socket.
- When the final bridge disconnects, the daemon stops accepting work, lets its
  current durable indexing operation reach a safe checkpoint, flushes stores,
  removes only its generation-owned artifacts, and exits.
- CLI calls use the same bounded lazy bootstrap and temporary lease behavior.
- The daemon hosts SCSWire only; it does not bind an MCP TCP port.
- Launchd service commands, plists, proxy discovery, and proxy runtime code are
  removed from the installed product.
- The tag, Python version, Rust workspace version, wheel metadata, installer,
  and release asset names agree exactly.
- Release actions are pinned to immutable commit SHAs and receive minimum job
  permissions.
- Every downloaded installer payload is versioned and SHA-256 verified before
  installation. The installer never accepts secrets in argv or deletes data.

## Test strategy and acceptance criteria

1. Unit-test bootstrap locking, readiness polling, process spawning, leases,
   and CLI state/error serialization with procedural temporary roots.
2. Contract-test the exact MCP inventory over stdio with stdout cleanliness.
3. E2E-test two simultaneous MCP bridges sharing one daemon generation and
   final-client shutdown, including abrupt bridge termination.
4. Preserve daemon socket safety, persistence-fault, indexing-equivalence,
   isolation, and performance gates.
5. Validate wheel contents and install it into an isolated environment with
   the checkout excluded from imports.
6. Test the installer against a local release fixture on both supported runner
   families; verify idempotent reinstall and data preservation.
7. Run `just verify`, proxy replacement tests, workflow linting where locally
   available, and GitHub Actions on the pushed branch.

Observable acceptance:

- two independent stdio clients report the same daemon generation;
- stopping one bridge leaves the other functional;
- stopping the last bridge terminates the daemon within a bounded interval;
- no SCS process listens on ports 28463 or 28465;
- `scs daemon status` is cross-platform and non-mutating;
- a release tag yields wheels, source archive, constraints, SBOM,
  `SHA256SUMS`, installer, and GitHub attestations;
- a clean supported host installs and runs `scs mcp` without the repository.

## Risks and mitigations

- Simultaneous first clients could spawn duplicate daemons. Serialize only the
  bootstrap decision and retain the daemon's independent ownership lock.
- A killed bridge could leak a lease. Tie lease ownership to a dedicated live
  SCSWire connection rather than a release-only RPC.
- Final-client shutdown could interrupt indexing. Stop admission first and let
  the runner persist a retryable checkpoint before teardown.
- Harness stderr/stdout handling could corrupt MCP framing. Reserve stdout for
  MCP and send diagnostics to stderr or files.
- Unsigned macOS artifacts show weaker trust UX. Disclose this prominently and
  retain checksums plus GitHub build provenance.
- Public release dependencies could drift. Export exact runtime constraints
  from the committed lock and enforce them during installation.

## Recovery and rollback

The change does not alter SCS persisted schemas. Roll back by installing the
previous wheel, restoring its launchd definitions with the previous CLI, and
starting those services. Release assets and tags are immutable; defects receive
a new patch release. Uninstall removes only installed code and runtime-owned
socket/identity artifacts, never `SCS_HOME` or logs.

## Direct rollout

1. Add daemon lease and shutdown coordination behind SCSWire.
2. Add race-safe lazy bootstrap and cross-platform daemon CLI.
3. Add the stdio MCP bridge and multi-client E2E coverage.
4. Remove HTTP proxy, launchd runtime, and obsolete dependencies/docs.
5. Consolidate mixed wheel packaging and version identity.
6. Add and test the release-bound installer.
7. Add pinned GitHub CI/release workflows, SBOM, checksums, and attestations.
8. Update installation, operation, upgrade, rollback, and trust documentation.
9. Run all local gates, commit each verified unit, push, and validate GitHub CI.

## Executable checklist

- [ ] Implement connection-owned daemon leases and safe final-client shutdown.
- [ ] Implement race-safe lazy daemon bootstrap and lifecycle CLI.
- [ ] Serve MCP over per-harness stdio through SCSWire.
- [ ] Remove launchd and HTTP proxy runtime architecture.
- [ ] Produce one checkout-independent mixed SCS wheel.
- [ ] Implement and test the versioned installer.
- [ ] Implement pinned GitHub CI and release workflows.
- [ ] Update public and operational documentation.
- [ ] Run full verification and record the architecture decision.
- [ ] Push both code and release configuration and validate GitHub CI.
