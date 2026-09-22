---
status: implemented
project: scs
project-root: /Users/leonardo/Repositories/scs
created: 2026-09-22
updated: 2026-09-22
owner: parser-and-indexing
decision: decisions/2026-09-22-use-openai-compatible-local-inference.md
supersedes:
superseded-by:
implementation:
  commits: [023f8d9]
  pull-request:
---

# Bound embedding requests

## Outcome

SCS converts every parsed entity into useful, bounded semantic text, sends
embedding inputs in bounded ordered requests, and reports a safe provider error
detail when the endpoint rejects a request.

## Problem and evidence

The Go parser currently stores an entire `var_spec` as a variable signature.
For `OperationRegistry` in an observed repository, a composite map initializer
produced a 279,937-character signature and a 280,017-character provider input.
The configured OpenAI-compatible facade correctly rejected that request because
it exceeded its 131,072-character request limit, but SCS retained only the
generic HTTP 422 reason.

Raw entity text already has parser-owned limits. Signatures do not have the same
shared invariant, and the HTTP provider currently partitions work only by item
count rather than aggregate text size.

## Scope and non-goals

### In scope

- Extract compact Go variable and constant declaration signatures without
  embedding arbitrarily large composite initializer bodies.
- Bound signature-bearing entity embedding text consistently at the Rust and
  Python parser contracts.
- Partition OpenAI-compatible embedding requests by both input count and total
  input characters while preserving order.
- Include a bounded, text-only response detail in HTTP provider failures.
- Add regression coverage for large Go composite declarations, Unicode-safe
  truncation, ordered character-aware batching, single-input rejection, and
  bounded error details.

### Non-goals

- Change MES limits, model identity, endpoint ownership, vector dimensions, or
  provider configuration.
- Change public MCP, SCSWire, CLI, or persisted graph/job formats.
- Split one parsed entity into multiple vectors or silently omit an entity.
- Log or retain complete provider response bodies.

## Contracts

- A Go variable or constant signature describes its declaration shape. A
  composite initializer may contribute only bounded semantic context; it may
  never make the signature proportional to the initializer body.
- Every signature inserted into embedding text is capped by one shared semantic
  signature limit in the producing language boundary. Truncation preserves valid
  UTF-8 and stable prefixes.
- OpenAI-compatible embedding requests preserve input and response order. Each
  request contains at most the configured item batch size and at most the
  provider request character budget.
- A single prefixed input that exceeds the request budget fails locally with a
  typed provider-unavailable error; it is never sent partially or silently
  truncated by the transport.
- HTTP status failures retain the status and a bounded plain-text or JSON detail.
  Credentials, headers, arbitrary binary data, and unbounded bodies remain
  excluded.
- Python and Rust node and relationship enum contracts remain unchanged.

## Design

```text
Go var/const AST
      |
      v
compact declaration signature
      |
      v
bounded ParsedEntity.embed_text()
      |
      v
count + character batch planner
      |
      v
OpenAI-compatible endpoint
      |
      +-- success -> ordered vectors
      |
      +-- failure -> status + bounded safe detail
```

The Go parser should use named AST fields to retain the declared name and type,
plus only a bounded value representation when useful. The shared entity
contract provides defense in depth for every language. The provider owns the
transport budget because aggregate request sizing is independent of parser
correctness.

## Security and data

Repository source remains local and read-only. Authentication, authorization,
network trust, and persisted data formats do not change. Provider error bodies
are untrusted input: decode them conservatively, normalize them to bounded text,
and never include request headers or credentials.

## Acceptance criteria

- The observed `OperationRegistry` declaration produces compact bounded
  embedding text and no longer causes an HTTP 422 request-size rejection.
- Large signatures in any supported entity kind cannot produce unbounded
  embedding input, including Unicode-bearing signatures.
- Multiple valid inputs are partitioned before either the item-count or
  character budget is exceeded, and returned vectors retain source order.
- An individually oversized prefixed input fails before transport with a clear
  bounded error.
- HTTP failures expose MES's `embedding input is too large` detail without
  retaining an unbounded response body.
- All existing parser, ingestion, provider, strict-type, and native tests remain
  compatible.

## Risks and recovery

Compact signatures intentionally remove low-value initializer detail from
semantic input. Search remains anchored by entity name, qualified name, type,
and bounded declaration context. An overly small request budget could increase
HTTP calls; keep it close to the serving contract and below the hard endpoint
limit. Rollback is a normal code revert followed by incremental semantic repair;
there is no schema or data migration.

## Execution checklist

- [x] Add regression tests that reproduce the large Go composite declaration.
- [x] Correct Go variable and constant signature extraction.
- [x] Add matching Rust and Python semantic signature bounds.
- [x] Add count-and-character-aware provider batching.
- [x] Preserve bounded HTTP response details.
- [x] Run focused Rust and Python checks, then `just verify`.
- [x] Record acceptance evidence and implementation commits.

## Verification results

Implemented by `023f8d9`.

### Acceptance criteria

- **Passed:** the rebuilt native parser converts the observed
  `OperationRegistry` signature to
  `OperationRegistry = map[OperationID]*OpMeta{…}`. Its prefixed embedding input
  fell from 280,017 to 126 characters, and the live loopback MES endpoint
  returned HTTP 200 with 4,096 vector components.
- **Passed:** Rust and Python cap function, method, variable, and constant
  signatures at 512 UTF-8 bytes without splitting a code point.
- **Passed:** provider tests prove count-and-character partitioning preserves
  source/vector order and every request remains at or below 120,000 characters.
- **Passed:** an individually oversized input fails before transport, while HTTP
  error handling reads at most 1,025 bytes and exposes a bounded detail.
- **Passed:** public contracts, persisted formats, model identity, vector
  dimension, and MES configuration remain unchanged.

### Executed checks

- `uv run pytest tests/unit/test_parser_base.py tests/unit/test_providers.py -q`
  — 32 passed.
- `cargo test -p scs-parser --no-fail-fast` — 96 passed.
- Focused Ruff and Basedpyright checks — passed with zero findings.
- `just verify` — passed: Basedpyright and Ruff reported zero findings; 363
  Python tests passed with 87.02% coverage; all Rust workspace and doc tests
  passed, including 108 unit tests.
- Exact `OperationRegistry` native parse plus live MES embedding request — HTTP
  200 and 4,096 dimensions.

No production deployment, daemon restart, automatic queue drain, persisted
store migration, or external-network inference check was performed. None is
required by this source-level compatibility fix.
