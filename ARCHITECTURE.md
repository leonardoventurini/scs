# Architecture

The current architecture is documented in [docs/architecture.md](docs/architecture.md).

```text
coding agent -> MCP stdio bridge -> SCSWire Unix socket -> shared daemon -> TSG
```

The daemon starts lazily, reads repository source without changing it, and owns
the durable index. SCS does not expose an MCP HTTP proxy or TCP listener. See
the [MCP tool reference](docs/mcp-tools.md) for the current agent-facing
inventory.
