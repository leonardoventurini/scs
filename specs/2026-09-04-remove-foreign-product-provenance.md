# Remove Foreign-Product Provenance

Project: `scs`

Project root: `/Users/leonardo/Repositories/mentagen/scs`

## Problem

SCS still names and models the product from which its earliest implementation
was separated. Those references appear in runtime path guards, MCP inventory
contracts, tests, scripts, documentation, decision records, agent guidance,
and reachable Git history. SCS must instead describe and enforce only its own
standalone contracts.

## Evidence and uncertainty

- The current tree contains foreign-product references in runtime Python,
  contract and isolation tests, shell scripts, documentation, and records.
- Reachable historical commits also contain the references.
- Runtime storage validation currently reserves external environment variables,
  directory names, and marker files that are not part of SCS's public contract.
- Only `main` is published, with three release tags. Rewriting every reachable
  ref will change commit identities and invalidate existing source provenance.

## Contracts

- The current tree and every reachable Git object contain no case-insensitive
  occurrence of the prohibited product name.
- SCS retains an exact ten-tool MCP allowlist, source-read-only behavior, empty
  startup, strict types, and its own storage/runtime ownership rules.
- Runtime configuration recognizes only SCS-owned settings and paths.
- Obsolete cross-repository cutover, rollback, and witness scripts are removed.
- All local branches and tags are rewritten, then force-updated on GitHub.
- Published binary release assets remain usable, while their source commit and
  attestations are documented as superseded by the rewritten refs.

## Risks

- Rewriting public history requires existing clones to re-clone or hard-reset.
- Signed attestations and source archives attached to earlier releases refer to
  pre-rewrite commits even though installed binaries remain unchanged.
- Removing external legacy-path collision checks means callers alone choose a
  safe `SCS_HOME`; SCS continues to isolate all writes beneath that root.
- Removing historical decision prose reduces extraction provenance by design.

## Recovery

Create a full repository bundle in an owner-only temporary directory before
rewriting. Retain it until the rewritten refs, tests, remote object audit, and
fresh clone audit succeed. Restore with a mirror push from that bundle only if
the rewrite is proven incomplete or corrupt.

## Direct rollout

1. Create an offline recovery bundle and record all current refs.
2. Replace foreign-derived runtime contracts with SCS-native contracts, delete
   obsolete scripts/records, and update tests and documentation.
3. Run targeted tests, zero-reference audits, and the full quality gate.
4. Commit the clean current tree.
5. Rewrite every branch and tag, removing prohibited text from all blobs and
   commit messages while preserving unrelated content.
6. Force-push rewritten branches and tags, prune obsolete remote refs, and
   verify GitHub from a fresh public clone.

## Executable checklist

- [ ] Recovery bundle and original refs recorded.
- [ ] Runtime and test contracts are SCS-native.
- [ ] Obsolete cross-repository scripts and records removed.
- [ ] Current-tree zero-reference audit passes.
- [ ] Targeted tests and `just verify` pass.
- [ ] Every branch and tag is rewritten and force-pushed.
- [ ] Fresh-clone current-tree and reachable-object audits pass.
- [ ] Recovery bundle removed after successful remote validation.

## Verification

- Search tracked and hidden files case-insensitively, excluding `.git`.
- Enumerate all reachable blobs and commit messages and scan their contents.
- Assert the MCP server exposes exactly the documented ten SCS tools.
- Run path/config, empty-startup, isolation, and MCP contract tests first.
- Run `just verify` before rewriting and again from a fresh clone afterward.
- Compare local and remote branch/tag object IDs after the force-push.
