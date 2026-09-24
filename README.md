# SCS

SCS is a headless code-intelligence service. It indexes source repositories and
lets coding agents investigate them through a local MCP tool. An agent asks
`query_code` a question; SCS returns bounded evidence from its structural and
semantic index.

SCS starts with an empty index. It enrolls a repository only after an explicit
CLI, MCP, or client request, and never changes repository source.

## Requirements

- Stable releases support Apple Silicon macOS and x86-64 Linux with CPython 3.14.
- Indexing needs an embedding provider. The default uses the OpenAI embeddings
  API and sends source-derived entity text to it. Local providers are available.
- Laya is an **optional, local Apple Silicon feature** for choosing query
  playbooks. SCS works without Laya on both supported platforms. See
  [Laya requirements](#optional-laya-routing) for its measured memory use.

## Quick start

Download the installer and checksum manifest from the same
[GitHub Release](https://github.com/leonardoventurini/scs/releases), verify the
installer, and install SCS:

```bash
VERSION=0.2.0
curl -fsSLO "https://github.com/leonardoventurini/scs/releases/download/v${VERSION}/scs-installer-${VERSION}.sh"
curl -fsSLO "https://github.com/leonardoventurini/scs/releases/download/v${VERSION}/SHA256SUMS"
shasum -a 256 -c SHA256SUMS --ignore-missing
sh "scs-installer-${VERSION}.sh"
scs version
```

On Linux, use `sha256sum -c SHA256SUMS --ignore-missing`. The installer
verifies its wheel and constraints, installs without `sudo`, and uses a pinned,
checksum-verified `uv` binary when necessary. Current macOS releases are not
Apple-signed or notarized. See
[distribution and upgrade details](docs/github-releases-distribution.md).

Configure an embedding provider before indexing. For the default OpenAI
provider, put this in `~/.scs/config.toml`:

```toml
embedding_provider = "openai"
embedding_model = "text-embedding-3-large"
embedding_dimension = 3072
openai_api_key = "replace-with-your-key"
```

Keep the file owner-readable only (`chmod 600 ~/.scs/config.toml`). See
[embedding configuration](docs/configuration.md) for local provider and
reranking options.

Register the installed stdio bridge with Codex, then index the repository
containing your current directory:

```bash
codex mcp add scs -- "$HOME/.local/bin/scs" mcp
codex mcp get scs
scs index "$PWD"
scs status
```

If an existing `scs` MCP entry points elsewhere, remove it first with
`codex mcp remove scs`. Restart open Codex clients after changing MCP
configuration. Indexing runs as a durable background job; use `scs status` or
`get_graph_stats` to check when it is ready. The path above assumes the
installer's default `~/.local/bin` location.

## How code queries work

```text
agent goal + repository + optional anchors
                 |
                 v
        validate request and paths
                 |
                 v
     select one of seven playbooks  <--- optional local Laya classifier
                 |
                 v
     bounded index search and graph reads
                 |
                 v
   evidence + routing + trace + completeness
```

For example, an agent can ask:

```text
query_code(
    goal="Find tests affected by changes to the parser",
    repo_path="/repo",
    file_paths=["src/parser.py"],
    mode="balanced",
)
```

The `fast`, `balanced`, and `thorough` modes set fixed time and evidence
budgets. Results show which playbook ran and whether evidence was complete,
truncated, or degraded. See the [MCP tool reference](docs/mcp-tools.md) for
anchors, tool contracts, and the
[`query_code` migration guide](docs/query-code-migration.md) for retired tools.

## Embeddings and indexing

SCS indexes supported source files structurally. Other regular UTF-8 text
files can be indexed at file level for lexical and semantic search. Git ignore
rules and size limits apply. Once a repository is enrolled, SCS watches
Git-visible changes and updates its index in the background. See
[indexing and project management](docs/indexing.md) for coverage, limits,
reindexing, and deletion.

The default OpenAI embedding provider sends source-derived entity text to
the configured API. SCS does not send whole repository files to a
summarization service. You can instead configure a local OpenAI-compatible
server or an in-process MLX provider. Provider details and trust controls
are in [embedding configuration](docs/configuration.md).

## Optional Laya routing

On Apple Silicon, Laya can choose one of SCS's bounded query playbooks.
SCS runs Laya in its own local MLX worker process; it does not call an
external inference service. Laya receives the goal and explicit anchors,
not repository source, embeddings, or retrieved evidence. SCS performs the
search and graph reads. Without Laya, routing follows deterministic rules.

**Resource example:** On a Mac Studio M3 Ultra, the pinned model bundle
occupied about 807 MB on disk, and a warmed Laya worker measured about
5.2 GB of physical memory footprint on 2026-09-24. This is one observed
measurement, not a fixed minimum; usage can vary by host and workload.
Laya is disabled unless explicitly configured.

To enable it from a source checkout on Apple Silicon:

```bash
uv sync --all-groups --extra laya
uv run --extra laya python scripts/install-laya.py
```

Add `decision_model = "laya"` to `~/.scs/config.toml`, then run
`uv run --extra laya scs daemon restart` from that checkout. The release
installer installs the base tool without the optional Laya dependency.
The installation script downloads and verifies a pinned model bundle;
queries never download a model. A configured daemon reports ready only
after its worker loads and warms. If inference fails during a query, SCS
reports degradation and uses deterministic routing.

## Operations and development

`scs list` shows enrolled projects and their stable numeric IDs.
`scs reingest ID|PATH` forces a full rebuild, and `scs delete ID|PATH`
removes only SCS-owned derived state. `scs doctor` checks daemon health;
`scs metrics --days 7 --json` reports aggregate operations without query
text, source text, file paths, job payloads, or results. See
[indexing and project management](docs/indexing.md) for lifecycle details.

Each MCP client runs a small stdio bridge. Bridges share one lazily started
daemon, which shuts down after the last bridge disconnects. Persistent state
lives under `SCS_HOME`. See [architecture](docs/architecture.md) for storage,
runtime ownership, and legacy-index migration.

For a source checkout:

```bash
just setup
just verify
just eval-search
```

`just setup` syncs dependencies, builds the private native extension, and
installs the repository's pre-commit hook. `just verify` runs strict
Basedpyright checks, Ruff, Python tests with branch coverage, and the Rust
workspace tests. Search and query evaluation guidance lives in
[evals/README.md](evals/README.md).
