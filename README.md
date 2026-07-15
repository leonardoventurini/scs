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

The runtime and installation commands will be added with the daemon boundary:
`scs service install|start|stop|restart|status|uninstall`.

