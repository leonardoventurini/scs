# Architecture

SCS has four ownership layers:

1. Native Rust parsers and graph persistence.
2. Python indexing orchestration and provider ports.
3. SCSWire for typed local product clients.
4. Streamable HTTP MCP for coding agents.

External product is downstream of SCSWire and is never part of the SCS runtime. MCP tools
call SCS services directly rather than routing through External product.

The supported protocol range begins at version 1. Clients and daemons advertise
minimum/maximum versions and capabilities. Unknown fields are ignored; a
non-overlapping range returns a typed compatibility error.

