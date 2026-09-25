---
status: superseded
project: scs
project-root: /Users/leonardo/Repositories/scs
created: 2026-09-23
updated: 2026-09-25
owner: code-intelligence
decision: decisions/2026-09-23-gate-daemon-readiness-on-classifier.md
supersedes:
  - specs/2026-09-22-unified-query-orchestration.md (Laya startup and recovery only)
superseded-by:
  - specs/2026-09-25-consume-external-laya-inference.md (classifier ownership and recovery only)
implementation:
  commits: [1c54a6f]
  pull-request:
---

# Classifier-Gated Daemon Readiness

The local-worker contract is superseded by
[Consume External Laya Inference](2026-09-25-consume-external-laya-inference.md).

## Outcome

A daemon configured with Laya becomes ready only after its private MLX worker
has loaded, warmed, and passed the verified handshake. The worker remains
supervised while the daemon runs; new queries wait through automatic recovery.

## Problem and evidence

The current daemon starts its socket before `LayaDecisionProvider` starts its
worker. The worker warms internally, but the first `query_code` request reaches
the 150 or 500 ms classifier deadline while worker startup is still underway.
An actual first balanced MCP request returned `classifier_timeout` and
DISCOVER; its immediate retry selected REFERENCES and returned four importing
test files. The original orchestration spec and MLX decision explicitly allowed
lazy startup; this focused successor replaces only that lifecycle contract.

## Scope and contracts

```text
configured daemon startup -> worker load + warm + handshake -> ready socket
worker exits -> ready=false -> automatic restart -> ready=true
                                      |
                                      +-> failed recovery -> daemon shutdown
```

- The daemon owns exactly one private worker generation at a time. Startup and
  recovery share one serialized spawn path and bounded handshake deadline.
- With configured Laya, `system.health.ready` is true only while a verified
  worker is ready. Socket identity and writer lock are not published as ready
  before the first handshake.
- An initial warmup failure aborts startup and releases socket, identity,
  writer lock, worker, jobs, and watchers. Recovery failure requests clean
  daemon shutdown.
- Queries arriving during recovery wait before their per-mode inference timer
  starts. A worker failure during an already running inference remains a typed
  request failure; the next request waits for recovery.
- `decision_model = "disabled"` retains the existing deterministic path and
  does not start a worker. No model download occurs during daemon startup.
- MCP and SCSWire schemas, repository source, persisted index, and model bundle
  format remain unchanged. Only the meaning of `system.health.ready` during
  worker recovery changes.

## Acceptance criteria

- A configured daemon cannot report ready or accept a query before a warmed,
  verified worker exists; its first query can route through Laya without a
  startup-induced classifier timeout.
- Missing or invalid configured model material fails daemon startup, leaving
  no live daemon socket, identity, worker, or writer lock.
- A worker exit triggers recovery without a new query. During recovery health
  is unready and a new query waits; after recovery it receives one classifier
  decision within the normal per-mode inference deadline.
- A failed recovery leads to clean daemon shutdown. Another daemon generation
  can subsequently start through the supported bootstrap path.
- Disabled-model startup and query behavior remain functional. Worker shutdown
  does not interrupt Cortex or mutate repository source or indexed data.

## Test and verification strategy

- Use procedurally generated fake protocol workers to prove startup blocking,
  startup failure cleanup, automatic restart, and query wait behavior without
  MLX in CI.
- Run focused provider, daemon, controller, and query tests before `just verify`.
- On the target workstation, restart only SCS through its cooperative command,
  verify MCP readiness and the first Laya-routed query, then kill only the
  private classifier worker to verify automatic recovery and query gating.
- Rerun the versioned fast, balanced, and thorough evaluation if the changed
  lifecycle affects measured query behavior. Record every executed check and
  any skipped hardware check below.

## Risks and recovery

Startup becomes slower and fails when configured model material is unusable.
The bootstrap controller must wait long enough for the bounded handshake and
must not spawn a second daemon while a live generation is recovering. Revert
this lifecycle change to restore lazy startup; no data migration is needed.

## Verification results

- Focused provider, controller, orchestration, and daemon tests passed: 25
  tests. The new cases cover delayed handshake, startup cleanup, automatic
  recovery, terminal recovery failure, repeated exits, query gating, health
  changes, and controller handling of a failed child.
- `just verify` passed on 2026-09-23: Basedpyright and Ruff clean, 395 Python
  tests passed, and 108 Rust tests passed. The temporary-home test fixtures
  explicitly disable Laya so they remain independent of the operator's model
  setting; classifier lifecycle tests explicitly enable it.
- A live daemon started from this checkout reported ready with its worker warm.
  Its first balanced REFERENCES query used Laya without a degraded reason and
  returned complete evidence. After killing only its private worker, health
  reported unready, then a query waited for automatic recovery and returned a
  complete Laya REFERENCES result without degradation; health returned ready.
- The daemon was shut down through `system.shutdown` after the live recovery
  check. No Cortex operation, repository source change, index migration, or
  model download was performed.
