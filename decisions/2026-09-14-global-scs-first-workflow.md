# Use SCS first in codebase workflows

Status: accepted

Project: `scs`

Project root: `/Users/leonardo/Repositories/scs`

Root ID: `d870ba89`

## Context

SCS provides repository-scoped semantic search, structural graph queries,
references, file inspection, regression-risk analysis, and durable indexing.
Without shared workflow guidance, agents can miss this intelligence, overuse
broad filesystem searches, or mistake queued ingestion for a ready index.

## Decision

Install an implicitly discoverable global `scs-workflow` skill for all codebase
work. It uses SCS first to narrow the relevant symbols, files, and relationships,
then verifies findings in current source and runs normal repository tests.

The workflow automatically queues one full ingestion when the repository is
absent or stale and checks readiness separately. It degrades to lexical SCS
results or targeted local tools when semantic search or SCS is unavailable.
Repository deletion always requires an explicit user request naming the target.

## Rejected alternatives

- Explicit-only invocation would make SCS easy to omit during ordinary work.
- Requiring approval for every ingestion would add friction even though SCS
  changes only its derived index and never repository source.
- Using SCS exclusively would make work fragile when the service is unavailable
  and would confuse indexed evidence with authoritative current source.
- Treating SCS and filesystem discovery equally would not establish a useful
  default and would preserve broad, repetitive exploration.

## Rationale

SCS has the highest leverage early, where semantic intent and graph relationships
can reduce the search space. Exact reads, Git, type checks, and tests remain the
correct authorities for edits and verification. Bounded automatic enrollment
keeps that discovery layer available without making it a hard dependency.

## Consequences

New Codex turns can select the skill automatically for codebase tasks. SCS may
perform derived-index writes without a separate prompt, but it cannot mutate
repository source. Agents must scope every query to the active repository,
distinguish queued work from readiness, and report meaningful fallbacks. Removing
`~/.agents/skills/scs-workflow` fully rolls back the behavior change.
