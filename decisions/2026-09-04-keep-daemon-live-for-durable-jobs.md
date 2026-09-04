# Keep the Daemon Live for Durable Jobs

Project: `scs`

Project root: `/Users/leonardo/Repositories/mentagen/scs`

## Context

SCS queues indexing as durable background work. A one-shot CLI client can
disconnect immediately after submission. The original final-client shutdown
path then removed the control socket and service identity while the job runner
still held the storage lock, making progress temporarily unobservable and
causing new clients to launch a losing daemon contender.

## Decision

The daemon treats queued or running durable jobs as an internal lease. After
the final external client disconnects, shutdown is deferred and periodically
re-evaluated until no durable job is active. The control socket and service
identity remain available throughout that interval.

## Rejected alternatives

- Make every submitting CLI or MCP client wait for job completion. This would
  defeat the durable background-job contract and make harness lifetimes own
  indexing work.
- Remove the socket but leave the worker alive. This preserves work but makes
  it unobservable and invites competing bootstrap attempts.
- Run a permanent platform service. This reintroduces launchd/systemd setup
  that the lazy shared-daemon architecture intentionally avoids.

## Rationale

Durability requires the service that owns a job to remain both alive and
observable until a terminal state. Treating active work as a lease preserves
lazy startup and idle shutdown while keeping one authoritative writer and one
stable control endpoint.

## Consequences

The daemon may outlive all external clients while indexing is active, then
shuts down normally once the queue is idle. Status and newly spawned MCP
bridges can reconnect throughout a long ingestion. Queue-state detection is
now part of lifecycle correctness and is covered by unit and integration
regressions.
