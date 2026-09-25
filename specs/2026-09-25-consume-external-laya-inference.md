---
status: validating
project: scs
project-root: /Users/leonardo/Repositories/scs
created: 2026-09-25
updated: 2026-09-25
owner: code-intelligence
decision: decisions/2026-09-25-use-external-laya-inference.md
supersedes:
  - specs/2026-09-23-classifier-daemon-readiness.md (classifier ownership and recovery only)
  - specs/2026-09-22-unified-query-orchestration.md (Laya runtime ownership only)
superseded-by:
implementation:
  commits: [e3238eb]
  pull-request:
---

# Consume External Laya Inference

## Outcome

SCS uses a configured choice API for optional Laya routing. It owns
playbooks and query policy but no Laya runtime, model bundle, or MLX dependency.

## Contract

- `decision_model = "laya"` selects an HTTP decision provider with an explicit
  `decision_base_url`, defaulting to a loopback service on port 10000.
- The URL accepts only loopback or an explicitly trusted exact hostname. No
  credential is sent. SCS sends bounded goal and anchor fields, plus its closed
  playbook choices; it sends no retrieved source or graph records.
- SCS strictly validates model identity, selected choice, confidence, and all
  finite probabilities before using one fixed playbook.
- A configured daemon waits for decision service readiness before publishing its
  socket. If the service is unavailable at startup, startup fails and releases ownership.
  Later outages make the daemon unready, hold new queries during bounded
  recovery, and request daemon shutdown if recovery fails.
- `decision_model = "disabled"` retains deterministic routing. The `query_code`
  schema and persisted index do not change.

## Acceptance criteria

- SCS can start and route a query through a compatible service without importing Laya or loading
  model weights in its process.
- Missing or incompatible decision service prevents configured daemon
  readiness and leaves no orphaned SCS ownership artifacts.
- Malformed decision responses cannot select a playbook; a failed in-flight
  decision follows existing deterministic degradation.
- Disabled routing still works without a decision service or MLX.
- SCS packaging and documentation no longer instruct users to install Laya
  weights or runtime under SCS.

## Verification strategy

Replace process-fixture tests with a generated local HTTP peer and add config,
contract, and lifecycle tests. Run targeted tests and `just verify`. Defer the
real inference endpoint and installed SCS check to the approved rollout.

## Risks and recovery

SCS startup now depends on the configured choice service when Laya is enabled. Revert the SCS
configuration to `decision_model = "disabled"` to restore independent daemon
startup; no index migration is needed.

## Verification results

- **Passed:** `just verify` completed: 390 Python tests, Basedpyright and Ruff
  clean, and 108 Rust workspace tests passed.
- **Passed:** focused configuration, provider, and daemon readiness tests cover
  trusted hosts, exact model identity, complete answers, startup gating,
  outage recovery, and terminal failure. The public SCS documentation and
  diagnostics contain no private serving-product name.
- **Inferred:** SCS carries no Laya runtime dependency or bundle installer and
  constructs only an HTTP client when routing is configured; this is verified
  by source inspection and package lock changes, not a live installed query.
- **Skipped:** an end-to-end query against a warmed external Laya endpoint and
  installed SCS binary await the local service rollout.
