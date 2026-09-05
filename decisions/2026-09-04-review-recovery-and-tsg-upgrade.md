# Recover force jobs from their own acknowledgements and adopt TSG v0.2.1

## Context

The review reproduced watcher omissions, blocked shutdown, and force-job recovery
skipping source already indexed by an earlier job. Related TSG review found three
storage/search defects. TSG v0.2.1 was released first at commit
`6aa395c246ab0342342662387dfdffe7dbea0be3`; GitHub CI and release workflow passed,
and the downloaded archive matched its published SHA-256 checksum.

## Decision and rationale

Pin SCS to that published TSG tag and rebuild its native adapter. Keep existing
storage formats and public interfaces. Track dirty-path metadata alongside Git
status so additional edits trigger reconciliation without hashing entire files
on every poll. Stop reading new requests during shutdown, close idle connections,
and allow already-dispatched requests to finish. Treat only connection refusal as
evidence that an existing same-user socket is stale.

For force jobs, use the existing job manifest's acknowledgements as the completion
boundary. Always force the remaining frozen targets. Matching ordinary ingestion
hashes cannot establish whether the current force job ran, so they no longer
reconstruct force acknowledgements. Retain the hash-query helper for source
compatibility, with documentation explaining its narrower content-equality meaning.

## Rejected alternatives and consequences

Reusing matching hashes can silently skip an entire force request. Adding an
attempt identifier to native ingestion records would solve attribution but require
a persisted format change. The existing manifest gives safe recovery without that
migration: a crash after native commit but before queue acknowledgement can repeat
one completed batch. Batches already acknowledged by the job stay skipped.

Closing every connection immediately would discard active responses; cancelling
handlers could outlive background thread operations. Closing idle connections and
draining dispatched work preserves the existing durable-operation boundary.
Shutdown still waits for a genuinely running handler to finish.

Metadata polling can coalesce multiple edits and is not a filesystem journal.
Independent Git submodules require their own enrollment for repeated internal
edits. Source hashes remain authoritative at indexing time. Previously completed
but skipped force jobs need a new force request. Rollback requires reverting the
code and dependency pin; there is no migration to undo.
