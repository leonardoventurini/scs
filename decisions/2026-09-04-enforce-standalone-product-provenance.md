# Enforce Standalone Product Provenance

Project: `scs`

Project root: `/Users/leonardo/Repositories/mentagen/scs`

## Context

SCS's runtime, tests, documentation, and history retained assumptions and
provenance from an external product. This contradicted the intended standalone
boundary and made unrelated storage names, environment variables, tools, and
release procedures part of SCS's conceptual surface.

## Decision

SCS models only SCS-owned contracts. Runtime settings recognize SCS inputs;
storage is canonicalized beneath `SCS_HOME`; MCP publishes a direct ten-tool
allowlist; startup creates no index until an explicit request; and repository
source remains read-only. Cross-repository compatibility scripts and historical
extraction narratives are removed.

The repository's branches and tags are rewritten so both current and historical
objects satisfy this boundary. Future provenance, compatibility, or migration
work must be expressed in generic SCS terms unless another product becomes an
explicit supported integration through a separately approved public contract.

## Rejected alternatives

- Documentation-only wording changes would leave runtime environment and path
  coupling in place.
- Keeping historical references would continue exposing the old conceptual
  boundary through clones, search, archives, and tag browsing.
- Maintaining compatibility aliases would turn unsupported external state into
  an indefinite SCS contract.

## Rationale

A standalone engine is easier to reason about when its code, tests, and history
describe only the behavior it owns. Exact SCS-native contracts preserve the
useful safety properties without encoding knowledge of unrelated products.

## Consequences

Existing clones must re-clone or reset to rewritten refs. Earlier release
assets remain installable but their recorded source commit identities predate
the rewrite. `SCS_HOME` selection is now entirely the operator's responsibility;
SCS canonicalizes and owns the selected root but does not reserve names or
inspect markers belonging to other applications.
