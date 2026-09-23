# SCS search evaluation suites

Search suites use strict, versioned JSON:

~~~json
{
  "schema_version": 1,
  "name": "repository-name",
  "cases": [
    {
      "query": "natural-language code question",
      "relevant": [
        {
          "qualified_name": "package.module.Symbol.method",
          "file_path": "src/package/module.py",
          "relevance": 3
        }
      ]
    }
  ]
}
~~~

The qualified name is required. The file path is optional but should be
included when the same qualified name occurs in more than one indexed source
path. Relevance is a positive integer; larger values indicate more useful
results.

Run the bundled SCS suite against the current checkout:

~~~bash
just eval-search
~~~

Override the repository, suite, cutoff, or number of repeated requests:

~~~bash
just eval-search evals/scs-search-v1.json /path/to/scs 10 3
~~~

The JSON report includes per-query Recall@k, reciprocal rank, nDCG@k, response
bytes, mean request latency, retrieval mode, graph statistics, and reranker
identity. Model-backed timings are intentionally observational rather than CI
gates. Commit representative suites for each evaluated repository and compare
reports produced with the same suite version and cutoff. The runner waits for
active indexing jobs to finish before collecting measurements.

## Unified query judgments

`scs-query-v2.json` is the current seven-playbook suite. It keeps the v1 goals,
anchors, and legacy call sequences, but expands file-level relevance judgments
after auditing the top candidates from both paths against this checkout's
source. V1 and its reports remain historical and must not be compared
numerically with v2 reports.

Grades are source-backed: 3 is the direct implementation or primary test, 2 is
an entry point, direct reference, or focused supporting test, and 1 is useful
context or a valid Python function file. The IMPACT case includes all three
tests that directly import `build_mcp` from the changed file. The REFERENCES
case includes files that instantiate or import `SCSDaemon`; unrelated
`DaemonController` matches are unjudged. The INVENTORY case judges Python
function files from the pooled top results; Rust function files remain
unjudged because the goal specifies Python.

The suite contains positive judgments, not an exhaustive list of every useful
file. `unsupported_rate` therefore means absent from this judgment set, not
proven incorrect. The file-level metric does not measure symbol-level relevance
or whether a returned function has the requested language. Keep those limits in
mind when reviewing the retirement gate.
