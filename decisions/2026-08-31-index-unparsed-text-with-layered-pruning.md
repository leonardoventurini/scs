# Index unparsed text with layered pruning

## Context

SCS previously equated parser support with file eligibility. This kept the
structural graph code-only but omitted repository context stored in Dockerfiles,
dotfiles, extensionless files, and configuration or documentation formats.
Simply accepting every regular file would expose indexing to binaries, large
artifacts, dependency trees, generated output, and caches.

## Decision

SCS separates discovery eligibility from structural parser support. A file
handled by a native parser retains structural extraction. Any other bounded
regular file that passes strict sampled UTF-8 detection becomes one file-level
text entity with no invented structural edges.

Resource control is layered:

1. Git ignore rules remain authoritative.
2. Known dependency, cache, VCS, environment, and build directories are always
   excluded from normal repository ingestion.
3. Per-file size and text-sampling limits bound file work.
4. A directory is heuristically pruned only when it exceeds a file-count or
   aggregate-size limit and also has generated/vendor naming or a high sampled
   ratio of generated artifact filenames.

The limits have conservative defaults and typed `SCS_` environment overrides.
The policy is shared by full indexing, explicit-file ingestion, cleanup, and
force-snapshot reconstruction.

## Rejected alternatives

- An allowlist of known configuration formats would repeatedly miss new tools,
  custom filenames, and extensionless text.
- Threshold-only pruning would incorrectly discard large first-party source
  trees.
- Relying only on Git ignores would make safety dependent on repository hygiene.
- Parsing arbitrary text as code would create misleading entities and edges.

## Rationale

File-level text preserves useful search context without weakening the typed
code graph. Requiring both scale and generated/vendor evidence makes heuristic
pruning conservative, while hard exclusions and per-file limits address common
high-cost cases deterministically.

## Consequences

- Search covers substantially more repository-owned textual context.
- Indexes may contain more file nodes and embeddings after the next explicit
  index or reindex.
- Present, unignored secrets are eligible under the deliberately broad text
  policy; repository ignore rules remain the owner-controlled exclusion layer.
- Text classification and directory pruning remain heuristics and may need
  default tuning from measured repositories.
- No storage migration or SCSWire/MCP protocol change is required.
