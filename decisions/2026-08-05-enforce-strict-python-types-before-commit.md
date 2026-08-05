# Enforce strict Python types before commit

## Context

The repository described its Python sources as typed, but `just typecheck` only
compiled files. That caught syntax errors while allowing incompatible calls,
unknown framework values, implicit `Any`, and untyped mutable state. There was
also no repository-owned pre-commit configuration, so developer and
verification behavior could diverge.

## Decision

Basedpyright strict mode is the authoritative static type checker for
`src/scs`. Its configuration additionally rejects explicit or inferred `Any`,
missing type stubs, unannotated class state, implicit overrides, and stale type
ignore comments. Existing source debt is remediated directly; no baseline is
accepted.

The local pre-commit framework invokes that same project command on every
commit with locked dependencies. `just typecheck` and `just verify` use the
same configuration, and `just setup` installs the versioned hook.

Tests are not included in this first strict boundary because the product
contract is first-party runtime source. The proxy retains its independent
Python project and verification command.

## Rejected alternatives

- Keep `compileall`: it provides no static type guarantees.
- Adopt a diagnostic baseline: it would prevent some new debt while leaving
  the stated strict source invariant false.
- Run only on staged filenames: type contracts cross modules, so a local edit
  can invalidate an unstaged consumer.
- Maintain separate hook and verification settings: duplicate policy would
  drift and create commits that pass locally but fail the repository gate.

## Rationale

A single strict, whole-source oracle makes the requirement executable and
keeps indirect type relationships visible. Basedpyright's strict diagnostics
also cover unknown values at dynamic Python boundaries, which are the main
source of false confidence in annotation-only typing.

## Consequences

- Developers must run `just setup` once per checkout to install the hook.
- Commits are blocked whenever any `src/scs` type contract is invalid, even if
  the offending file is not staged.
- Dynamic/native/provider boundaries require explicit protocols and runtime
  narrowing instead of `Any` or blanket suppressions.
- `just typecheck` becomes a meaningful hard gate and may take longer than
  syntax compilation.
