---
status: shipped
project: scs
project-root: /Users/leonardo/Repositories/scs
created: 2026-09-21
updated: 2026-09-24
owner: parser-and-indexing
decision:
supersedes:
superseded-by:
implementation:
  commits: [6a00e45, 0f0f237]
  pull-request:
---

# Go ingestion support

## Outcome

SCS discovers every eligible `.go` file and ingests its package, imports,
declared types, interfaces, embedded types, fields, functions, methods,
constants, variables, calls, documentation, signatures, and cyclomatic
complexity through the existing native parsing and indexing pipeline. Go code
is searchable and traversable through the existing graph and MCP contracts
without a migration or new public graph vocabulary.

## Problem and evidence

- The native parser registry is the source of truth for structurally supported
  extensions, but it has no `.go` entry or Go parser.
- Python discovery separately maps supported extensions to `FileEntry.language`;
  without a `.go` mapping, Go files cannot be labeled consistently as `go`.
- `scs-parser` already implements one tree-sitter parser per language and the
  PyO3 boundary exposes registry support without language-specific Python code.
- The existing graph vocabulary can represent Go constructs with `module`,
  `class`, `function`, `method`, `variable`, `constant`, `import`, and
  `type_alias` nodes plus existing structural relationships.
- Go interface satisfaction is implicit and requires type checking across a
  package. A file-local tree-sitter parser cannot prove `implements` edges.
- `tree-sitter-go` 0.25 exposes the same `LANGUAGE` binding shape used by this
  workspace's tree-sitter 0.25 integration. Pin the compatible Cargo release
  selected by `cargo add` and commit the resulting lockfile.

## Scope and non-goals

### In scope

- Treat `.go` as a native structural-parser extension and label it `go` during
  discovery.
- Add a Rust `GoParser` backed by `tree-sitter-go`.
- Extract packages, imports, type declarations, interfaces, struct fields,
  embedded types, functions, methods, constants, variables, calls, doc
  comments, signatures, raw text, source spans, and complexity.
- Accept all `.go` filenames, including `_test.go`, generated files, and files
  under vendored paths, subject only to SCS's existing repository ignore,
  always-skip-directory, size, readability, and pruning policies.
- Verify parser behavior, registry/discovery propagation, native serialization,
  durable ingestion, graph relationships, and searchability.

### Non-goals

- Parse `go.mod`, `go.sum`, `go.work`, assembly, CGo headers, or build metadata.
- Run `go list`, `go/packages`, a Go compiler, or network dependency resolution.
- Evaluate build tags or select a buildable platform-specific file set.
- Infer implicit interface satisfaction or perform type-aware call resolution.
- Add node or relationship enum variants, change MCP inventory, or migrate
  persisted graph data.
- Special-case generated, vendor, or test sources beyond existing generic
  discovery policy.

## Contracts

### Discovery and registry

- `.go` appears in the Rust parser registry's supported extension set, resolves
  to one cached `GoParser`, and maps to the stable language discriminator `go`.
- Python discovery maps `.go` to `go`; it continues to consume the native
  registry for extension eligibility.
- Existing ignore and safety boundaries remain authoritative. “All `.go`
  files” does not override `.gitignore`, `ALWAYS_SKIP_DIRS`, file-size limits,
  unreadable-file handling, or generated-directory pruning.

### Identity and containment

- Every parsed file emits one `file` entity whose name is its repository-relative
  path and whose qualified name is that path without `.go`, with `/` replaced
  by `.`.
- Every file emits one `module` entity for its package declaration. Its stable
  qualified name combines the repository-relative directory and declared
  package name so same-named packages in different directories do not collide.
- File `contains` package; package `contains` top-level declarations and import
  entities; named types `contain` their fields and methods.
- Top-level declaration identities are package-scoped and therefore converge
  across files in the same directory/package. Method identities include the
  normalized receiver base type, independent of pointer syntax or receiver
  variable name.
- Import identities remain file-scoped so repeated imports in separate files
  remain distinct occurrences.

### Entity mapping

| Go construct | SCS node | Required metadata |
|---|---|---|
| `package p` | `module` | package name and directory-qualified identity |
| `import` spec | `import` | path, explicit alias when present, signature |
| struct, interface, or defined named type | `class` | type parameters, signature, docs, bases/embeds |
| alias declaration `type A = B` | `type_alias` | aliased type in signature |
| package function | `function` | parameters, results, type parameters, docs, complexity |
| receiver method | `method` | normalized receiver type, signature, docs, complexity |
| struct/interface field or embedded member | `variable` | parent type and declaration signature |
| `const` name | `constant` | explicit type/value when present |
| package `var` name | `variable` | explicit type/value when present |

- Grouped `type`, `const`, `var`, and `import` declarations emit one entity per
  declared name or import spec.
- Multi-name declarations emit distinct entities and preserve the shared
  declaration signature without inventing values for omitted `iota` entries.
- Anonymous struct/interface literals do not create synthetic named classes.
- Generic declarations preserve type parameters in signatures and normalize
  receiver identities to the declared base type.

### Relationships

- Emit `contains` edges according to the ownership contract above.
- Emit `imports` from the package entity to each import entity.
- Emit `inherits` from a struct or interface to each syntactically embedded
  named type. This represents embedding, not subtype proof.
- Do not emit `implements` from structural coincidence; that relationship is
  reserved for evidence SCS can prove.
- Emit `calls` from functions and methods for direct calls, selector calls, and
  generic function invocations using the existing best-effort name-based call
  contract. Filter Go predeclared built-ins to avoid false graph targets.
- Preserve unresolved edge targets in parser output; the indexing pipeline
  continues to persist only edges whose endpoints resolve under its existing
  rules.

### Documentation, content, and complexity

- Contiguous `//` or `/* */` comments immediately associated with a declaration
  become its docstring after comment markers are removed conservatively.
- Raw text uses the existing major/minor byte limits and UTF-8-safe truncation.
- Complexity starts at 1 for each function or method and increments for Go
  decision points: `if`, `for`, non-default expression/type switch cases,
  `select` communication cases, and short-circuit `&&`/`||` expressions.
- Parsing malformed Go remains best effort and must not panic. The existing
  pipeline rule still applies: an actual parser failure never records a
  successful ingestion hash.

## Design

```text
.go source
   |
   v
discovery.py -- language="go"
   |
   v
native registry -- GoParser -- tree-sitter-go
   |                         |
   |                         +-- entities + unresolved edges
   v
PyO3 JSON contract (GIL released)
   |
   v
IngestionPipeline -- identity/edge resolution -- TSG store -- MCP search
```

Implement `crates/scs-parser/src/parser/go.rs` as a typed tree walk following
the established language parser boundary. Keep Go-specific node-kind matching,
receiver normalization, grouped-declaration expansion, doc extraction, and
built-in filtering inside that module. Extend the shared complexity helper only
with Go decision-node semantics. Register the module and parser centrally, add
the grammar as a workspace dependency, and add the Python language label.

Do not introduce a second Go-specific path in Python or the ingestion pipeline.
The existing native serialization and durable pipeline should carry the new
language once registry and discovery contracts are complete.

## Security and data

- Go source remains untrusted input processed locally by tree-sitter; parsing
  must not execute source, invoke the Go toolchain, load modules, or access the
  network.
- Repository read-only, path containment, ignore, file-size, and GIL-release
  boundaries do not change.
- The new grammar is a production dependency and must be reviewed through its
  resolved Cargo source, license, lockfile delta, and native build behavior.
- Existing stores need no migration. Re-indexing adds Go-derived nodes only
  when `.go` files are present.
- Authentication, authorization, MCP surface, runtime identity, and daemon
  ownership are unchanged.

## Acceptance criteria

- An eligible `.go` file is reported as language `go`, parsed structurally,
  durably ingested, and searchable without text-fallback classification.
- Representative Go covering packages, aliased/blank/dot imports, structs,
  interfaces, embedding, fields, aliases, defined types, generics, functions,
  pointer/value receiver methods, grouped constants/variables, documentation,
  calls, and branches produces the specified entities and relationships.
- Symbols declared in separate files of one package use compatible package
  identities, while same-named packages in different directories remain
  isolated.
- `_test.go`, generated `.go`, and otherwise eligible vendored `.go` fixtures
  are accepted when existing generic policies do not ignore or prune them.
- Go built-ins do not create call edges, implicit interface satisfaction does
  not create `implements` edges, and embedded named types create `inherits`
  edges.
- Malformed and Unicode-bearing Go input does not panic or violate raw-text
  limits.
- Existing supported languages, native JSON contracts, graph enums, MCP
  inventory, and fresh-index behavior remain compatible.

## Test strategy

Design and land tests before or alongside each implementation unit. Use inline
or procedurally generated source; do not add a repository of hard-coded fixture
files.

1. Add Rust parser tests first for each entity mapping, grouped declarations,
   cross-file-compatible identities, relationships, docs, signatures, raw-text
   bounds, Unicode, malformed syntax, built-in filtering, generics, receivers,
   and complexity.
2. Add registry tests proving `.go` support, language mapping, parser caching,
   and preservation of the existing extension inventory.
3. Add Python discovery tests proving the `go` label and the selected inclusion
   behavior for `_test.go`, generated names, and vendor paths under policies
   configured below pruning thresholds.
4. Extend the native contract test to parse Go through `_scs_native`, validate
   the serialized enum/field shape, and retain the existing GIL-release gate.
5. Add an integration test that procedurally creates a multi-file Go repository,
   ingests it through `NativeParser` and the real graph store, then asserts
   persisted nodes, cross-file edges that can resolve, language/hash records,
   and search retrieval.
6. Run the entire Rust parser suite and focused Python discovery/native/pipeline
   tests before the repository-wide gate.

## Risks and recovery

- Package import paths cannot be proven without interpreting `go.mod`; using
  repository directory plus declared package name gives deterministic local
  identity but is not a globally importable Go path.
- Go's implicit interfaces cannot be modeled correctly by syntax alone. The
  parser deliberately omits speculative `implements` edges.
- Selector calls are ambiguous without type information. Preserve the existing
  best-effort terminal-name behavior and avoid pretending it is type-aware.
- Build-constrained files may describe mutually exclusive declarations. SCS
  indexes source structure, not one compiled build, so all eligible files are
  intentionally retained.
- Vendored/generated trees can enlarge ingestion. Existing size and pruning
  controls remain the recovery mechanism; this feature adds no bypass.
- Rollback removes the Go registry/parser/dependency and Python mapping, then
  re-indexes affected repositories. No schema downgrade is required; ordinary
  replacement ingestion removes nodes no longer produced.

## Execution checklist

- [x] Add failing Go parser unit tests and shared complexity tests.
- [x] Add `tree-sitter-go` through Cargo and review the lockfile/license delta.
- [x] Implement `GoParser` entity, identity, documentation, signature, call,
      embedding, and complexity extraction.
- [x] Add failing registry and discovery tests, then register `.go`/`go` in
      Rust and Python.
- [x] Add native-boundary and real-store ingestion/search integration tests.
- [x] Update supported-language documentation if implementation discovery finds
      a maintained inventory outside the parser registry.
- [x] Run `cargo fmt --check` and focused `cargo test -p scs-parser` checks.
- [x] Run focused Python discovery, native-contract, and ingestion tests.
- [x] Run `just verify` and record exact results here before implementation
      sign-off.
- [x] Update lifecycle status and implementation commit metadata after all
      acceptance criteria pass.

## Verification results

Implemented by `6a00e45` with explicit dot-import coverage in `0f0f237`.

### Acceptance criteria

- **Passed:** `.go` is exposed by the native registry, labeled `go` by Python
  discovery, parsed structurally, durably ingested, and found by lexical name
  search without text fallback.
- **Passed:** parser tests cover packages, all import forms, structs,
  interfaces, embedding, fields, aliases, defined and generic types, functions,
  pointer receiver methods, grouped values, docs, direct and selector calls,
  built-in filtering, branches, malformed syntax, and Unicode bounds.
- **Passed:** parser and real-store integration tests prove shared package
  identity across files, directory isolation, and a resolved cross-file call.
- **Passed:** discovery tests accept ordinary, `_test.go`, generated-name, and
  vendor-name files. Existing ignored-directory and pruning policies remain
  unchanged, as required by the discovery contract.
- **Passed:** tests prove embedding creates `inherits`, built-ins create no call
  edge, and syntax-only parsing creates no speculative `implements` edge.
- **Passed:** the unchanged graph enums, MCP inventory, native JSON boundary,
  and all previously supported parsers pass the full repository gate.

### Executed checks

- `cargo fmt --all -- --check` — passed.
- `cargo test -p scs-parser --no-fail-fast` — 92 passed.
- `uv run pytest tests/unit/test_indexing_discovery.py -q` — 13 passed.
- `uv run pytest tests/contract/test_native_contract.py -q` — 6 passed.
- `uv run pytest tests/integration/indexing/test_go_ingestion.py -q` — 1
  passed against the real native graph store.
- Focused combined Python checks — 20 passed.
- `just verify` — passed: Basedpyright reported 0 errors/warnings/notes, Ruff
  passed, 348 Python tests passed with 87.38% coverage, and all Rust workspace
  and doc tests passed (104 unit tests total).
- SCS regression-risk analysis completed without truncation. It found no
  uncovered indexed dependents or test targets beyond the directly exercised
  changed surface.

No external Go toolchain, network module resolution, production environment,
or persisted-store migration check was run because none is part of the
implemented contract. No plan deviation was required.
