---
status: implementing
project: scs
project-root: /Users/leonardo/Repositories/scs
created: 2026-09-22
updated: 2026-09-22
owner: code-intelligence
decision: decisions/2026-09-22-unify-agent-code-queries.md
supersedes:
superseded-by:
implementation:
  commits: [0eadb87]
  pull-request:
---

# Unified Query Orchestration

## Outcome

SCS exposes one goal-oriented, read-only `query_code` MCP tool for code
investigation. A local Laya classifier selects exactly one typed, deterministic
playbook, SCS executes its bounded stages, and the response returns compact
evidence plus an inspectable execution trace. After compatibility and live
evaluation gates pass, `query_code` replaces the seven existing read-oriented
code-intelligence tools. Repository lifecycle operations remain separate.

## Problem and evidence

SCS currently asks the calling agent to choose among seven read tools:
`search_code`, `graph_context`, `get_related`, `list_symbols`, `inspect_file`,
`find_references`, and `regression_risk_report`. The tools expose useful
specialized behavior, but the caller must understand SCS's internal retrieval
taxonomy and manually sequence operations.

The September 2026 usage analysis recorded 647 consecutive `search_code` calls
across 207 SCS-using threads. Structural tools were much less frequently used:
`graph_context` received 91 calls, `get_related` 15, and `list_symbols` 6. The
same analysis found that repeated searches were usually distinct explorations,
not exact retries. This supports consolidating agent-facing selection while
preserving the underlying specialized operations.

SCS already has deterministic hybrid search, optional reranking, graph
traversal, file inspection, indexed references, symbol inventory, impact
analysis, typed degradation, and bounded search modes. The missing layer is a
small decision boundary that maps a user's goal and explicit anchors onto one
of these existing workflows.

Laya is the first candidate because it provides non-generative Choice-style
classification with probabilities, publishes open weights under Apache 2.0,
and can run locally through ONNX Runtime. Its SCS-specific accuracy,
calibration, Apple Silicon latency, packaging integrity, and resource use have
not yet been validated. Published model claims are hypotheses until the live
evaluation in this specification records evidence.

## Scope and non-goals

### In scope

- Add one `query_code` MCP tool with a stable goal-oriented input contract.
- Add a typed decision-provider boundary and a Laya implementation.
- Run Laya in an SCS-owned local ONNX Runtime subprocess.
- Select one fixed playbook in one classifier call per `query_code` request.
- Execute the selected playbook with deterministic, bounded SCS operations.
- Return compact evidence, routing facts, completeness, degradation, and stage
  timings.
- Add a versioned orchestration evaluation suite and deterministic metrics.
- Install the implementation from this checkout as the active local SCS
  runtime and run a live end-to-end evaluation.
- Retire the seven legacy read tools only after the compatibility and
  evaluation gates pass.

### Non-goals

- Laya does not rank search candidates, generate query rewrites, summarize
  source, answer the user's coding question, or decide whether evidence is
  sufficient after retrieval.
- SCS does not implement an open-ended planning loop or let model output name
  arbitrary routes, tools, files, relationships, or parameters.
- The classifier does not receive repository source, retrieved snippets,
  embeddings, graph records, or persisted SCS data.
- `ingest_project`, `ingest_files`, `get_graph_stats`, and
  `delete_repository` are not folded into `query_code`.
- Internal service routes are not removed merely because their MCP wrappers are
  retired.
- No persisted graph or index format changes.
- No hosted classifier or automatic hosted fallback is introduced.

## Public contracts

### Final MCP inventory

After the retirement gates pass, the exact model-facing inventory is:

```text
query_code
ingest_project
ingest_files
get_graph_stats
delete_repository
```

During compatibility validation, the seven legacy read tools remain exposed
beside `query_code`. Their removal is a deliberate breaking API change and
must ship in a release that identifies the migration in the changelog.

### `query_code` input

The public input is one typed request rather than a union of the legacy tool
schemas:

```text
goal: string, required, non-empty
repo_path: absolute repository path, required
mode: fast | balanced | thorough, default balanced
node_type: optional code-node type
symbol_name: optional symbol name
node_ids: optional list of stable SCS node IDs
file_paths: optional list of repository-contained source paths
source_position: optional { file_path, line }
limit: integer, default 10, maximum 50
```

All supplied paths pass the existing canonicalization and containment checks.
`source_position.line` remains zero-based. Collections are deduplicated in
first-seen order and bounded before classifier or graph work begins.

The anchors are evidence supplied by the caller, not instructions to the
classifier. SCS validates them before routing. An ineligible playbook can never
be selected merely because model output requests it.

### Playbook enum

Laya returns one value from this versioned closed enum:

```text
DISCOVER
UNDERSTAND
RELATIONSHIPS
REFERENCES
INSPECT_FILES
IMPACT
INVENTORY
```

Each label has a constant description and eligibility predicate. The model
receives the goal, the presence and bounded values of explicit anchors, and
the enum descriptions. It does not receive source-derived content.

If the selected playbook is ineligible, SCS chooses the deterministic fallback
defined below and records `routing.degraded_reason = "ineligible_playbook"`.
SCS never fabricates a missing file, node, symbol, or source position.

### Mode budgets

Modes determine fixed resource ceilings; the classifier never changes them:

| Budget | Fast | Balanced | Thorough |
|---|---:|---:|---:|
| Classifier deadline | 150 ms | 500 ms | 1,000 ms |
| Search policy | fast | balanced | thorough |
| Search seeds | 5 | 10 | 20 |
| Graph depth | 1 | 2 | 3 |
| Files hydrated | 1 | 3 | 5 |
| Total playbook deadline | 1 s | 7 s | 35 s |

Implementation constants may tighten these limits without changing the public
contract. Increasing a ceiling requires evaluation evidence and an explicit
spec amendment.

### Deterministic playbooks

Every playbook is ordinary typed code with a fixed stage graph:

```text
DISCOVER       search -> compact ranked evidence
UNDERSTAND     search -> seed selection -> bidirectional graph traversal
RELATIONSHIPS  resolve anchors -> bounded directional traversal
REFERENCES     resolve position/node/symbol -> incoming reference evidence
INSPECT_FILES  validate files or search -> bounded file inspection
IMPACT         validate changed files -> dependency/test impact analysis
INVENTORY      validate node type -> paginated symbol inventory
```

`mode` controls the bounds shown above. A playbook can skip a stage when an
explicit anchor already provides its output. It cannot branch back to Laya,
select another playbook, or repeat a stage.

Eligibility and fallback rules are evaluated in this order:

1. `IMPACT` requires at least one validated `file_path`.
2. `REFERENCES` requires a source position, node ID, or symbol name.
3. `INSPECT_FILES` may use supplied files; without them it may inspect only
   files surfaced by its bounded search stage.
4. `RELATIONSHIPS` may use a node ID or symbol; without one it may resolve
   seeds through its bounded search stage.
5. `INVENTORY` uses the supplied node type or the public default symbol type.
6. All other missing-anchor or invalid-selection cases fall back to
   `DISCOVER`.

Within each playbook, ordering, deduplication, graph direction, truncation, and
tie-breaking are deterministic for the same index and provider responses.

### Output

`query_code` returns one stable envelope:

```text
goal
repo_path
routing:
  requested_mode
  playbook
  provider
  model
  confidence
  probabilities
  fallback_applied
  degraded_reason
evidence:
  symbols
  files
  relationships
  references
  test_targets
trace:
  ordered stages with status, counts, truncation, and elapsed_ms
complete
truncated
degraded_stages
timings:
  classification_ms
  execution_ms
  total_ms
```

Evidence items use compact, discriminated variants with stable node identity,
repository-relative file location, line span, evidence kind, and the stage
that produced them. Empty evidence categories remain empty lists so callers do
not need playbook-specific response schemas.

`complete` means that the chosen playbook exhausted its configured bounds; it
does not claim semantic completeness of the repository or correctness of a
user-level conclusion. A timeout, provider failure, missing anchor, unavailable
index, or truncated stage is represented truthfully.

The response contains evidence, not generated prose. Agents remain responsible
for reading authoritative source and forming conclusions.

## Decision-provider contract

The orchestration layer depends on a provider-neutral protocol:

```text
DecisionProvider.classify(request: RoutingRequest) -> RoutingDecision
```

`RoutingRequest` and `RoutingDecision` are strict typed models. A routing
decision must contain a recognized playbook, a finite probability for every
playbook, a finite confidence value, and the configured model identity.
Unknown fields are rejected. Probabilities and confidence are diagnostic; SCS
does not treat them as correctness guarantees.

The first implementations are:

- `LayaDecisionProvider`, using the subprocess protocol below;
- `DeterministicDecisionProvider`, used for tests and fail-open discovery.

Provider failure, timeout, malformed output, subprocess exit, or an unavailable
model selects `DISCOVER` exactly once and records degradation. It never makes
code intelligence unavailable.

## Laya subprocess design

### Ownership and lifecycle

The shared SCS daemon exclusively owns at most one Laya subprocess. It starts
the child lazily on the first classified query, serializes initial model load,
and reuses the ready process. Concurrent requests use a bounded queue and the
daemon's configured inference concurrency.

The daemon starts the runner with an argument vector, never through a shell.
The child is terminated during normal daemon shutdown after in-flight requests
finish or their bounded deadline expires. An unexpected exit fails the active
request open. SCS may perform one lazy restart for a later request; it does not
restart repeatedly inside one request.

### Transport

Parent and child communicate through newline-delimited JSON on private
stdin/stdout pipes. Every request and response carries a generated request ID.
Message schemas reject extra fields and enforce byte, collection, and string
bounds before allocation or inference. Logs use stderr and must not contain the
goal or anchor values.

The protocol includes a startup handshake reporting:

```text
protocol_version
model_repository
model_revision
model_digest
runtime_version
question_schema_version
```

An incompatible handshake disables classification and preserves deterministic
discovery.

### Runtime and model packaging

ONNX Runtime is an optional production dependency activated only when
`decision_model` is configured. The exact package version, supported Python
3.14 wheel, Apple Silicon execution provider, and Linux x86_64 behavior must be
proven before the dependency is locked.

Implementation must pin an exact Laya model repository and immutable revision,
record the weights' Apache 2.0 license, verify every downloaded artifact by
SHA-256, and store the bundle in the SCS model cache. Queries never trigger a
network download. Model acquisition is an explicit setup/install operation and
an absent bundle is a normal fail-open state.

The runner loads only the configured local directory, enables the model
library's offline mode, and does not discover other local model runtimes or
caches. SCS does not depend on oMLX support for this integration.

### Configuration

New typed configuration is disabled by default:

```toml
decision_model = "laya"
decision_model_path = "/absolute/path/to/verified/model-bundle"
decision_timeout_seconds = 0.5
decision_max_concurrency = 1
```

The model path must be absolute, readable, and outside the indexed repository
unless it is beneath the SCS-owned model cache. Configuration stores no secret.
Changing or disabling the decision model requires a daemon restart but never a
repository reindex.

## Security and data

- `query_code` is read-only, non-destructive, idempotent for a stable index and
  model, and closed-world in its MCP annotations.
- The Laya process receives the user goal and bounded explicit anchors. It does
  not receive repository source or retrieved evidence.
- Model output is untrusted. Only strict enum and finite numeric fields cross
  the subprocess boundary.
- Source-like text in the goal is data and cannot name commands or arbitrary
  internal routes.
- The subprocess is never given credentials, an inherited API key, or a shell.
- Model installation is a supply-chain boundary: repository, revision,
  license, digest, and acquisition URL are recorded and verified.
- Routing requests and decisions are not persisted. Existing metrics may
  record only allowlisted content-free dimensions such as playbook, outcome,
  degradation, and latency.
- No authorization, repository-source mutation, graph persistence, or index
  ownership boundary changes.

## Compatibility and migration

Migration has three externally observable phases:

```text
Phase A: query_code + seven legacy read tools
                   |
                   | live equivalence and efficiency gates
                   v
Phase B: query_code preferred; legacy tools deprecated
                   |
                   | release boundary and removal approval
                   v
Phase C: query_code + four lifecycle tools
```

The internal routes behind the legacy tools remain available to playbooks and
service tests in Phase C. MCP aliases are not retained after removal because
aliases would preserve the schema footprint this change is intended to remove.

The migration guide maps every retired input onto `query_code` anchors and
documents output evidence variants. Hard-coded callers receive an unknown-tool
error after Phase C and must migrate.

Rollback before Phase C disables `decision_model` or removes `query_code` while
leaving legacy tools intact. Rollback after Phase C re-exposes the legacy MCP
wrappers in a patch release; internal routes remain available, so rollback
requires no data migration.

## Test strategy

Tests are designed and added before or alongside each implementation unit.

### Provider and subprocess

- Contract tests cover every strict request, response, and handshake variant.
- Procedurally generated malformed messages cover unknown playbooks, missing
  probabilities, non-finite values, oversized inputs, wrong request IDs,
  partial lines, unexpected EOF, and protocol-version mismatch.
- Unit tests cover lazy single-process startup, concurrent first requests,
  bounded queues, timeout, crash, one later restart, shutdown, and log privacy.
- Dependency tests verify the pinned model revision, digests, license metadata,
  offline loading, and supported platform wheels.
- A deterministic fake runner is used in `just verify`; CI never downloads or
  loads Laya.

### Routing and playbooks

- Table-driven tests cover every intent, eligibility predicate, fallback, and
  mode budget.
- Each playbook has integration tests proving its exact ordered stages,
  parameter bounds, deterministic deduplication, evidence projection,
  truncation, degradation, and no second classifier call.
- Property tests prove no generated routing decision can call a route outside
  the closed playbook graph or exceed public ceilings.
- Isolation tests prove every playbook leaves repository bytes unchanged.
- Existing route tests remain authoritative for search, traversal, reference,
  inspection, inventory, and impact semantics.

### MCP compatibility

- Phase A contract tests require `query_code` and all current tools.
- Migration tests compare every legacy read-tool scenario with its
  `query_code` representation and assert equivalent evidence identity.
- Phase C contract tests require exactly the five-tool final inventory and
  reject retired names.
- Input and output schemas are snapshot-tested for intentional compatibility.

### Evaluation

Add `evals/scs-query-v1.json` and `scripts/evaluate-query.py`. Cases contain a
goal, explicit anchors, expected playbook, graded evidence identities, and
allowed legacy baseline calls. Fixtures are generated procedurally where
possible; real repository judgments remain versioned and reviewable.

The evaluator runs both:

```text
unified: one query_code request
baseline: the versioned legacy-tool sequence for the same task
```

It reports:

- routing accuracy and per-playbook recall;
- macro evidence Recall@k, MRR, and nDCG@k;
- unsupported or incorrect evidence rate;
- MCP call count and serialized response bytes;
- classifier, stage, total, p50, and p95 latency;
- fallback, timeout, truncation, and degradation rates;
- agreement between reported confidence and observed routing correctness,
  including expected calibration error and Brier score.

Live Laya values are machine-specific observations, not portable CI gates.
Deterministic metric calculation and fake-provider orchestration remain CI
gates.

## Local-checkout validation

After targeted tests and `just verify` pass, validate the real installation
rather than only the development environment:

1. Record the active `scs` launcher, version, daemon status, configured model
   identity, and current repository readiness without exposing secrets.
2. Build the native extension and install this checkout into the existing uv
   tool environment using the supported cooperative daemon handoff. Do not
   replace files while the previous daemon still owns the runtime.
3. Verify the installed launcher resolves to the checkout build, the daemon
   starts, `scs doctor` passes, and the previously indexed repository remains
   structurally and semantically readable.
4. Explicitly install and checksum the pinned Laya bundle in the SCS-owned model
   cache; enable it in the local SCS configuration.
5. Run the versioned orchestration evaluation against this repository in fast,
   balanced, and thorough modes with warmed repetitions.
6. Restart the daemon and repeat a representative subset to prove model and
   index recovery.
7. Record exact model revision/digest, ONNX Runtime version and execution
   provider, machine architecture, evaluation report path, and all skipped or
   degraded checks.

The validation may stop or restart the SCS daemon through its supported
cooperative lifecycle. It must not interrupt Cortex or alter Cortex's
LaunchAgent. A failed SCS handoff aborts installation before replacing the
active tool.

## Acceptance criteria

- One `query_code` request can represent every supported scenario of the seven
  legacy read tools using the documented anchors.
- Laya is invoked at most once per request and only selects one closed-enum
  playbook; all subsequent stages are deterministic.
- Classifier input contains no repository source or retrieved evidence.
- Provider failure, timeout, malformed output, missing bundle, and subprocess
  crash return bounded `DISCOVER` evidence with truthful degradation.
- The same request, stable index, fixed model revision, and successful provider
  produce the same playbook and evidence ordering across repeated runs.
- The evaluation suite shows no decrease greater than 0.02 in macro evidence
  Recall@10 or nDCG@10 versus its versioned legacy baseline.
- Unified execution reduces mean MCP call count by at least 50% and mean
  serialized response bytes by at least 25% across multi-stage cases.
- On the target Apple Silicon workstation, warmed balanced-mode p95 is below
  five seconds and classifier p95 is below 500 ms.
- Routing accuracy is at least 90%, every playbook's recall is at least 80%,
  and confidence calibration is reported rather than assumed.
- Every response identifies the playbook, provider/model, ordered stages,
  truncation, degradation, completeness, and timings.
- The active local `scs` launcher runs the checkout build, survives daemon
  restart, and completes the live orchestration evaluation without losing or
  mutating indexed repository state.
- Phase C exposes exactly five MCP tools and the migration guide covers every
  retired tool.
- `just verify` passes without requiring a model download or a live Laya
  process.

Legacy-tool retirement requires every criterion above to pass except where a
criterion explicitly reports observational calibration. A failure retains
Phase A, keeps the seven legacy read tools available, and records the evidence;
it is not waived by manually inspecting a few successful requests.

## Risks and recovery

- A wrong route can omit relevant structural evidence. Fixed playbooks,
  eligibility checks, conservative discovery fallback, legacy comparison, and
  per-playbook recall gates limit this risk.
- One public tool has a broader schema than any legacy tool. Typed anchors and
  one stable output envelope trade schema breadth for fewer model-facing
  choices.
- Laya or ONNX Runtime may not support Python 3.14 or Apple Silicon adequately.
  The provider remains disabled and Phase A remains in force until proven.
- Model probabilities may be poorly calibrated on code-investigation intents.
  They are diagnostic only; deterministic eligibility controls execution.
- The subprocess adds memory and lifecycle complexity. Lazy startup, a single
  owner, bounded concurrency, explicit shutdown, and fail-open restart contain
  it.
- Model artifacts create supply-chain and disk risks. Immutable revisions,
  checksums, recorded licensing, explicit acquisition, and the SCS-owned cache
  make installation auditable and removable.
- Removing legacy tools breaks hard-coded callers. Retirement occurs only at a
  documented release boundary after measured compatibility, and re-exposure is
  the rollback path.

No irreversible migration is introduced. Disabling the model, deleting its
cache bundle, reverting MCP registration, or reinstalling the prior SCS release
does not alter repository source or graph data.

## Execution checklist

- [x] Add failing provider, subprocess protocol, lifecycle, timeout, and
      privacy tests.
- [x] Verify and pin the Laya repository, immutable revision, license, model
      digests, ONNX Runtime version, and supported execution providers.
- [x] Implement strict decision models and the provider-neutral interface.
- [x] Implement the SCS-owned Laya subprocess runner and fail-open provider.
- [x] Add failing table-driven tests for routing, eligibility,
      budgets, and deterministic fallback.
- [x] Implement the seven fixed playbooks over existing internal routes.
- [x] Add failing MCP schema, evidence-envelope, and Phase A inventory tests.
- [x] Expose `query_code` beside the legacy read tools.
- [x] Add the versioned orchestration suite, baseline cases, metrics, evaluator,
      and a `just eval-query` command.
- [x] Update README, architecture, configuration, privacy, model-installation,
      and migration documentation.
- [x] Run targeted checks and commit each implementation unit separately.
- [x] Run `just verify` without a live model.
- [x] Install this checkout as the active local SCS runtime through a safe
      daemon handoff.
- [x] Install and enable the pinned Laya bundle explicitly.
- [x] Run and record live fast, balanced, and thorough orchestration evaluations
      plus restart recovery.
- [x] Review every acceptance criterion and retain Phase A if any retirement
      gate fails.
- [ ] In the breaking release, remove the seven legacy MCP wrappers, require
      the exact five-tool inventory, update migration documentation, and run
      `just verify` plus the live evaluation again.

## Verification results

Phase A implementation is present. `just verify` passed with 384 Python tests,
108 Rust tests, strict Python type checking, lint, and the native build. Focused
provider tests exercised a subprocess protocol mismatch and credential-free
environment. A manually launched pinned Laya worker classified two goals and
closed cleanly. The seven legacy read tools remain exposed.

The active `scs` launcher is a symlink to this checkout's `.venv/bin/scs`.
`scs doctor` passed after a cooperative daemon restart. The previously indexed
SCS repository remained readable. Laya revision
`68f27dfe5a27a54fb2b1fefc432f43f972e90868` and ONNX digest
`487746363a8da57bcadb4345352997d22a0fb90d70aa22c6856668d023242aba`
were verified. The local runtime used ONNX Runtime 1.27.0 on arm64 with the CPU
execution provider. The local SCS configuration enables the model; queries do
not download it. A second cooperative daemon restart preceded the thorough
evaluation; `scs list` still showed this repository indexed afterward.

The seven-case warmed live reports are
`evals/reports/2026-09-22-laya-{fast,balanced,thorough}.json`. Balanced and
thorough routing accuracy was 100%, mean call count fell from two to one, and
mean response bytes fell by about 77%. Balanced p95 was 1.93 s and classifier
p95 was 330 ms. Macro Recall@10 matched the legacy baseline at 0.857, but
nDCG@10 fell from 0.804 to 0.699, beyond the allowed 0.02. The IMPACT route
returned no target for the judged MCP test file. Fast-mode classification
timed out at its 150 ms ceiling on all seven cases, reducing routing accuracy
to 14%. The evaluation is a small repository-specific sample and route-call
proxy for MCP payloads, not a broad quality claim. Retirement gates failed;
Phase A remains in force. "Unsupported" evidence in the report means absent
from the small positive-judgment set; the suite does not establish that all
such evidence is incorrect. The legacy baseline route choices are versioned
inputs, so baseline routing accuracy is synthetic and not a model score.
