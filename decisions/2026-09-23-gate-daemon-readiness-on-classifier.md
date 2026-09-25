---
status: accepted
project: scs
project-root: /Users/leonardo/Repositories/scs
created: 2026-09-23
updated: 2026-09-25
supersedes:
  - decisions/2026-09-23-use-native-mlx-for-query-routing.md (lazy worker lifecycle only)
superseded-by:
  - decisions/2026-09-25-use-external-laya-inference.md (worker supervision only)
related-specs:
  - specs/2026-09-23-classifier-daemon-readiness.md
implementation:
  commits: [1c54a6f]
  pull-request:
---

# Gate Daemon Readiness on the Configured Classifier

The worker supervision portion is superseded by
[Use External Laya Inference](2026-09-25-use-external-laya-inference.md).

## Context

The MLX worker warms before its own handshake, but the daemon currently
publishes a ready socket before starting that worker. The first MCP query can
therefore exhaust its classifier deadline during startup and return DISCOVER
even when the model is installed and healthy. Removing the dedicated read tools
made that cold fallback more visible.

## Decision

When `decision_model = "laya"`, start and warm the worker before publishing a
ready daemon. If it cannot become ready within the bounded startup deadline,
fail daemon startup and clean up all owned resources. When the worker exits
later, restart it automatically, report the daemon unready during recovery,
and hold new query routing until the worker is ready. If recovery fails, shut
down the daemon rather than serving indefinitely without its configured
classifier. Disabled-model installations keep deterministic discovery.

## Rejected alternatives

- A larger per-query classifier deadline still makes the first user request
  responsible for worker startup.
- A background warmup after daemon readiness leaves the same race.
- Continuing DISCOVER after a configured worker cannot recover violates the
  selected daemon-readiness contract.

## Consequences

- Configured daemon startup now includes offline model loading and one GPU
  warmup. MCP bridge bootstrap must allow that bounded startup time.
- An invalid or absent configured bundle prevents that daemon generation from
  becoming ready. This supersedes the former absent-bundle fail-open behavior
  for configured Laya only; disabled-model startup remains available.
- Recovery pauses new code queries and can extend their end-to-end latency.
  A failed recovery closes the daemon cleanly so a later launch can retry.
- The MLX package, model bundle, private protocol, MCP schema, repository
  source, and persisted index format do not change.
