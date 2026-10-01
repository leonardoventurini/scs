---
status: shipped
project: scs
project-root: /Users/leonardo/Repositories/scs
created: 2026-10-01
updated: 2026-10-01
related-specs:
  - 2026-09-04-lazy-mcp-daemon-and-github-release.md
  - 2026-09-08-upgrade-safe-daemon-shutdown.md
---

# Uninstall CLI

## Outcome and scope

`scs uninstall` safely removes the executing uv-installed SCS package while
preserving data. `scs uninstall --purge` additionally removes configured SCS
indexes, jobs, configuration, model cache, logs, and runtime artifacts. MCP
registrations stay untouched; successful output explains their manual removal.
The user approved these two choices before implementation.

This extends the [release lifecycle](2026-09-04-lazy-mcp-daemon-and-github-release.md)
and reuses [cooperative shutdown](2026-09-08-upgrade-safe-daemon-shutdown.md).
Its optional purge supersedes the prior manual-only cleanup guidance. No schema,
production dependency, MCP inventory, or service-manager integration changes.

## Evidence and contracts

- Release installation uses `uv tool install`. The command supports that
  installation method; source-tree, pip, and pipx invocation must fail safely.
- uv must be available on PATH. An installer can use a temporary uv binary;
  users of those installations must install uv before running this command.
- Verify the current environment's uv receipt and package location before
  changing lifecycle state. Pin uv's tool directory to the executing instance,
  so changed environment variables cannot select a different installed SCS.
- Validate purge paths before shutdown. Configured directories must be dedicated
  to SCS; reject symlink roots, shared/system containers, repository trees,
  registered source roots, and paths overlapping the running installation.
- Hold bootstrap ownership across cooperative cancellation, shutdown, package
  removal, and cleanup; hold writer ownership once shutdown releases it.
- Failed validation, lock acquisition, or shutdown leaves installed code and
  existing data intact. Failed package removal must not trigger purge.
- Cleanup happens after successful package removal. Cleanup failure returns
  nonzero and reports remaining paths; removed data cannot be restored.
- Purge includes `~/.scs/config.toml` even for a custom data root, but does not
  remove a different root's indexes. Missing paths are harmless. Nested roots
  are cleaned once; internal symlinks are unlinked without following them.
- Retain `.bootstrap.lock`, `.daemon.lock`, and the directories containing
  them: replacing lock inodes would break concurrent lifecycle coordination.
- Close connected MCP clients before uninstalling. Registrations are never
  edited and repository source is never removed.

```text
verify installation + validate cleanup paths
                   |
          hold bootstrap lock
                   |
        cancel work + stop daemon
                   |
           hold writer lock
                   |
         uv tool uninstall scs
                   |
    optional purge (retain lock inodes)
                   |
        print MCP removal guidance
```

## Acceptance criteria and planned verification

1. Both forms parse and appear in CLI help; contract tests cover dispatch.
2. Default uninstall preserves existing data, cache, logs, and config contents;
   temporary-directory tests compare those contents.
3. Stop and writer-lock acquisition precede package removal, and stop failures
   or lifecycle contention prevent removal; procedural tests observe ordering
   and lock ownership.
4. Missing uv and non-uv invocation fail before shutdown. The executing tool
   root controls removal even if `UV_TOOL_DIR` points elsewhere.
5. Purge removes configured state and fixed config only after successful
   package removal; tests include nested roots, absent directories, internal
   symlinks, unsafe roots, enrolled sources, and package/cleanup failure.
6. MCP instructions appear without any harness configuration edits.
7. A disposable uv installation can uninstall itself with preservation and
   purge, without affecting the active installation. Verify on local macOS;
   Linux execution remains a disclosed platform limitation.

Run targeted tests first, then `just verify`, and manually exercise a wheel in
isolated uv tool/data/runtime directories. Tests must not invoke the real
instance's uninstall or stop commands.

## Recovery

Reinstall a compatible release to restore the executable. Preserved data is
reusable. `--purge` is explicitly destructive: restore a backup to recover
deleted state. On cleanup failure, use the reported remaining paths for manual
cleanup after checking that no clients remain connected.

## Verification results

Implementation: `feat(cli): add safe uninstall with optional state purge`.
Review the CLI and `src/scs/uninstall.py`, then its contract/unit tests, then the
release documentation. No contract deviations; retained lock inodes are the
documented cleanup exception.

- Criteria 1 and 6: passed via CLI parser/dispatch/output tests. MCP config
  preservation was also checked in procedural purge tests and installed-wheel
  smoke runs.
- Criteria 2–5: passed via procedural tests for preserved contents, original
  tool selection, absent uv/non-uv invocation, shutdown/removal failure,
  bootstrap/writer contention, root and lock symlinks, common shared roots,
  corrupt catalogs, enrolled source overlap in both directions, nested/missing
  cleanup roots, retained inode identities, and partial cleanup failure.
- Criterion 7: passed on Apple Silicon macOS. `uv build --wheel` produced a
  wheel installed twice into temporary uv tool directories. The installed CLI's
  help exposed `--purge`; separate installed Python processes invoked CLI main
  for preserve and purge, with `Path.home()` patched to a disposable user root.
  Both processes removed their executing environments and registered launchers,
  ignored a deliberately changed `UV_TOOL_DIR`, retained MCP configuration, and
  produced the expected data results. Temporary installations were cleaned up;
  the active SCS instance was untouched.
- `uv run --all-groups pytest -q tests/unit/test_uninstall.py
  tests/contract/test_cli_contract.py tests/unit/test_daemon_controller.py`:
  54 passed.
- `just verify`: strict Basedpyright and Ruff passed; 422 Python tests passed,
  87.28% branch-aware coverage (83% gate), and 108 Rust tests passed.
- `git diff --check`: passed.

Linux wheel/self-uninstall execution was not run locally. The wheel smoke runs
used absent-daemon shutdown; the full suite separately exercised daemon
shutdown/cancellation and lifecycle coordination. pip and pipx support,
automatic MCP configuration changes, and publishing/installing the updated
release on the active workstation are outside this approved scope.

The command was subsequently published as
[SCS 0.2.2](https://github.com/leonardoventurini/scs/releases/tag/v0.2.2).
Both hosted platform gates and wheel smoke tests passed, and the public macOS
installer exercised both self-uninstall modes. See the
[release verification](2026-10-01-release-0.2.2.md); the active installation was
not upgraded.
