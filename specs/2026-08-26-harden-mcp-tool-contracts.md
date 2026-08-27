# Harden the SCS MCP tool contracts

## Goal and scope

Correct the two live MCP defects found in the tool evaluation and implement the
five evidence-led usability improvements in one compatible rollout. The public
inventory remains exactly ten tools and SCS continues to read repository source
without mutating it.

## Evidence and uncertainty

- Project: `scs`
- Project root: `/Users/leonardo/Repositories/mentagen/scs`
- A live `find_references` call resolves successfully through SCSWire but fails
  MCP output validation because mutually exclusive omitted TypedDict fields
  become invalid non-null defaults in the generated schema.
- `regression_risk_report` includes file-to-symbol `contains` edges and
  affected nodes in dependent results, yielding a false self-dependent.
- A live `graph_context` query produced valid import seeds but no context under
  default outgoing-only traversal. A 59-node, 116-edge file inspection produced
  an unbounded public response. Semantic store availability can coexist with
  zero indexed embeddings.
- Risk tier: medium because the MCP output schema and tool payloads are public
  local contracts. Main uncertainty: clients may rely on today's unbounded
  inspection output or ambiguous entity-name resolution.

## Contracts and decisions

- `find_references` returns a discriminated `available` union. Available
  results contain a symbol and list; unavailable results contain
  location/reason/configuration state. This avoids optional-property schema
  defaults while making each valid shape explicit to MCP clients.
- Regression dependents exclude every affected node and only retain dependency
  relationships (`calls`, `imports`, `references`, `inherits`, and
  `implements`); `contains` expresses ownership, not blast radius.
- `get_related(symbol_name=...)` resolves only symbol node types. Exact node-ID
  lookup remains type-agnostic, preserving direct graph access.
- `graph_context` accepts an explicit `direction` (`incoming`, `outgoing`, or
  `both`) and defaults to `both` because context assembly needs surrounding
  ownership and usage information. Each result states the direction that
  produced it.
- Stats adds `semantic_search_ready`, true only when the vector store is usable
  and indexed embeddings/vector entries exist.
- `inspect_file` accepts bounded `node_limit` and `edge_limit`, defaults to
  documented safe values, caps each to a named maximum, and returns explicit
  truncation booleans. The limits constrain only MCP response payloads, never
  stored graph data.
- Adapter path validation maps missing/unreadable paths and out-of-scope paths
  into stable `ValueError` messages before routing.

## Risks and recovery

- Schema change rejects a valid client payload → exact schema/transport tests
  validate both reference variants. Recover by reverting the commit and
  restarting SCS; persisted indexes are unaffected.
- Dependency filtering hides a true dependent → focused service-route graph
  fixtures retain an external `references` edge while rejecting containment and
  self nodes.
- Response bounds hide decisive file details → return truncation flags and allow
  callers to request bounded larger limits. Recover by increasing a request
  limit within the explicit maximum.
- Traversal broadening inflates context → preserve hop bounds and deduplicate
  node IDs; tests assert contexts are bounded and contain the intended parent.
- launchd teardown race strands the locally configured endpoint → after a failed
  kickstart, poll teardown for a bounded two seconds and bootstrap only once
  registration disappears; a lifecycle test simulates the exit-37 race.

## Verification gauntlet

- **Hard gate — usable references:** live MCP transport tests make both
  available and unavailable reference calls with no output validation error.
- **Hard gate — trustworthy risk:** a graph fixture proves a changed file and
  its containment edges are excluded while an external reference remains.
- **Hard gate — bounded contract:** inspect output signals truncation and never
  exceeds requested/default caps; schema asserts new input/output fields.
- **Hard gate — semantic clarity:** stats distinguishes an available empty
  vector store from semantic-search readiness.
- **Hard gate — integration:** targeted MCP/service tests, `just verify`, proxy
  tests, live daemon/MCP calls, and a source-boundary Git check pass.
- **Hard gate — service restart:** a teardown-race lifecycle test proves a
  failed kickstart with an absent registration is bootstrapped, and the local
  daemon is healthy after the real restart.

## Execution checklist

- [x] Define hardened public contracts and API decision — files:
  `src/scs/mcp/contracts.py`, `specs/...`, `decisions/...`; verify: contract
  schema tests; done when reference variants, stats readiness, and bounded
  inspection types are explicit.
- [x] Fix service-route semantics — files: `src/scs/services/routes.py`; verify:
  focused service-route tests; done when risk, symbol, traversal, and response
  bounds satisfy the stated invariants.
- [x] Normalize MCP adapters and test true transport behavior — files:
  `src/scs/mcp/server.py`, `src/scs/mcp/paths.py`, MCP tests; verify: available
  and unavailable streamable-HTTP reference calls; done when neither errors.
- [x] Run the full verification and live rollout — files: running local SCS
  service; verify: `just verify`, proxy tests, live MCP calls, `git diff --check`;
  done when all hard gates pass and source is unchanged.

## Verification and rollout

Commit code, tests, the specification, and decision record together. Restart
the paired local services after verification so the configured Codex MCP client
loads the hardened contract. Rollback is `git revert <commit>` followed by
`scs service restart`; no data migration or repository mutation is involved.
