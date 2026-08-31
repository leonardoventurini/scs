# Text fallback ingestion

## Problem

SCS currently discovers only extensions registered by native structural parsers. Useful textual repository context such as `Dockerfile`, dotfiles, extensionless configuration, YAML, TOML, and documentation is therefore absent from lexical and semantic search.

Broad discovery must not turn dependency trees, caches, generated output, binaries, or very large files into unbounded indexing work.

## Evidence

- `src/scs/indexing/discovery.py` filters both full and incremental discovery against `NativeParser.supported_extensions()`.
- `src/scs/indexing/pipeline.py` sends every discovered file to the structural parser and has no plain-text entity fallback.
- Git-backed discovery already respects tracked/untracked visibility and Git ignore rules.
- `ALWAYS_SKIP_DIRS` already excludes common dependency, cache, build, and VCS directories, but has no configurable resource limits or heuristic pruning.

## Uncertainty

- Text detection cannot identify every binary format perfectly. A bounded byte sample and strict decoding checks will intentionally favor false negatives over indexing binary data.
- Generated/vendor classification is heuristic. It must require both a resource threshold and generated/vendor evidence so large first-party source directories remain indexable.
- Repository-scale defaults will need operational tuning after observing real indexing statistics.

## Contracts

1. Native-parser files retain their current structural parsing behavior.
2. Other regular files are ingestable as plain text when a bounded sample contains no NUL bytes and decodes as UTF-8.
3. Plain-text files produce one `file` entity containing bounded searchable text and no invented structural edges.
4. Git ignore rules remain authoritative. Existing always-skipped dependency/cache/build directories remain excluded.
5. Files larger than the configured per-file byte limit are excluded before hashing or parsing.
6. A directory is heuristically pruned only when it exceeds a configured file-count or aggregate-byte threshold and exhibits generated/vendor/cache evidence.
7. Discovery limits have conservative defaults and typed `SCS_` environment overrides.
8. Full discovery, explicit-file ingestion, cleanup, force snapshots, and retry reconstruction apply the same file eligibility policy.
9. Library ingestion's explicit `skip_always_dirs=False` escape hatch continues to bypass directory pruning, but file safety and size limits remain active.

## Proposed defaults

- Maximum individual file size: 1 MiB.
- Text detection sample: 8 KiB.
- Large-directory file threshold: 10,000 files.
- Large-directory aggregate-size threshold: 512 MiB.
- Generated/vendor evidence: known directory-name segments plus high proportions of generated/minified/map/lock artifacts in a bounded deterministic sample.

These values are conservative safeguards, not public protocol constants, and are overridable through `SCSSettings`.

## Test strategy and acceptance criteria

- Unit-test typed defaults, environment overrides, and invalid limits.
- Unit-test `Dockerfile`, dotfile, extensionless, and unfamiliar-extension text discovery.
- Unit-test binary, invalid UTF-8, oversized, ignored, and known dependency/cache exclusions.
- Unit-test that large first-party directories survive while threshold-exceeding generated/vendor-like directories are pruned.
- Integration-test that a plain-text file creates a searchable `file` node while native code remains structurally parsed.
- Verify incremental ingestion and force-snapshot reconstruction use identical eligibility rules.
- Run targeted discovery/config/pipeline tests, strict type checking, and `just verify`.

## Risks

- More files increase hashing, parsing, embedding, storage, and indexing time.
- A permissive classifier could ingest secrets that are present and not ignored. This feature follows the chosen broad-text policy and preserves repository ignore controls rather than adding secret-name exclusions.
- Truncating stored node content can reduce retrieval quality for long text files. The existing bounded-content contract remains initially; changing chunking is separate work.
- Git's flat candidate listing prevents true early traversal termination on its fast path, though pruning before hashing and parsing still avoids the expensive work.

## Recovery

- Disable fallback text ingestion through configuration if operational issues appear.
- Lower file or directory limits to reduce work.
- Revert the implementation commit and run a full reindex; stale text nodes are removed by the existing full-ingestion sweep.

## Direct rollout

Ship the policy enabled by default with conservative limits. Existing indexes change only on their next explicit index/reindex or file-ingestion event. No storage migration or protocol change is required.

## Executable checklist

- [ ] Add failing configuration and discovery tests.
- [ ] Add a failing pipeline test for plain-text file nodes.
- [ ] Introduce a typed ingestion policy sourced from `SCSSettings`.
- [ ] Implement bounded text detection and per-file limits.
- [ ] Implement deterministic directory statistics and conservative generated/vendor pruning.
- [ ] Emit a plain-text `file` entity for unsupported textual files.
- [ ] Wire the policy through daemon-created pipelines and every ingestion mode.
- [ ] Document environment controls and behavior.
- [ ] Record the architectural decision.
- [ ] Run targeted and full verification.

## Verification

The feature is complete when all acceptance tests pass, Basedpyright remains strict, native tests remain unchanged and green, performance ceilings pass, and a temporary repository containing code, `Dockerfile`, dotfiles, binary data, and an oversized generated directory indexes only the eligible text/code files.
