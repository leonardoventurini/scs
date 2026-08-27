# Set up and start the local SCS service

## Goal and scope

Prepare this checkout's supported development environment and run both of its
macOS user services: the public MCP proxy and private SCS daemon. This rollout
does not index a repository, change source, alter service topology, or delete
SCS data.

## Evidence and uncertainty

- Project: `scs`
- Project root: `/Users/leonardo/Repositories/mentagen/scs`
- `README.md` defines `just setup` as the supported bootstrap command and
  `scs service install` followed by `scs service start` as the service path.
- Preflight on 2026-08-26 found neither launchd agent registered and daemon
  health unavailable. `uv run` successfully resolved the Python environment.
- Risk tier: medium, because installation writes per-user LaunchAgent and log
  artifacts and starts persistent local processes. Main uncertainty: the
  native extension build and launchd child processes may fail after setup.

## Contracts and decisions

- The proxy is installed and started before the daemon, as enforced by
  `ServiceManager.start`.
- Persistent state is retained only in `SCS_HOME`; the service operation must
  not index source or access legacy External product data.
- Success means both launchd agents are loaded and `scs doctor` reports a
  ready daemon. No code change is expected, so no decision record is needed.

## Risks and recovery

- Bootstrap/build failure: do not install or start services; report the
  failing command and preserve diagnostics.
- Service startup failure: inspect SCS logs and `scs status`; recover with
  `scs service stop` and then `scs service uninstall` if the user requests
  removal of the registration. Neither action deletes `SCS_HOME`.
- Wrong service ownership: verify both labels and daemon health before
  declaring success.

## Verification gauntlet

- **Hard gate — supported environment:** `just setup` exits zero.
- **Hard gate — service registration:** `scs service status` reports both
  `proxy_loaded` and `daemon_loaded` true after start.
- **Hard gate — daemon availability:** `scs doctor` exits zero and reports
  `daemon.available` and `daemon.ready` true.
- **Diagnostic — runtime ownership:** inspect the runtime service records and
  confirm proxy/daemon artifacts exist under the documented runtime directory.

## Execution checklist

- [x] Bootstrap dependencies and native extension — files: `.venv`, native
  build outputs; verify: `just setup`; done when the command exits zero.
- [x] Install and start the paired user services — files:
  `~/Library/LaunchAgents/com.mentagen.scs.{proxy,daemon}.plist`; verify:
  `uv run --all-groups scs service install && uv run --all-groups scs service start`;
  done when both launchd agents are loaded.
- [x] Prove daemon readiness and correct ownership — files:
  `~/Library/Application Support/SCS/{mcp.json,proxy-service.json,scs.sock,daemon-service.json}`;
  verify: `uv run --all-groups scs doctor`; done when health is ready and
  service records are present.

## Verification and rollout

Run the hard gates in checklist order. If a service gate fails, stop it before
leaving the rollout incomplete; full removal is available through
`scs service uninstall` and preserves the data root.
