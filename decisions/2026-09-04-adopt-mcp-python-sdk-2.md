# Adopt MCP Python SDK 2

Project: `scs`

Project root: `/Users/leonardo/Repositories/mentagen/scs`

## Context

The first tagged release's clean-wheel smoke test resolved the unconstrained
`mcp>=1.25` dependency to MCP 2.1.1. SCS still used the MCP 1.x `FastMCP`
surface, so the installed command could not import even though development
tests passed against the older lockfile. No GitHub Release was published.

## Decision

SCS requires MCP Python SDK 2.1.1 or newer and implements its MCP host with
`MCPServer`. Internal direct-dispatch tests use MCP 2's typed
`CallToolResult`, and protocol tests use the SDK's snake-case Python fields.
Expected input-validation failures are translated to `ToolError` so clients
receive actionable messages; unexpected exceptions retain MCP 2's sanitized
error behavior.

The failed `v0.1.0` tag remains immutable and unpublished. The corrected
`v0.1.1` tag is the first candidate for a published GitHub Release.

## Rejected alternatives

- Pinning `mcp<2` would make the existing implementation importable but leave
  SCS on the superseded SDK line and contradict the dependency freshness goal.
- Moving or deleting `v0.1.0` would obscure the failed release attempt and
  violate the repository's immutable-tag release policy.
- Allowing all tool exceptions to expose their original messages would weaken
  MCP 2's protection against accidental internal-data disclosure.

## Consequences

- Clean installations resolve an SDK version that matches the implemented API.
- SCS follows MCP 2 result, annotation, and error contracts.
- Downstream Python code calling `build_mcp()` directly must consume
  `CallToolResult`; the agent-facing protocol inventory and tool schemas remain
  unchanged.
- Future release validation must continue installing the built wheel in a fresh
  tool environment so lockfile-only compatibility cannot mask dependency drift.
