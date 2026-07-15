# SCS

SCS is the headless Semantic Code System extracted from External product. It indexes
source repositories into a structural and vector-backed code graph and exposes
that intelligence through a local control socket and MCP.

SCS starts with an empty index. It does not migrate, inspect, or recreate any
legacy External product graph data. Repositories are added only through an explicit CLI,
MCP, or client request.

The service has no graphical interface. Use `scs status`, `scs doctor`, logs,
SCSWire, or MCP diagnostics for operational visibility.

## Development

```bash
just setup
just verify
```

`just setup` installs Python dependencies and builds the private `_scs_native`
extension. The daemon can then be run directly with `scs serve`, while explicit
repository enrollment uses `scs index <repo>` or `scs reindex <repo>`.

Install and operate the independent user services with:

```bash
scs service install
scs service start
scs service status
scs service restart
scs service stop
scs service uninstall
```

Uninstall removes only service registrations and runtime ownership. It
preserves `SCS_HOME` and every SCS-owned index. SCS never reads External product's legacy
index and never enrolls repositories merely because External product knows about them.
