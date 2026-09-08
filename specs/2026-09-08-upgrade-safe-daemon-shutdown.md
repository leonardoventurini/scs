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

## Test strategy and acceptance criteria

1. Extend CLI contract tests first for the explicit cancellation flag and its
   forwarding to the controller.
2. Unit-test controller shutdown with a captured identity after socket loss,
   including success, timeout, and absent-daemon behavior.
3. Integration-test cancellation of queued and running jobs through the daemon
   shutdown route and verify the identity remains until the lock is released.
4. Extend installer tests with a procedural fake `scs`/`uv` environment to prove
   cancellation is requested and installation is skipped on stop failure.
5. Run targeted Python tests, then `just verify`.
6. Build and install the release artifact, then complete a real MCP initialize
   exchange within Codex's configured startup limit.

Observable acceptance criteria:

- the cancellation flag is part of the documented CLI contract;
- an upgrade cannot invoke `uv tool install` after daemon-stop failure;
- queued/running jobs reach `cancelled` without losing committed batch state;
- successful stop implies the captured daemon PID is gone and the writer lock
  is acquirable;
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

- [ ] Add cancellation and full-exit lifecycle tests.
- [ ] Implement `daemon stop --cancel-active`.
- [ ] Keep identity until writer-lock release and wait for exact daemon exit.
- [ ] Make the installer block replacement on unsuccessful shutdown.
- [ ] Update documentation.
- [ ] Run targeted tests and `just verify`.
- [ ] Record the decision and commit the verified unit.
- [ ] Publish, install, and validate the patch release locally.
