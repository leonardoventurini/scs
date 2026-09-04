# Architecture

SCS has five ownership layers:

1. Native Rust parsers and graph persistence.
2. Python indexing orchestration and provider ports.
3. SCSWire for typed local product clients.
4. Streamable HTTP MCP for coding agents.
5. A stable public MCP proxy that survives daemon replacement.

MCP tools call SCS services directly through the typed service gateway.

## Process and artifact boundaries

The proxy owns public port `28463`, `mcp.json`, and `proxy-service.json`. The
daemon owns internal port `28465`, `scs.sock`, and `daemon-service.json`.
Service records use `{service, pid, start_time, generation, artifact_sha256,
protocol_min, protocol_max}` and are durably written by atomic replacement.
Cleanup requires matching service and generation, so neither process removes
the other's artifacts.

SCSWire startup uses `lstat`. It refuses symlinks, non-sockets, foreign-UID
sockets, and every successfully connected live peer. Only a same-UID socket
whose connection is refused is reclaimed as stale. A protocol health exchange
classifies a verified SCS service without risking a live foreign listener.

## Data and source boundary

There is no legacy migration or automatic repository enrollment. `SCS_HOME`
starts empty and creates state only beneath its configured storage root.
Repository files are read-only inputs; parsing, Git provenance, search,
inspection, composites, and LSP reads persist only in SCS-owned storage.

SCSWire uses four-byte big-endian length-prefixed JSON frames capped at 16 MiB.
The proxy independently caps HTTP bodies at 10 MiB and streams responses in
bounded chunks.

## Performance contract

Committed regression ceilings are 300 MiB pre-embedding daemon RSS, two-second
warmed query p95, and ten seconds for the 100-file structural index fixture.
Cross-process performance claims require direct measurements from the affected
processes; SCS benchmarks cover only resources and latency owned by SCS.

The supported protocol range begins at version 1. Clients and daemons advertise
minimum/maximum versions and capabilities. Unknown fields are ignored; a
non-overlapping range returns a typed compatibility error.
