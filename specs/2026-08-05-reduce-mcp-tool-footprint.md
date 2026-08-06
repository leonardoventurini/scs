# Reduce the MCP tool footprint to ten essential operations

## Goal and scope

Replace the inherited 27-tool MCP catalog with ten distinct, agent-oriented
operations while preserving SCS's read-only repository boundary and explicit,
background indexing model. This is one direct public-contract rollout: retire
redundant or misleading MCP adapters, delete backing service routes that have
no non-retired caller, strengthen the retained contracts, update exact
inventory documentation, and verify the daemon/proxy boundary end to end.

The retained tools are:

- `search_code`
- `graph_context`
- `get_related`
- `list_symbols`
- `inspect_file`
- `find_references`
- `regression_risk_report`
- `ingest_project`
- `ingest_files`
- `get_graph_stats`

The rollout does not change repository source, persisted graph schemas,
embedding representations, daemon ownership, or proxy behavior.

## Evidence and uncertainty

- `src/scs/mcp/inventory.py` exposes 27 tools whose serialized discovery
  definitions occupy 12,137 characters. The proposed ten existing definitions
  occupy 4,951 characters, a 59.2% reduction.
- All 27 tools arrived together during the External product extraction in commit
  `304f952`; history contains no per-tool demand evidence.
- Repository and sibling-client searches found no hard-coded consumers outside
  SCS tests. The local daemon is stopped, and `ToolRecorder` retains only a
  process-local 2,000-event window, so long-term production usage is unknown.
- `search_knowledge` and `search_code` share `knowledge.search`; the former's
  `data_scope` is ignored. Several other tools are weaker projections of kept
  routes.
- `test_coverage_map` is a bounded graph heuristic, not measured coverage;
  `consistency_check` does not compare neighboring conventions;
  `find_symbol` ignores its file filter; positional routes ignore `column`.
- Current agent-tool guidance favors clear, distinct operations and
  evaluation-driven consolidation:
  <https://www.anthropic.com/engineering/writing-tools-for-agents>.
- Current MCP guidance recommends accurate read-only, destructive, and
  open-world annotations:
  <https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/>.

The main uncertainty is an unknown external hard-coded client. Exact tool and
SCSWire inventory tests plus a breaking release note make the change explicit;
recovery is a normal commit revert. Stop and revisit scope if a local client or
test reveals a non-MCP dependency on a route selected for deletion.

## Contracts and decisions

- `MOVED_TO_SCS_TOOLS` equals the ten-tool set exactly. The other 35 former
  External product tools remain explicitly classified in `RETIRED_TOOLS`, preserving the
  45-tool disposition invariant.
- Retired MCP names fail as unknown tools; compatibility aliases are not kept,
  because aliases preserve the ambiguity and schema cost being removed.
- `get_related` accepts exactly one of `symbol_name` or `node_id`, supports
  optional repository scoping, and returns the resolved seed nodes plus bounded
  traversal results. Exact ID lookup replaces `get_node_detail`; incoming
  traversal replaces `contract_check`.
- `find_references` selects the narrowest indexed symbol containing the supplied
  zero-based line. The unused `column` argument is removed because parser
  entities persist line ranges, not column ranges; adding a false column
  contract would be worse than exposing the real capability.
- `regression_risk_report` requires `repo_path`; paths without an explicit
  repository previously produced an empty report that looked successful.
- `get_graph_stats` becomes the sole model-facing readiness/status operation and
  supports repository scoping. CLI status/doctor and internal health routes
  remain available for operators.
- Every retained tool publishes an explicit top-level output schema so clients
  can inspect decisive response fields instead of an unconstrained object.
- Query tools are annotated read-only, non-destructive, idempotent, and
  closed-world. Ingestion tools are closed-world and carry
  `destructiveHint=true`: incremental deletion and full-project stale-file
  reconciliation can remove SCS-owned index state even though repository
  source remains immutable.
- `ingest_git_history` is removed as a standalone synchronous MCP operation.
  Existing provenance data remains readable; no persisted data is deleted.
- Service routes used only by retired tools are removed from the public daemon
  router and implementation. Shared primitives and CLI/system routes remain.

## Risks and recovery

- **Hard-coded client calls a retired name → unknown-tool failure.** Detect with
  exact inventory/E2E tests and release notes. Recover by reverting the rollout
  commit; no data migration is involved.
- **Retirement removes unique code intelligence → degraded agent tasks.** Keep
  every distinct retrieval, traversal, exhaustive listing, file inspection,
  exact reference, impact, indexing, and readiness workflow. Verify each
  retained adapter maps to the intended service route.
- **Consolidated lookup becomes ambiguous → wrong-repository result.** Require
  exactly one lookup selector and apply `repo_path` to symbol-name resolution.
- **Position lookup implies unsupported precision → wrong expectations.** Remove
  the unused column input and verify narrowest line-range selection.
- **Tool annotations overstate safety → inappropriate client approval policy.**
  Assert exact annotation tuples. Mark indexing destructive because it can
  reconcile away stale index state, independently of source immutability.
- **Dead route removal breaks a non-MCP caller → SCSWire compatibility failure.**
  Search local clients before deletion and run the full integration/isolation
  suite. Restore the specific route or revert if a real consumer is found.

## Verification gauntlet

- **Hard gate — exact public inventory:** a contract test asserts precisely ten
  names, 35 retired names, disjointness, and total 45. Before implementation it
  fails against the 27-tool catalog; after implementation it must pass.
- **Hard gate — adapter completeness:** parametrized FastMCP tests invoke or
  inspect every retained tool and assert its route, normalized arguments, and
  annotations. Exact output-schema field tests reject unconstrained result
  objects. No retained tool may exist only in the allowlist.
- **Hard gate — retired names unavailable:** representative duplicate,
  misleading-analysis, diagnostic, and provenance names fail through FastMCP,
  while `tools/list` exposes none of the 35 retired names.
- **Hard gate — strengthened behavior:** targeted route tests prove exact-one-of
  lookup validation, repository scoping, narrowest line selection, required
  regression-risk repository scope, and repository-scoped stats.
- **Hard gate — boundary preservation:** MCP security, daemon E2E, service-route
  inventory, repository read-only isolation, and proxy tests pass.
- **Hard gate — repository quality:** `just verify` and
  `cd proxy && uv run --all-groups pytest -v` pass with no type, lint, Python,
  Rust, or proxy failures.
- **Diagnostic metric — discovery footprint:** serialize `tools/list`; ten tools
  and a material reduction from the 12,137-character baseline are expected.

## Execution checklist

- [x] Freeze the ten-tool contract and red tests — files:
  `tests/contract/test_mcp_inventory.py`, `tests/integration/test_mcp_server.py`;
  verify: targeted pytest; done when failures identify the old 27-tool surface.
- [x] Strengthen retained route behavior — files: `src/scs/mcp/server.py`,
  `src/scs/services/routes.py`, targeted route tests; verify: targeted pytest;
  done when selector, scope, position, stats, and annotation contracts pass.
- [x] Retire adapters and dead routes — files: `src/scs/mcp/inventory.py`,
  `src/scs/mcp/server.py`, `src/scs/main.py`, `src/scs/services/routes.py`, route
  inventory tests; verify: contract, MCP, service, and isolation suites.
- [x] Record the public decision — files: `README.md`, `CHANGELOG.md`,
  `decisions/2026-08-05-reduce-mcp-tool-footprint.md`; verify: exact inventory
  and documentation inspection.
- [x] Complete independent review and integration gate — verify: targeted
  sensitivity evidence, `just verify`, proxy tests, `git diff --check`; done
  when review has no unresolved blocking finding and the worktree contains only
  task-owned changes.

## Verification and rollout

Ship the inventory reduction, behavior strengthening, route cleanup, tests,
documentation, and decision together. The daemon and proxy discover the new
catalog on restart. There is no database migration or destructive cleanup.
Rollback is `git revert <rollout-commit>` followed by the normal service
restart; persisted indexes remain compatible in both directions.

The implemented ten-tool discovery document is 10,999 serialized characters,
9.4% below the 12,137-character baseline after adding exact safety annotations
and explicit output schemas that the old catalog did not provide. Tool count is
down 63.0%, from 27 to 10. The Python suite contains 142 tests and reports
86.08% branch-aware coverage after the dead-route removal.
