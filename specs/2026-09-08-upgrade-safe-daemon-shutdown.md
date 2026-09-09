# Upgrade-safe daemon shutdown

Project: `scs`

Project root: `/Users/leonardo/Repositories/mentagen/scs`

## Problem

The release installer asks the currently installed CLI to stop SCS before
replacing the `uv` tool, but it ignores stop failures. The controller currently
declares shutdown complete as soon as the SCSWire socket disappears. During
shutdown, the daemon removes that socket and its identity before active durable
indexing has drained and before the root-scoped writer lock is released.

An upgrade can therefore replace the executable while the previous daemon still
owns `SCS_HOME/.daemon.lock`. A later MCP bridge cannot reach the old daemon and
cannot start a new one, so it exits before replying to MCP `initialize`.

## Evidence

- The affected workstation had a live `scs.cli serve` process holding
  `~/.scs/.daemon.lock` with no `scs.sock` or daemon identity.
- The daemon log showed every replacement process failing with `SCS daemon is
  already running for this storage root`.
- A process sample showed the inaccessible daemon completing native embedding
  batches while retaining the writer lock.
- Codex allows 10 seconds for MCP startup by default, so an MCP bridge cannot
  conceal an unbounded lifecycle transition.
- `SCSDaemon.stop()` removes the socket and identity before waiting for the job
  runner and before releasing the process lock.
- `DaemonController.stop()` polls only socket-backed health, and the installer
  suppresses and ignores its result.
- A live v0.1.12 upgrade reproduced a second handoff edge: the daemon exited but
  remained as zombie PID 966 until its MCP bridge reaped it. `kill -0` continued
  to succeed, so the installer waited even though no process could retain the
  writer lock and `scs daemon status` correctly reported no daemon.

## Desired outcome

An upgrade explicitly cancels active indexing, waits until the old daemon has
fully exited and released its writer lock, and only then replaces installed
code. A successful daemon stop must never mean only that the control socket has
disappeared.

## Scope, assumptions, and constraints

- Add an explicit upgrade-oriented cancellation mode to `scs daemon stop`.
- Ordinary final-client shutdown continues to let durable indexing reach its
  safe boundary.
- Cancellation is cooperative: queued work is cancelled immediately and
  running work stops at the next existing durable batch boundary.
- The installer must abort before `uv tool install` if the old daemon cannot be
  stopped safely.
- Persisted index and job formats do not change.
- Process coordination remains same-UID and root-scoped; SCS must not terminate
  an unverified process.
- The affected workstation may force-stop only the already verified wedged SCS
  PID as one-time recovery. Durable work remains retryable.

## Contracts

- `scs daemon stop --cancel-active` requests cancellation for every queued,
  retrying, running, or already-cancelling ingestion job before requesting
  shutdown.
- The daemon keeps its generation identity available until durable teardown is
  complete and removes it only as the writer lock is released.
- `DaemonController.stop()` captures the daemon PID before shutdown and waits
  for that exact same-UID process to exit, bounded by the daemon stop timeout.
- A PID reuse or unverifiable identity is treated as a failed stop, never as
  authority to signal another process.
- The installer invokes the explicit cancellation mode, surfaces failures, and
  does not replace SCS until shutdown succeeds.
- With no daemon present, stop remains idempotent and installation proceeds.
- For legacy PID polling, a zombie is treated as exited while every other
  observable process state remains blocking.

## Test strategy and acceptance criteria

1. Extend CLI contract tests first for the explicit cancellation flag and its
   forwarding to the controller.
2. Unit-test controller shutdown with a captured identity after socket loss,
   including success, timeout, and absent-daemon behavior.
3. Integration-test cancellation of queued and running jobs through the daemon
   shutdown route and verify the identity remains until the lock is released.
4. Extend installer tests with a procedural fake `scs`/`uv` environment to prove
   cancellation is requested and installation is skipped on stop failure.
5. Procedurally create a live process and its unreaped zombie child, then prove
   the installer distinguishes the two states.
6. Run targeted Python tests, then `just verify`.
7. Build and install the release artifact, then complete a real MCP initialize
   exchange within Codex's configured startup limit.

Observable acceptance criteria:

- the cancellation flag is part of the documented CLI contract;
- an upgrade cannot invoke `uv tool install` after daemon-stop failure;
- queued/running jobs reach `cancelled` without losing committed batch state;
- successful stop implies the captured daemon PID is gone and the writer lock
  is acquirable;
- an unreaped zombie daemon does not delay or fail installation;
- local `scs status` reports no hidden lock holder after stop;
- the installed bridge replies successfully to MCP `initialize`.

## Risks and mitigations

- Cancelling running native work mid-call could corrupt state. Use the existing
  durable batch boundaries and cancellation status rather than task killing.
- Waiting on a PID alone can mistake PID reuse for the original daemon. Bind the
  wait to the generation identity and same-UID process observations; never send
  a signal from the controller's polling path.
- Removing identity too late could leave stale metadata after a crash. Identity
  ownership cleanup remains generation-safe, and controller status still probes
  the socket for readiness.
- Installer failure after cancellation interrupts user-requested indexing. This
  is an explicit upgrade policy; the failure is surfaced and the installed code
  remains unchanged.

## Recovery and rollback

No data migration is involved. Roll back by reinstalling the preceding release;
cancelled jobs can be resubmitted with `scs index` or `scs reindex`. If shutdown
coordination fails, the installer exits before replacement and reports that the
existing SCS installation is unchanged.

## Direct rollout

1. Add failing CLI, controller, service, and installer tests.
2. Implement cooperative cancel-all shutdown and full-process waiting.
3. Reorder identity/lock teardown and harden the installer gate.
4. Update operational and release documentation.
5. Run full verification and record the lifecycle decision.
6. Publish a patch release, install it locally, and execute the consumer-level
   MCP startup smoke test.

## Executable checklist

- [x] Add cancellation and full-exit lifecycle tests.
- [x] Implement `daemon stop --cancel-active`.
- [x] Keep identity until writer-lock release and wait for exact daemon exit.
- [x] Make the installer block replacement on unsuccessful shutdown.
- [x] Treat an unreaped zombie daemon as exited without weakening live-process
  blocking.
- [x] Update documentation.
- [x] Run targeted tests and `just verify`.
- [x] Record the decision and commit the verified unit.
- [x] Publish, install, and validate the patch release locally.

## Verification evidence

- `just verify` passed for v0.1.13 with 299 Python tests, 85.99% coverage,
  strict Basedpyright, Ruff, the native build, and 99 Rust tests.
- CI run `34292343781` passed on Ubuntu and macOS, including the supply-chain
  audit. Release run `34292353799` built, smoke-tested, attested, and published
  all v0.1.13 artifacts.
- The checksum-verified public installer upgraded the active v0.1.11 daemon
  through the reproduced live MCP bridge handoff and preserved the `0600`
  machine-local oMLX configuration.
- The installed v0.1.13 bridge initialized in 1.25 seconds. `find_references`
  returned both available and typed-unavailable results without MCP errors, and
  the configured Qwen reranker produced `semantic_reranked` search results.
