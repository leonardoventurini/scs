# Preserve source aliases while checking resolved targets

## Context

Ordinary discovery recorded both a source file and its internal symlink alias.
Forced snapshot reconstruction and incremental ingestion resolved the alias first,
then reused the target's relative path. That collapsed distinct file identities
and caused duplicate native node IDs despite occurrence disambiguation.

## Decision

Keep lexical repository-relative source paths as the identity across all ingestion
modes. Resolve targets separately for containment and exclusions. Normalize a
symlinked checkout root without collapsing aliases beneath that root. Both alias
and target must satisfy ignore/exclusion policy. Discovery applies the same target
containment rule already enforced for explicit files; broken and cyclic links
are excluded. Batch target ignore checks during discovery to avoid subprocesses
for every file. Do not modify repository symlinks or dereference them into copies.

Generated fixtures reproduce alias identity across native ordinary/forced/
incremental runs, checkpoint preservation, external/skipped/ignored target
exclusion, dangling/cyclic aliases, and directory/checkout aliases. No private
repository source is copied into tests.

## Alternatives and consequences

Dropping aliases would change ordinary discovery identity and hide valid source
paths. Deduplicating by target during forced runs would disagree with persisted
snapshots. Preserving aliases while checking targets keeps existing identities
and repository containment together. No MCP, parser, or storage schema changes.

A separate existing result-accounting error replaced the embedding total on each
batch. Accumulate it instead; a generated 33-file/two-batch regression proves
66 total embeddings rather than only the final two. This preserves the existing
result-field meaning without adding an interface.

Rollback requires no migration; older code still cannot force-index repositories
containing aliases and can report incomplete embedding totals. Release 0.1.6 is
required because 0.1.5 is already immutable. Full real-repository forced ingestion
and installed artifact verification remain parent-owned rollout checks.
