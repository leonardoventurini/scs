---
status: accepted
project: scs
project-root: /Users/leonardo/Repositories/scs
created: 2026-09-23
updated: 2026-09-23
supersedes:
  - decisions/2026-09-22-unify-agent-code-queries.md (classifier runtime only)
superseded-by:
  - decisions/2026-09-23-gate-daemon-readiness-on-classifier.md (lazy worker lifecycle only)
related-specs:
  - specs/2026-09-22-unified-query-orchestration.md
implementation:
  commits: [dd56819]
  pull-request:
---

# Use Native MLX for Query Routing

## Context

The initial local Laya classifier ran through ONNX Runtime on CPU. Its warmed
four-thread inference took about 300–330 ms on the target Apple Silicon Mac,
so every fast-mode request reached its approved 150 ms deadline and fell back
to discovery. Sixteen CPU threads reduced direct inference to about 110–130 ms,
leaving little margin for scheduling and subprocess transport.

The user approved replacing the optional ONNX classifier with native MLX. In
an isolated local check, `laya-mlx==0.2.0` with the FP16 checkpoint at revision
`20aed815fc6acde75733882e7ec0e3f28aeb9717` selected the same playbooks as
ONNX for all seven versioned cases. Seventy warmed decisions had a 14.3 ms p95.
The first GPU decision took about 800 ms, so startup must warm the model before
the worker announces readiness. These measurements establish feasibility on
this workstation, not broad model quality.

## Decision

Replace the optional ONNX Runtime classifier with an SCS-owned `laya-mlx`
subprocess. Pin the package and FP16 model revision, verify every acquired file,
and load only an explicit local bundle. Keep the provider-neutral decision
contract, private bounded pipe protocol, daemon ownership, fixed playbooks,
offline queries, and deterministic fail-open discovery.

The MLX worker performs one synthetic startup inference before its handshake.
The first caller may still time out during lazy startup; warmed fast requests
must meet the unchanged 150 ms classifier deadline. The user-facing
`decision_model = "laya"` configuration and `query_code` contract stay stable.

The lazy worker lifecycle in the paragraph above is superseded by
[Gate Daemon Readiness on the Configured Classifier](2026-09-23-gate-daemon-readiness-on-classifier.md).

## Rejected alternatives

- Raising the fast deadline would change the approved resource contract.
- Using 16 ONNX CPU threads was measured but leaves limited margin and occupies
  substantially more CPU capacity per request.
- ONNX Runtime's Core ML provider keeps the ONNX bundle but requires model
  conversion and a separate parity and latency study on this checkpoint.
- Keeping two classifier runtimes would double the optional supply-chain and
  lifecycle surface without an observed need.

## Consequences

- The optional production package and pinned bundle change; explicit install
  and checksum verification remain required. The former ONNX bundle can be
  deleted after the local MLX handoff is verified.
- Native MLX is supported on Apple Silicon. Other platforms retain
  deterministic discovery unless a future classifier is approved.
- FP16 probabilities may differ slightly from the former FP32 ONNX export.
  Routing parity, calibration, latency, and failure behavior must be measured
  with the versioned suite before promoting `query_code` or retiring tools.
- The dependency-backed IMPACT evidence gap is separate and remains governed by
  the orchestration specification's quality gate.
