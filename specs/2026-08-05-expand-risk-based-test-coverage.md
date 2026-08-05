# Expand risk-based test coverage

## Goal and scope

Expand automated coverage around SCS contracts whose failure can lose durable
work, cross repository boundaries, or silently mis-index source. Establish a
reproducible Python branch-coverage command and improve the measured baseline
with focused unit, integration, and native tests.

The goal is not blanket line coverage. Trivial protocol ellipses, CLI process
entrypoints already covered through subprocess contracts, and defensive paths
that require unsafe host mutation are lower priority than state transitions,
filesystem trust boundaries, persistence recovery, and native error behavior.

## Evidence and uncertainty

- Baseline command:
  `coverage run --branch --source=src/scs -m pytest -q && coverage report`.
- Baseline totals: 3,026 statements, 486 missed, 638 branches, 270 missed;
  83.94% statement coverage, 57.68% branch coverage, and 79.37% combined.
- `indexing/jobs.py` is 64% combined and 38% branch-covered despite owning the
  durable queue state machine. Direct execution confirms the retry behavior is
  sound but was previously unprotected by any regression oracle.
- `indexing/repository_paths.py` is 51% combined and 38% branch-covered despite
  owning home-directory and nested-repository safety policy.
- `indexing/discovery.py` is 69% combined and 59% branch-covered; Git failure,
  ignore fallback, path containment, and unreadable-file behavior are sparse.
- Rust coverage tooling is not installed, so native gaps require public-symbol
  to test mapping plus targeted failure-oracle tests rather than a percentage.
- Native negative-path probing exposed a data-integrity defect: a replacement
  embedding with the wrong dimension can update SQLite and remove the prior
  valid vector before returning an error.
- Main uncertainty: raw coverage can overvalue shallow API invocation. New
  tests must assert durable state or boundary outcomes, not merely execute code.

Stop and revisit scope if a test requires destructive host state, network
services, timing-dependent sleeps, or a product contract change beyond a defect
directly demonstrated by an uncovered path. The demonstrated native atomicity
defect is in scope and raises this rollout to high data-integrity risk.

## Contracts and decisions

- Job transitions preserve at most one queued follow-up per repository, release
  leases on every terminal/retry path, and merge payloads without losing
  changes.
- Repository safety rejects the user home and nested repositories, chooses the
  nearest indexed parent, and handles duplicate cleanup failures truthfully.
- Discovery never indexes unsupported, ignored, escaped, missing, or unreadable
  files; Git unavailability falls back deterministically to root ignore rules.
- Native persistence tests target failure atomicity and compatibility behavior
  not already protected by existing examples.
- A replacement embedding rejected for the wrong dimension must preserve the
  complete prior node and vector state. Dimension validation occurs before
  either storage backend is mutated.
- Coverage configuration records branch data and reports missing lines. A
  threshold may be added only after the final measured result supports a stable
  non-regressive floor.

## Risks and recovery

- Tests could lock implementation details. Prevent by asserting observable job,
  filesystem, wire, or database state; detect in independent review; recover by
  replacing brittle assertions before commit.
- Queue tests could be flaky through wall-clock leases. Use negative lease
  durations and state reads instead of sleeps.
- Filesystem tests could depend on macOS casing. Patch the narrow listing seam
  or assert platform-independent containment rules.
- Coverage tooling could bloat runtime dependencies. Add it only to the dev
  group through `uv`, keep runtime dependencies unchanged, and provide one
  explicit `just coverage` command.
- The native atomicity repair could still permit partial mutation. Prevent by
  validating dimensions before SQLite/vector writes; detect with a seeded valid
  prior state and rejected replacement; recover by reverting the code change
  because no schema or persistent migration is involved.

## Verification gauntlet

- Hard gate — retry state machine: targeted job-store tests prove queued retry,
  terminal failure, queued-followup merge, cancellation, and lease release
  outcomes.
- Hard gate — repository trust boundary: direct tests assert home/nested denial,
  nearest-parent selection, canonical duplicate handling, and deletion failure
  accounting.
- Hard gate — discovery boundary: deterministic tests cover Git success/failure,
  ignore fallback, skip directories, path escape, and read failure.
- Hard gate — native invariants: targeted Cargo tests exercise independently
  identified persistence or FFI failure branches. The highest-risk oracle seeds
  a valid node/vector, attempts an invalid-dimensional replacement, and requires
  both old states to remain unchanged.
- Diagnostic metric — Python coverage: rerun the identical branch measurement,
  report baseline versus final totals, and add a non-regressive floor only if it
  is comfortably below the achieved evidence.
- Hard gate — integration: `just verify` passes after rebuilding native code if
  Rust/PyO3 changes occur.
- Hard gate — review: an independent read-only reviewer validates risk ranking,
  assertion strength, and remaining gaps.

## Execution checklist

- [x] Expand durable queue tests and fix only defects they expose — files:
  `tests/unit/test_indexing_jobs.py`, `src/scs/indexing/jobs.py`; verify:
  `pytest -q tests/unit/test_indexing_jobs.py`.
- [x] Add repository-path and discovery boundary tests — files:
  `tests/unit/test_repository_paths.py`, `tests/unit/test_indexing_discovery.py`;
  verify both modules with targeted pytest.
- [x] Add highest-value native tests and repair the demonstrated atomicity defect
  without changing successful behavior — files under `crates/scs-store/`;
  verify owning crate tests and the seeded negative control.
- [x] Add reproducible dev-only branch coverage command and record the measured
  improvement — files: `pyproject.toml`, `uv.lock`, `Justfile`, this spec;
  verify `just coverage`.
- [x] Run independent review, `just verify`, final diff/status review, and make
  one path-limited semantic commit.

## Verification and rollout

Tests and the native dimensionality fix roll out together. No persistent schema
or user data is changed. Recovery is a normal revert of the task commit; the
added coverage command has no production runtime effect.

## Measured result and remaining gaps

The identical Python measurement now collects 130 tests. The final full gate
reports 3,026 statements with 360 missed plus 638 branches with 213 missed:
88.10% statement, 66.61% branch, and 84.36% combined coverage. Repeated runs
landed between 84.36% and 84.58% because existing watcher tasks can execute a
few additional shutdown lines before coverage stops. The committed floor is
83%, below that observed range but above the original 79.37% baseline. The
focused gains are `jobs.py` 64% → 86%, `repository_paths.py` 51% → 96%,
`discovery.py` 69% → 88%, and `routes.py` 78% → 81%. Four new native and
cross-language negative tests protect vector and cross-store dimensional
integrity.

Remaining useful work, ordered by risk rather than raw percentage:

- Late daemon-startup unwind and watcher restoration in `main.py` remain
  branch-light; failures after identity publication need a stronger resource
  ownership oracle.
- Runner exception, cancellation, and per-mode dispatch paths remain only 80%
  combined and should receive a typed pipeline/event-state matrix.
- Native truncate/clear operations need reopen tests spanning SQLite and the
  vector sidecar; parser registry mappings need a single consistency property.
- `mcp/paths.py` and `mcp/server.py` have low percentages, but much of the server
  gap is repetitive tool forwarding already exercised through the live method
  inventory. Path normalization error branches remain the higher-value subset.
- `cli.py` remains 50% in-process because operational commands are exercised by
  subprocess contract tests, which do not attribute child-process lines to the
  parent coverage file. Its raw percentage therefore understates behavioral
  coverage and is not a priority for shallow in-process mocks.
