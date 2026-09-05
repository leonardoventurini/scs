# Share source identity validation across ingestion and MCP

## Context

Version 0.1.6 stores separate identities for internal source symlinks, but MCP
input validation still forwarded their resolved targets. Incremental service
requests and indexed regression/reference lookups repeated that normalization.
The native index can therefore hold correct identities that tools cannot address.

## Decision

Move lexical repository-path normalization into a small shared source-path module
and use it from discovery and validated tool/service inputs. Check resolved target
existence and containment independently; return the lexical source path. Preserve
checkout-root alias handling, strict file checks, and invalid/escaping target
rejection. Risk lookups retain their existing ability to address removed files.
The MCP helper delegates to shared validation; services do not import MCP code.

Generated tests cover all four affected MCP forwards, durable incremental-job
payloads, actual native index inspection/risk/reference identities, and invalid
source targets. Wire/MCP signatures and persisted schemas remain unchanged.

## Alternatives and consequences

Fixing only MCP leaves backend normalization lossy. Copying normalization code
would permit ingestion/read contracts to drift again. Resolving aliases in the
index instead would undo established separate source identities. Shared validation
keeps read and write identities aligned without changing repository contents.

Publish as 0.1.7 independently, then install after the current 0.1.6 force
ingestion completes; do not interrupt that job merely to repair tool addressing. Existing indexed data needs no rebuild.
Rollback restores earlier tool behavior without a schema migration. End-to-end
released MCP validation remains the final rollout check.
