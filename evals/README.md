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
