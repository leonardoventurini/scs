# Package SCS for redistribution

## Goal and scope

Project: `scs`
Project root: `/Users/leonardo/Repositories/mentagen/scs`

Produce one installable, versioned SCS distribution for end users. The first
supported target is macOS on Apple Silicon, installed with `uv tool install`
or `pipx`, and includes the daemon, public MCP proxy, and private
`_scs_native` extension. `scs service install` remains the only supported
mechanism for registering the paired launchd agents.

This plan deliberately does not promise Linux, Windows, Intel macOS, a GUI
installer, an auto-updater, or a hosted MCP service. Those are distinct
product/runtime contracts and should be added only after their service and
native-wheel acceptance gates exist.

## Evidence and uncertainty

- Risk tier: medium. Redistribution crosses the package/runtime trust boundary
  and can install persistent user services, although SCS state remains under
  `SCS_HOME` and uninstall is intentionally non-destructive.
- The root `pyproject.toml` builds with Hatchling and currently ships Python
  packages only; `scripts/build-native.sh` uses `maturin develop`, which is a
  checkout-local install rather than a redistributable artifact.
- `crates/scs-python/pyproject.toml` can build the extension, but it is a
  separate `scs-native` package with an independently repeated version and a
  different Python floor. That makes an end-user installation of root `scs`
  insufficient.
- The existing service manager writes `ProgramArguments` using the installed
  `scs` executable, so it can serve a tool-isolated install when the generated
  launcher is stable.
- Main uncertainty: OMLX availability and model provisioning are currently
  external to SCS even though `omlx` is the default provider. A release must
  either make that prerequisite explicit or provide an SCS-owned, verified
  setup path; it must not claim offline semantic readiness without it.

## Contracts and decisions

### Release contract

- The single public distribution name is `scs`. It exposes only `scs`; the
  proxy remains an internal implementation package, not a second user install.
- A released wheel contains `scs`, `scs_mcp_proxy`, and exactly one compatible
  `_scs_native` binary. Importing `scs.graph.native` after installation must
  not depend on the source checkout, a compiler, Cargo, or `maturin`.
- Release version, Python constraint, project metadata, changelog section, and
  Python/Rust package metadata derive from one source of truth. A tag and all
  release artifacts carry that same version.
- The initial compatibility matrix is declared and enforced in metadata and CI:
  macOS arm64, the selected CPython ABI(s), and the minimum macOS version.
  Unsupported platforms fail before service registration with an actionable
  message.
- Service configuration is explicit and durable: `service install` validates
  and writes one SCS-owned configuration/environment file, and both generated
  plists reference it. It carries `SCS_HOME`, runtime/log locations, and
  provider settings without relying on the interactive shell environment that
  launchd does not inherit. `doctor` reports the effective configuration.
- `scs service install` is allowed only through an absolute, resolved installed
  console launcher; it records launcher, package version, and release identity
  in the service record. Upgrade is `stop -> install new package -> service
  install -> start`; it never deletes `SCS_HOME`.
- Compatibility inspection reads every catalog/project-generation database
  schema directly, without constructing `KnowledgeGraph` or triggering its
  forward migration. A release may migrate only after the inspection and a
  verified SCS_HOME snapshot. Downgrade is unsupported after a schema-advancing
  release and must be refused before service startup; recovery restores the
  snapshot with the prior compatible release.
- A distribution never embeds runtime state, indexes, credentials, model
  weights, or a user-specific path. Release archives contain source and
  license/notice material only.

### Packaging decision

Use a mixed Rust/Python build rooted at the existing Maturin project, with
Maturin producing the public `scs` wheel and including both Python source
trees. First prove the exact layout in a release-candidate branch: root Cargo
manifest, `python-source`/package inclusion for `src/scs` and
`proxy/src/scs_mcp_proxy`, extension placement, and all Rust workspace/path
crates in the sdist. This is the smallest coherent design because the
executable cannot operate without the extension. Keeping Hatchling for Python
and publishing a second native wheel would require a resolver-visible,
platform/ABI-correct dependency relationship and coordinated versioning; it
offers no present user benefit and is easier to install partially.

Keep `scs_mcp_proxy` importable inside the wheel while removing its separate
end-user packaging contract. The proxy process remains independently managed
at runtime; package composition must not be mistaken for process ownership.

## Atomic implementation slices

1. **Make one wheel buildable.** Move the public project metadata and build
   backend to the Maturin-owned package configuration; configure inclusion of
   `src/scs` and `proxy/src/scs_mcp_proxy`, the `scs` console script, package
   data, license, README, classifiers, project URLs, and one version source.
   Files: `pyproject.toml`, `crates/scs-python/pyproject.toml`,
   `Cargo.toml`, package init/version modules as needed. Invariant: a clean
   build produces an sdist and a platform wheel whose metadata both identify
   `scs` at the same version. Pin Python 3.14, macOS arm64, and the minimum
   macOS deployment target consistently in public/native metadata and release
   CI; add a canonical `LICENSE` and dependency-license/SBOM policy. Verify
   with the committed Maturin release build, `twine check dist/*`, wheel/sdist
   contents inspection, `otool -L`, and codesign inspection of the extension.
   Abort if the wheel or sdist omits either Python package, `_scs_native`, or a
   Rust path crate; do not publish split artifacts.

2. **Prove clean-install behavior.** Add a release smoke-test script that
   creates a fresh tool/virtual environment from the built wheel, uses no
   repository path in `PYTHONPATH`, runs `scs --help`, `scs status`, `scs proxy`,
   and imports the native graph module. Files: `scripts/release-smoke-test.sh`,
   `tests/integration/test_distribution_install.py` or an equivalent isolated
   shell test. Invariant: no compiler, Cargo, Maturin, source checkout, or
   editable install is needed after the wheel exists. Verify in a CI runner
   with a temporary `SCS_HOME`. Recover by deleting only the temporary test
   environment and artifacts.

3. **Make lifecycle upgrades explicit.** Add `scs version` (machine-readable
   release/build/protocol/schema data), a read-only all-store compatibility
   inspector, and a preflight used by service install and doctor. Persist the
   validated effective service configuration in an SCS-owned location and pass
   it to both plists; reject a launcher that is not the resolved installed
   console entry point. Files: `src/scs/cli.py`, `src/scs/service.py`,
   `src/scs/config.py`, storage schema APIs, service lifecycle tests. Invariant:
   install, `uv tool`/pipx upgrade, and uninstall preserve configured data
   ownership; an incompatible schema is detected before graph construction or
   mutation. Verify service tests plus a disposable macOS launchd upgrade smoke
   test with non-default SCS_HOME/provider values. Recover with `scs service
   stop`, reinstall the prior compatible wheel, and restore the pre-upgrade
   SCS_HOME snapshot only when diagnosis requires it.

4. **Define the provider prerequisite.** Convert the OMLX dependency into a
   documented and testable release preflight: report whether the configured
   loopback OMLX endpoint and model are ready, and direct the user to the exact
   supported setup when unavailable. Files: `src/scs/config.py`,
   `src/scs/providers/`, `src/scs/cli.py`, `README.md`, `OMLX.md`, tests. The
   daemon may start degraded, but `doctor` must distinguish structural service
   readiness from semantic-provider readiness. Verify positive and unavailable
   provider fixtures. Do not bundle model weights in the wheel.

5. **Automate signed release production.** Add a tag-triggered Forgejo Actions
   workflow using `runs-on: arm64` that runs `just verify`, builds the wheel and
   sdist, executes the clean-install smoke test, emits SHA-256 checksums and an
   SBOM/provenance attestation, signs artifacts, and attaches them to the
   matching release. Files: `.forgejo/workflows/release.yml`, release scripts,
   `CHANGELOG.md`, `README.md`. Invariant: only verified tag builds may be
   published; release credentials are CI secrets and never artifacts. Verify a
   draft/prerelease pipeline before enabling a production package registry.
   Recovery: revoke the signing/publishing credential and yank/delete the
   release artifact while leaving user data untouched.

6. **Publish and document the supported journey.** Add installation, upgrade,
   verification, log location, service removal, data-backup, compatibility,
   and incident-recovery documentation. Files: `README.md`,
   `service/launchd/README.md`, `CHANGELOG.md`. Invariant: a user can install,
   start, verify, upgrade, stop, and uninstall without cloning the repository.
   Verify by following the document in a clean macOS account or VM.

## Risks and recovery

- **A wheel imports but lacks the native extension** → service crashes on first
  graph use → clean-install native-import gate → do not publish; rebuild from
  the tag after correcting the package manifest.
- **A package upgrade starts against a newer/older incompatible store** → data
  availability loss → direct, read-only all-store schema preflight, verified
  pre-migration snapshot, and versioned migration ledger → stop services,
  reinstall the prior compatible release, and restore the snapshot when
  necessary.
- **Launchd starts with defaults instead of user configuration** → data writes
  to the wrong root or unavailable semantic provider → durable validated
  service config passed to both plists and effective-config doctor output →
  stop services, correct/reinstall service config, then restart.
- **A release works only in the checkout** → user cannot start services →
  source-path-denied isolated smoke test → fix launcher/package data and repeat
  release candidate validation.
- **Missing OMLX is presented as ready semantic search** → misleading results
  or failed jobs → distinct doctor/provider state → configure/start OMLX and
  rerun doctor; retain lexical/structural behavior only if explicitly reported.
- **Compromised publishing key or altered artifact** → untrusted code execution
  → CI-scoped credentials, signed checksums, provenance, and signature
  verification → revoke key, yank the release, publish a signed replacement.

## Verification gauntlet

- **Hard gate — artifact completeness:** missing native extension/proxy
  package → inspect wheel contents and `twine check dist/*` → every expected
  module and metadata file exists; failure blocks publication. Sensitivity:
  remove `_scs_native` from a local candidate and confirm the smoke test fails.
- **Hard gate — clean execution:** checkout coupling → isolated installation
  script with source path denied → `scs --help`, native import, and status
  succeed; failure blocks publication.
- **Hard gate — service ownership/configuration:** incorrect launcher,
  environment, or upgrade behavior → disposable launchd integration test with
  non-default SCS_HOME/provider values and an actual tool-install upgrade →
  proxy/daemon become ready and generated plists invoke the recorded installed
  executable with the intended configuration; failure stops services and blocks
  release.
- **Hard gate — behavioral regression:** package changes break service/index
  contracts → `just verify` and `cd proxy && uv run --all-groups pytest -v` →
  both exit zero with committed coverage/performance safeguards intact.
- **Hard gate — supply-chain integrity:** modified or untraceable release →
  verify checksum, signature, SBOM/provenance, tag/version match → every
  artifact verifies before publication.
- **Diagnostic — upgrade compatibility:** representative prior `SCS_HOME` →
  install candidate and run `scs doctor` → migrations and project stores remain
  valid; failure is an abort condition if a supported upgrade path is affected.

## Execution checklist

- [ ] Build one redistributable SCS wheel — files: package manifests and Cargo
  metadata; verify: committed build command plus `twine check dist/*`; done
  when Python, proxy, and native extension share one versioned artifact.
- [ ] Establish clean-install proof — files: release smoke-test script/test;
  verify: isolated wheel install with source path denied; done when CLI and
  native imports work without the checkout or compiler.
- [ ] Preserve service/data contracts through upgrade — files: CLI, service,
  config, storage schema APIs, lifecycle tests; verify: disposable launchd
  upgrade test with non-default configuration; done when SCS_HOME remains
  intact and incompatibility is rejected before startup mutation.
- [ ] Make OMLX operational readiness explicit — files: provider/CLI/docs/tests;
  verify: ready and unavailable provider tests; done when doctor reports the
  correct semantic capability.
- [ ] Automate signed, traceable artifacts — files: release workflow/scripts;
  verify: draft tag release; done when checksums, signature, SBOM/provenance,
  and smoke-test evidence are attached.
- [ ] Document end-user operations — files: README, launchd guide, changelog;
  verify: clean-account walkthrough; done when no repository clone is required.

## Verification and rollout

Start with an internal prerelease on the macOS arm64 compatibility matrix.
Install it into a fresh isolated environment, then exercise
`scs service install`, `start`, `status`, `doctor`, `stop`, and `uninstall`
against a disposable `SCS_HOME`. Repeat with an upgrade from the immediately
previous prerelease before allowing a stable tag.

For a release incident, stop the agents first; never delete user indexes as a
packaging recovery action. Yank the faulty artifact, restore the prior
compatible wheel, re-run `scs service install` and `start`, and use the
documented SCS_HOME backup only if the diagnosed failure requires restoration.
