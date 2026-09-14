# Global SCS workflow skill

Project: `scs`

Project root: `/Users/leonardo/Repositories/scs`

Root ID: `d870ba89`

## Problem

Codex can access SCS code intelligence, but no global workflow explains when to
index a repository, which SCS query best fits a task, how to combine semantic
results with exact source inspection, or how to degrade when SCS is unavailable.

## Evidence and uncertainty

- SCS exposes eleven MCP tools: seven read-only code-intelligence queries, two
  index-ingestion operations, one index deletion operation, and graph stats.
- Indexing is queued durable work. An accepted ingestion response does not mean
  the repository is immediately ready.
- SCS never mutates repository source. Ingestion and deletion affect only its
  derived index and catalog state.
- This Codex session does not expose the configured SCS MCP, so the workflow
  must be validated structurally rather than exercised through live tool calls.

## Contracts

- Create an automatically discoverable global skill at
  `~/.agents/skills/scs-workflow` for all codebase work.
- Prefer repository-scoped SCS discovery before broad filesystem exploration.
- Automatically queue full ingestion when a repository is absent or stale, then
  verify readiness separately with graph stats.
- Use targeted filesystem reads, exact search, Git, and tests after SCS narrows
  the relevant surface.
- Route semantic lookup, graph context, traversal, symbol inventory, file
  inspection, references, and regression-risk analysis to their matching tools.
- Never use repository deletion as routine maintenance; require an explicit user
  request naming the repository.
- Bound readiness polling and fall back transparently when SCS or semantic search
  is unavailable. Never block ordinary code work on SCS.
- Preserve user authorization and all repository-specific instructions.

## Risks and recovery

The main risk is overusing ingestion or treating stale search results as current
source. The workflow queues at most one necessary refresh, checks readiness, and
requires direct source verification before edits. A misleading global workflow
could affect every repository; recovery is removal or revert of the one skill
directory. No SCS data or repository source needs migration.

## Executable checklist

- [x] Define structural assertions for metadata, tool coverage, safety rules,
      readiness handling, and targeted fallback behavior.
- [x] Create the skill and implicit-invocation metadata with the bundled skill
      initializer.
- [x] Write concise routing and lifecycle guidance grounded in public SCS
      contracts.
- [x] Run the skill validator and structural assertions.
- [x] Review the final artifact for scope, source safety, and context economy.
- [x] Record the workflow decision and commit only task-owned paths.

## Direct rollout

Install the skill directly in the requested global skill directory. It becomes
available to new Codex turns through normal implicit skill discovery. No SCS
daemon restart, index deletion, or repository reindex is part of installation.

## Verification

- `quick_validate.py` must accept the global skill.
- YAML parsing must confirm implicit invocation and SCS MCP dependency metadata.
- A structural check must find every current MCP tool name and the explicit
  deletion guard in the instructions.
- Manual review must confirm the skill distinguishes queued ingestion from
  readiness and treats direct source as authoritative before mutation.

## Verification results

- The bundled `quick_validate.py` reported `Skill is valid!`.
- Structural assertions found all eleven current MCP tool names, implicit
  invocation, the SCS MCP dependency, queued-versus-ready guidance, the
  deletion guard, direct-source verification, and fallback behavior.
- The installed `scs` 0.1.14 command reported its daemon ready, and
  `codex mcp get scs` reported the enabled stdio bridge at
  `/Users/leonardo/.local/bin/scs mcp`.
- Live MCP calls were not available in this already-open tool session. The next
  Codex turn can discover the new skill and use the configured MCP normally.
