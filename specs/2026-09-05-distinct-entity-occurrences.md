# Preserve distinct parsed entity occurrences

## Problem and evidence

Mentagen ingestion fails after parsing roughly 2,925 files with TSG's
`node IDs must be non-empty and unique within a batch` error. A read-only native
parser scan found 430 duplicate identity groups, including separate type/value
imports from one package and CSS variables declared in multiple scopes. SCS
hashes only repository, path, kind, and qualified name. Its embedding lookup also
uses only path and qualified name, aliasing repeated entities and different kinds.

## Scope, contracts, and uncertainty

Fix SCS identity planning and embedding association. TSG correctly enforces unique
batch IDs and does not need a change. Keep wire/storage schemas, 32-character
opaque IDs, parser interfaces, and non-colliding symbol IDs unchanged. Preserve
the first occurrence's existing hash; derive subsequent hashes using their
ordinal among occurrences of the same kind and qualified name within that file.
Occurrence order follows deterministic parser output, independent of source line
numbers. Inserting/removing a same-name occurrence can shift later occurrence IDs;
whole-file replacement removes stale nodes and vectors. This is derived index
data, not source mutation.

Qualified-name edges remain a canonical symbol lookup and cannot distinguish
individual overloads/occurrences without a richer parser reference contract.
Do not invent precise call-target relationships as part of this fix.

## Tests and executable checklist

- [x] Add native parser/storage regressions with generated repeated TS imports
      and CSS declarations; reproduce the failed batch before implementation.
- [x] Preserve every occurrence, assign unique IDs, and map embeddings by parsed
      entity position rather than a lossy qualified-name key.
- [x] Verify forced re-ingestion stability, whitespace stability, distinct
      embeddings, and removal of stale occurrences.
- [x] Run affected tests, then `just verify`, keeping performance ceilings.
- [x] Bump SCS to an unused patch version and update release metadata/lockfiles.
- [x] Record the decision and verified results; commit with hooks enabled.
- [ ] Push main, await CI, tag and publish the immutable GitHub release.
- [ ] Verify checksums, install the released artifact, and restart the daemon.
- [ ] Ingest Mentagen and verify graph/search readiness through MCP.

## Release, risks, and recovery

The user explicitly authorizes direct SCS/TSG fixes, publication, and consumer
updates. Current SCS main also contains three existing unpublished commits
(44c1576, 8fef52d, 8d46b88); the release includes these previously committed fixes.
Use the documented GitHub release installer, preserving SCS_HOME and configuration.
Do not replace immutable release assets or weaken TSG's duplicate-ID validation.
Rollback code with a prior compatible release. Indexes are rebuildable; a forced
reindex with the previous implementation cannot handle the colliding sources.
Concurrent source edits may cause retryable discovery/hash failures; verify final
job status rather than treating queue acceptance as completion. If old bridges
retain the old runtime, reconnect only SCS processes and verify the new daemon.

## Anonymized fixture requirement

The user requested anonymized fixtures during implementation. Procedural fixtures
now generate invented type/value imports, custom properties across selectors,
and TypeScript type/value declarations sharing qualified names, in both same-line
and multiline layouts. No original application source or identifiers are copied.

## Local verification

- Original native TS/CSS regressions failed with TSG duplicate-ID rejection.
- Eight anonymized native fixture cases pass after the fix.
- `just verify` passes: 244 Python tests, 98 Rust tests, strict Basedpyright,
  Ruff, native build, and 84.58% Python coverage against the 83% gate.
- Release identity check passes for 0.1.4. The release identity unit test now
  compares metadata against the runtime version rather than a stale hardcoded tag.
- A temporary native structural-only ingestion of Mentagen succeeds with 2,929
  discovered files, 21,994 entities, and zero failed files. Semantic embeddings
  are reserved for the final installed-service verification.
- Code review found no blocker; canonical qualified-name edge ambiguity and
  ordinal shifts within edited repeated groups remain documented limitations.

## Linux CI discovery

CI run 33977741437 passed on macOS and passed supply-chain checks. Linux passed
242 tests, including every occurrence regression, but failed two preexisting
shutdown cases (`partial_header`, `partial_body`) introduced by the unpublished
lifecycle changes included in this release. Linux resets a Unix socket closed
with unread partial data; the tests incorrectly require EOF and rethrow the reset
in cleanup. Correct the test closure contract to accept EOF or ConnectionResetError
only for incomplete-frame cases, preserving timeout, stop completion, socket
removal, and lease-count assertions. Production wire behavior is unchanged.
Rerun the full local gate and both CI platforms before tagging.

The portable shutdown assertions pass all 17 daemon tests on macOS and in a
Linux CPython 3.14 container with read-only source. No native build or host package
changes were required for the focused Linux reproduction. Idle/attached clients
still require graceful EOF; only partial-frame cases accept reset. Timed shutdown,
socket removal, and client-count assertions remain intact.

## Installed verification discovery: indexed relationship lookup

SCS 0.1.4 was published from commit 305aa0d after CI 33978442829 and release
33978925410 passed. The checksum-verified released installer installed the macOS
wheel, and a fresh MCP session confirmed runtime 0.1.4 from site-packages. The old
daemon briefly retained its lock while finishing shutdown, then exited without
forced termination. Mentagen's structural graph stored 22,116 nodes; an interim
read confirmed 3,936 persisted embeddings and the same vector index count.

Concurrent source edits merged the initial watcher ingestion into a follow-up,
preserving acknowledged files. During follow-up edge planning, a process sample
showed repeated TSG attribute scans. SCS already memoizes positive and negative
qualified-name lookups and creates an expression index for `$.qualified_name`.
TSG's bound JSON path prevents SQLite from matching that index; its optional-scope
OR predicate also inhibits scoped index access. An isolated EXPLAIN reproduced
the scan, and validated literal-path SQL with direct scope equality used the index.

The user authorizes direct upstream TSG fixes, publication, and consumer updates.
Extend this task to correct internal TSG query planning with generated correctness
and query-plan regressions, preserving public API, schema, value binding, and JSON
path validation. Publish the verified upstream patch, update SCS to that immutable
tag, and publish a new SCS patch. Existing release assets remain immutable. Final
installed ingestion and search checks must use the resulting released wheel.

A local isolated SQLite probe with 22,000 procedurally generated nodes and 100
missing-name lookups took 0.897 seconds with the old query and 0.00104 seconds
with the index-compatible query. This in-memory synthetic result validates the
query-planning diagnosis; it is not an end-to-end ingestion time guarantee.

SCS 0.1.5 consumes TSG v0.2.2 at immutable commit
`6e6e607ed80704b4169ed52c5217e76d9a36196a`. The consumer `just verify` gate passes:
244 Python tests, 98 Rust tests, strict types/lint, native build, and 84.58%
coverage. The release identity check passes for v0.1.5.

## Consumer CI timing discovery

CI 33981446767 passed macOS and supply-chain checks. Linux passed 243 tests
but its fresh-index isolation test stopped after 100 ten-millisecond polls while
the requested background job was still running without an error. Replace the
iteration-count assumption with an explicit bounded integration-test deadline,
track the acknowledged job ID, and report terminal failures or timeout state.
Preserve fresh-root isolation and indexing assertions; do not change production
timeouts or performance-test ceilings. Verify repeatedly before rerunning CI.

## Bulk replacement deletion follow-up

A subsequent runtime sample identified another ingestion bottleneck: replacing
files calls native single-node deletion for every old entity, and TSG rebuilds
its accelerator after every call. Its existing bulk-delete API can perform the
same cascade once per replacement plan. Add an internal typed Python/Rust bridge
and call it once with sorted unique replacement IDs, skipping empty plans.
Preserve inbound-edge capture before deletion, stale node/vector cascades, and
complete-file acknowledgement. No MCP, wire, schema, or TSG changes are needed.
Test with generated native occurrence fixtures and count bulk invocations; native
storage tests should prove one generation transition and correct edge/vector
cleanup. Run affected tests and full verification before committing. Rollback
requires no data migration and restores slower per-node behavior.

Bulk deletion verification: the two generated native lifecycle cases failed
before the pipeline change because replacement never invoked a bulk operation.
Afterward, all 29 affected indexing/property tests passed. The Rust adapter test
proved one generation increase for seven generated node deletions, empty-batch
no-op, edge cascades, and durable vector absence/survival. Full `just verify`
passed: 244 Python tests, 99 Rust tests, strict typing/lint, and 84.57% coverage
against 83%. Native rebuilding and existing performance ceilings passed. The
installed production ingestion has not yet run this newly compiled release;
release-installed verification remains a rollout step.

## Force snapshot symlink identity follow-up

The released occurrence fix passed ordinary discovery (2,973 files and 22,448
unique IDs), but forced snapshot reconstruction failed again. `build_file_entry`
resolved a source symlink before computing its relative path, collapsing the
instruction-file alias onto its target while discovery retained both names.
Preserve lexical repository-relative identity in the single-file builder and
keep separate resolved-target containment, skipped-directory, generated-directory,
and ignore checks. Avoid changing full-discovery symlink policy. Generated
alias fixtures must cover ordinary/forced/incremental ID agreement, distinct
checkpoints, and external/excluded target rejection. No file or index mutation
outside explicit ingestion; a new immutable release is required.

Alias containment consistency includes ordinary discovery, preserving the
repository-scoped read contract already enforced by explicit-file construction.
Generated tests cover both Git-listing and filesystem-fallback paths, internal
file/directory aliases, root aliases, outside targets, exclusions/ignores, dangling
links, and cycles. A separate generated two-batch accounting regression failed
with 2 embeddings reported versus 66 stored; accumulation restores total reporting.
The native alias test failed before the fix with the exact duplicate-ID storage
error; targeted discovery/indexing coverage now passes all 42 cases.

Alias/accounting candidate verification completed: `just verify` passed 250 Python
tests, 99 Rust tests, strict typing/lint, native rebuild, and existing performance
ceilings. Coverage is 84.61% against 83%. An initial strict typecheck identified an
untyped empty-set branch in the new ignore-target collection; an explicit set type
fixed it before the successful full run. Parent owns the version metadata and
real-project verification commit, followed by publication/installed reindex.

## SCS 0.1.6 real-project verification

Against the current Mentagen checkout, temporary native structural-only storage
completed both normal ingestion and force-snapshot ingestion: 2,973 files,
22,448 entities, 19,003 edges, 25,132 unresolved/dropped edges, and zero failed
files in each pass. Normal ingestion took 6.95 seconds; force snapshot validation
and ingestion took 50.79 seconds. All 2,973 file hashes were acknowledged, with
AGENTS.md and its CLAUDE.md alias retaining distinct identities. No model requests
or persistent service data were used for this diagnostic. The released wheel
with the configured model still requires final ingestion and search verification.

## MCP source alias forwarding follow-up

Although ingestion now stores distinct alias identities, MCP source validation
still resolves aliases before forwarding reads and incremental requests; service
routes repeat that collapse for ingestion, regression risk, and indexed reference
lookup. Share source-path normalization between discovery, MCP, and services:
retain lexical file identity, independently validate resolved target containment,
and normalize checkout-root aliases. Keep wire/MCP schemas and scope boundaries
unchanged. Generated alias fixtures must exercise MCP forwarding, durable job
payloads, index lookup identity, and unsafe target rejection. Do not restart the
active released force ingestion. Publish 0.1.7 only after the current run completes.

MCP alias validation verification: the new forwarding and queued-payload tests
failed before the fix because aliases were replaced by targets. After shared
validation and backend corrections, 40 focused cases passed, including a native
indexed alias exercised through inspection, risk, and public reference routes.
Full `just verify` passed 254 Python tests, 99 Rust tests, strict typing/lint/native
rebuild, existing performance ceilings, and 84.67% coverage against 83%. A bounded
read-only request to the busy installed daemon initially timed out; no daemon
restart or index write was performed. Installed alias addressing is a post-release
verification step and does not require regenerating already correct index data.

Direct bounded SCSWire inspection during the installed 0.1.6 force job confirms
the native store already retains both aliases: AGENTS.md has ID
`5b48058529d9d1d8003dd7daf980559c`; CLAUDE.md has ID
`7580b42990ca2c809f25db1a9cb51cb3`. Each retains its corresponding metadata path.
The 0.1.7 change corrects request addressing and requires no index rebuild.
Let the current model-backed force pass finish, then install released 0.1.7 and
verify searches and alias inspection through a fresh MCP connection.
