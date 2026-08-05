# Strict Python type gate

## Goal and scope

Make strict static typing a blocking, repository-owned pre-commit and
verification gate for first-party Python under `src/scs`. Replace the existing
syntax-only `typecheck` recipe with Basedpyright, remediate the existing source
without a diagnostic baseline, and preserve runtime behavior. Tests and the
proxy remain outside this rollout's strict source boundary.

## Evidence and uncertainty

- `just typecheck` currently runs `compileall`, so it does not validate types.
- The repository has no versioned pre-commit configuration.
- A strict Basedpyright reconnaissance run, after resolving the `src` import
  root correctly, reports 371 diagnostics across 27 source files.
- The largest risks are dynamically decoded job payloads, framework callback
  discovery, and optional/native provider boundaries.

The main uncertainty is whether stricter boundary types expose latent runtime
contract errors. Stop and revise the rollout if remediation requires changing
wire formats, persisted payloads, public MCP inventory, or native behavior.

## Contracts and decisions

- `uv run --all-groups basedpyright` is the single authoritative type command.
- `typeCheckingMode = "strict"`; `Any`, explicit `Any`, missing stubs, unknown
  types, unannotated instance state, implicit overrides, and unnecessary type
  suppressions are errors.
- No baseline file or blanket ignore is permitted. Boundary data must narrow
  from `object`/`unknown` through validation or a typed protocol.
- `.pre-commit-config.yaml` invokes the authoritative checker for every commit,
  independently of the staged file set, so indirect contract breakage is
  caught.
- `just setup` installs the repository hook after dependencies and the native
  extension are available.

## Risks and recovery

- Incorrect narrowing could change runtime behavior. Preserve existing tests,
  add focused coverage if behavior must change, and inspect every conversion.
- The hook could diverge from CI/local verification. Both call the same
  Basedpyright configuration and dependency lock.
- A missing environment could make commits fail unclearly. `just setup` owns
  dependency synchronization, native build, and hook installation.
- Rollback is a normal revert of the task commit; no persistent product state
  or schema changes are involved.

## Verification gauntlet

- Hard gate — strict source contract: `just typecheck`; zero diagnostics.
  Sensitivity: temporarily introduce an untyped function and confirm the hook
  rejects it, then remove the mutation.
- Hard gate — hook wiring: `uv run --all-groups pre-commit run
  strict-python-types --all-files`; exit zero after sensitivity proof.
- Hard gate — behavior preservation: `just test`; all Python tests pass.
- Hard gate — native boundary preservation: `just native-test`; all workspace
  tests pass.
- Hard gate — repository quality: `just lint`; zero diagnostics.
- Diagnostic — final diff review confirms no baseline, broad suppressions, or
  unrelated files.

## Execution checklist

- [x] Configure Basedpyright and the locked development dependencies in
  `pyproject.toml` and `uv.lock`; verify with `just typecheck`.
- [x] Remediate `src/scs` diagnostics using typed state and validated boundary
  narrowing; verify zero strict diagnostics.
- [x] Add `.pre-commit-config.yaml` and align `justfile`; verify the installed
  hook executes the same gate.
- [x] Record the durable workflow decision under `decisions/`.
- [x] Run the verification gauntlet and independent review; prepare the
  path-limited semantic commit.

## Verification and rollout

The change rolls out directly in one commit. Developers run `just setup` once
to install the hook; subsequent commits and `just verify` block on the same
strict contract. There is no runtime deployment or data migration.

Verification completed with zero strict diagnostics, a passing installed hook,
96 Python tests and all Rust workspace tests. A temporary untyped source file
made the hook fail with four type diagnostics, proving the gate's sensitivity.
Independent review found and corrected two compatibility regressions in numeric
provider conversion and vector-metadata quarantine behavior; focused regression
tests now preserve both contracts.
