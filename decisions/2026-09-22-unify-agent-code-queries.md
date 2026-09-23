---
status: accepted
project: scs
project-root: /Users/leonardo/Repositories/scs
created: 2026-09-22
updated: 2026-09-23
supersedes:
  - decisions/2026-08-05-reduce-mcp-tool-footprint.md
  - decisions/2026-09-14-add-evidence-driven-agent-contracts.md (inventory only)
superseded-by:
  - decisions/2026-09-23-use-native-mlx-for-query-routing.md (classifier runtime only)
related-specs:
  - specs/2026-09-22-unified-query-orchestration.md
implementation:
  commits: [0eadb87, f0187cd]
  pull-request:
---

# Unify Agent Code Queries Behind One Orchestrated Tool

The classifier-runtime choice in this decision was superseded by
[Use Native MLX for Query Routing](2026-09-23-use-native-mlx-for-query-routing.md).
The tool, playbook, and migration decisions remain active.

On 2026-09-23, after the versioned fast, balanced, and thorough evaluation
passed every reported retirement gate, the user approved direct full
replacement without an intermediate deprecation interval. The five-tool MCP
inventory is implemented in the unreleased breaking change. Internal service
routes remain available for orchestration, evaluation, and rollback.

## Context

SCS deliberately reduced its original MCP surface to distinct code-intelligence
operations. Later usage evidence showed that agents still perform long chains
of searches while rarely selecting specialized structural tools. The underlying
operations remain useful; requiring every caller to understand and orchestrate
their taxonomy is the remaining source of friction.

SCS now has bounded search modes, multi-query fusion, graph traversal, file
inspection, indexed references, impact analysis, and truthful degradation. A
closed routing decision can compose those capabilities without granting a model
open-ended control or making SCS generate conclusions.

## Decision

Add one goal-oriented `query_code` MCP tool. A local classifier selects exactly
one typed deterministic playbook from a closed enum, and ordinary SCS code
executes the bounded stages and returns inspectable evidence.

Use Laya through an SCS-owned ONNX Runtime subprocess as the first classifier
candidate. Keep the decision-provider interface model-neutral and fail open to
deterministic discovery. The classifier receives the user goal and validated
anchors, not repository source or retrieved evidence.

Expose `query_code` alongside the seven read-oriented tools during measured
compatibility validation. After the approved specification's quality,
efficiency, installation, and live-evaluation gates pass, remove
`search_code`, `graph_context`, `get_related`, `list_symbols`, `inspect_file`,
`find_references`, and `regression_risk_report` from MCP. Retain their internal
service capabilities for playbook execution and rollback.

Keep repository lifecycle operations separate. The final MCP inventory is
`query_code`, `ingest_project`, `ingest_files`, `get_graph_stats`, and
`delete_repository`.

## Rejected alternatives

- Keeping all read tools indefinitely was rejected because it would preserve
  the selection burden and discovery-schema cost this change targets.
- Putting ingestion, deletion, or readiness behind classifier routing was
  rejected because those operations have different mutation, authorization,
  idempotency, and observation semantics.
- An iterative model-controlled loop was rejected because it increases
  latency, nondeterminism, failure modes, and the model's authority over
  execution.
- Letting Laya select individual tools or arbitrary parameters was rejected in
  favor of versioned playbooks with deterministic bounds.
- Using Laya for candidate reranking was rejected because SCS already has a
  purpose-built reranking boundary and evaluation contract.
- Running Laya in the daemon process was rejected because model/runtime faults
  and dependency state should remain isolated from the graph writer.
- Depending on oMLX support was rejected because the selected local ownership
  model is an explicit SCS subprocess and must not depend on another runtime's
  feature roadmap.
- Removing legacy tools before live equivalence evaluation was rejected because
  hard-coded clients need a measured and documented migration boundary.

## Rationale

One intent-oriented tool lets agents express their goal once while SCS applies
its own structural capabilities consistently. A single non-generative routing
decision is proportionate to the task: it handles fuzzy intent classification,
while deterministic code retains control of source access, graph operations,
budgets, and evidence projection.

Local open weights preserve the source-private product boundary. Subprocess
isolation prevents an optional ML runtime failure from compromising the daemon.
Keeping lifecycle tools explicit preserves meaningful safety distinctions.

## Consequences

- The final public MCP API deliberately breaks callers of seven retired tool
  names. A compatibility phase, migration guide, release boundary, and rollback
  wrapper remain mandatory.
- SCS gains an optional ONNX Runtime dependency, verified local model bundle,
  subprocess protocol, and model lifecycle responsibility.
- The fixed inventory changes from eleven tools to five. Existing internal
  routes and their tests remain valuable implementation boundaries.
- Routing quality becomes a measured product property. Model confidence is
  diagnostic and cannot bypass deterministic eligibility or budget checks.
- A fresh or unconfigured installation remains functional through
  deterministic discovery, without downloading a model or reading another
  product's state.
- The active local checkout and real Laya model must pass the versioned
  orchestration evaluation before legacy-tool retirement.
