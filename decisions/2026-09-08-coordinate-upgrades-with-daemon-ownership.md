# Coordinate upgrades with daemon ownership

Project: `scs`

Project root: `/Users/leonardo/Repositories/mentagen/scs`

Date: 2026-09-08

## Context

The release installer previously treated disappearance of the SCSWire socket as
proof that the daemon had stopped. In reality, orderly shutdown removed both the
socket and daemon identity before the durable job runner finished and before the
root writer lock was released. Replacing the installed tool in that interval
left later MCP bridges unable to contact the old process or start a new one.

The failure was amplified by two startup behaviors. Recovered jobs opened large
native vector indexes on the control-plane event loop, delaying readiness, and
the unattached startup grace closed the socket while recovered work was still
active. Codex's documented default MCP startup limit is 10 seconds.

## Decision

Upgrade shutdown is an explicit lifecycle operation:

- `scs daemon stop --cancel-active` atomically cancels queued jobs and marks
  running work for cooperative cancellation;
- pipelines observe cancellation between durable operations, never inside a
  native mutation or provider call;
- the controller reports successful shutdown only after it can acquire and
  release the root writer lock;
- daemon identity remains published until teardown releases that lock;
- the release installer invokes cancellation when supported, waits for the
  captured legacy daemon PID when upgrading older releases, and aborts before
  replacement on failure.

Legacy PID polling combines signal visibility with process state. An unreaped
zombie has already exited and cannot own the writer lock, so the installer
treats `ps` state `Z` as complete. Every other observable state remains live and
subject to the bounded wait. This does not grant authority to signal a process
or weaken the generation and same-UID checks used by the controller.

Control-plane availability is independent of graph workload. Project graphs are
not eagerly opened while restoring watchers, pipeline construction runs off the
event loop, and active durable work defers both attached and unattached idle
shutdown. Deleted file sets use the existing native bulk-delete primitive so one
durable batch causes one vector-accelerator rebuild.

## Rejected alternatives

- Continue using socket disappearance as the stop boundary. This recreates a
  live-lock/no-socket state and violates the single-writer handoff.
- Force-kill active indexing during every upgrade. Native operations are not
  safely interruptible at arbitrary instructions; forced termination remains a
  one-time, verified recovery action rather than product behavior.
- Merely increase Codex's MCP startup timeout. It would hide event-loop blocking
  and would not repair the inaccessible live daemon.
- Cancel asyncio tasks around native calls. The native work can continue after
  task cancellation, making ownership and completion ambiguous.
- Open every registered graph before publishing readiness. The daemon needs only
  catalog records to restore watchers; graph handles belong to demand-driven
  indexing and search paths.

## Consequences

- Upgrades can take as long as the current non-interruptible durable operation,
  bounded to five minutes before the installer leaves the existing installation
  unchanged.
- Releases older than cooperative cancellation drain their current operation on
  the first upgrade; later releases cancel at a durable boundary.
- Explicit upgrade cancellation can leave a project semantically stale. Users
  can resubmit indexing, and no successful ingestion hash is recorded for an
  unacknowledged batch.
- A live daemon with an unreachable socket and no verifiable generation is
  surfaced as an error; SCS never signals an unverified process.
- MCP initialization remains responsive while recovered indexing opens or
  rebuilds a large graph in a worker thread.
- An MCP bridge that has not yet reaped its daemon child no longer stalls an
  otherwise complete upgrade handoff.
- Persisted schemas and the default fresh-install configuration are unchanged.
