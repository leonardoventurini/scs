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
