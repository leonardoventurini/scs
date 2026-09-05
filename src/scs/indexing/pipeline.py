"""Authoritative incremental code-indexing pipeline for SCS."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Coroutine, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

from scs.indexing.discovery import (
    FileEntry,
    IngestionPolicy,
    build_file_entry,
    discover,
)
from scs.graph.models import Edge, NodeType
from scs.indexing.parser.base import LanguageParser, ParsedEdge, ParsedEntity
from scs.indexing.repository_paths import (
    assert_ingestable_repo_path,
    canonicalize_repo_path,
)
from scs.providers.base import EmbeddingProvider, ProviderUnavailableError

logger = logging.getLogger(__name__)

RAW_TEXT_LIMIT = 2_048
PARSE_WORKERS = 4
EMBED_BATCH_SIZE = 128
INGESTION_BATCH_MAX_FILES = 32
INGESTION_BATCH_MAX_ENTITIES = 512
T = TypeVar("T")


class GraphStore(Protocol):
    """Persistence operations required by indexing, suitable for native fakes."""

    def get_or_create_repo_sync(self, path: str) -> int: ...
    def resolve_repo_id_sync(self, path: str) -> int | None: ...
    def get_all_ingested_files_sync(self, repo_path: str) -> dict[str, str]: ...
    def get_file_paths_for_repo_sync(self, repo_path: str) -> list[str]: ...
    def get_node_ids_for_file_sync(
        self, repo_path: str, rel_path: str
    ) -> list[str]: ...
    def get_file_paths_with_missing_embeddings_sync(
        self, *, repo_id: int
    ) -> set[str]: ...
    def resolve_node_id_by_qualified_name_sync(
        self, repo_path: str, qualified_name: str
    ) -> str | None: ...
    def batch_upsert_nodes_sync(self, nodes: list[dict[str, object]]) -> int: ...
    def batch_upsert_edges_sync(self, edges: list[dict[str, object]]) -> int: ...
    def get_edges_sync(
        self, node_id: str, *, direction: str = "both"
    ) -> list[Edge]: ...
    def batch_upsert_embeddings_sync(
        self, embeddings: list[tuple[str, list[float]]]
    ) -> int: ...
    def flush_vector_index_sync(self) -> bool: ...
    def reopened_vectors_contain_sync(self, node_ids: list[str]) -> bool: ...
    def reopened_vectors_absent_sync(self, node_ids: list[str]) -> bool: ...
    def delete_nodes_sync(self, node_ids: list[str]) -> int: ...
    def remove_file_graph_and_vector_sync(
        self, repo_path: str, rel_path: str
    ) -> int: ...
    def delete_ingestion_records_batch_sync(
        self, repo_path: str, rel_paths: list[str]
    ) -> int: ...
    def delete_ingested_file_sync(self, repo_path: str, rel_path: str) -> int: ...
    def acknowledge_ingested_files_batch_sync(
        self, records: list[dict[str, object]]
    ) -> None: ...
    def get_ingestion_stats_sync(self) -> dict[str, dict[str, object]]: ...


@dataclass(frozen=True, slots=True)
class IngestionProgress:
    """One transport-independent progress event."""

    phase: str
    current: int
    total: int
    file_path: str = ""
    message: str = ""


@dataclass(slots=True)
class IngestionResult:
    """Durable outcome of one explicit indexing operation."""

    files_discovered: int = 0
    files_changed: int = 0
    files_deleted: int = 0
    files_failed: int = 0
    entities_created: int = 0
    edges_created: int = 0
    edges_dropped: int = 0
    embeddings_created: int = 0
    semantic_degraded_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _ParsedFile:
    entry: FileEntry
    entities: list[ParsedEntity]
    edges: list[ParsedEdge]


@dataclass(frozen=True, slots=True)
class _StructuralPlan:
    """The complete structural replacement prepared before batch enrichment.

    Edges are planned across every successfully parsed changed file before the
    first embedding batch runs.  This deliberately decouples graph integrity
    from the bounded semantic acknowledgement boundary.
    """

    parsed: tuple[_ParsedFile, ...]
    nodes: list[dict[str, object]]
    node_ids: dict[str, str]
    entity_node_ids: dict[tuple[str, int], str]
    replaced_ids: set[str]
    edges: list[dict[str, object]]


@dataclass(frozen=True, slots=True)
class _IngestionBatch:
    """One deterministic, complete-file semantic acknowledgement unit."""

    files: tuple[_ParsedFile, ...]

    @property
    def entity_count(self) -> int:
        """Return the number of provider inputs retained by this batch."""

        return sum(len(item.entities) for item in self.files)


def _run(coroutine: Coroutine[object, object, T]) -> T:
    """Run provider work from the pipeline's dedicated background thread."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    raise RuntimeError("IngestionPipeline must run off the event-loop thread")


def _node_id(
    repo_path: str, rel_path: str, entity: ParsedEntity, occurrence: int = 0
) -> str:
    """Preserve legacy symbol IDs while distinguishing repeated occurrences.

    Ordinals are local to a file/kind/qualified-name group, so unrelated symbols
    and whitespace changes do not change identities. A separate prefix keeps
    occurrence identities distinct from canonical absolute-repository identities.
    """

    identity = f"{repo_path}:{rel_path}:{entity.kind.value}:{entity.qualified_name}"
    if occurrence:
        identity = f"occurrence:{occurrence}:{identity}"
    return hashlib.sha256(identity.encode()).hexdigest()[:32]


def _file_record_id(repo_path: str, rel_path: str) -> str:
    return hashlib.sha256(f"file:{repo_path}:{rel_path}".encode()).hexdigest()[:32]


class IngestionPipeline:
    """Discover, parse, structurally persist, enrich, and then commit hashes."""

    def __init__(
        self,
        *,
        graph: GraphStore,
        parser: LanguageParser,
        embeddings: EmbeddingProvider | None = None,
        progress: Callable[[IngestionProgress], None] | None = None,
        policy: IngestionPolicy | None = None,
    ) -> None:
        self._graph: GraphStore = graph
        self._parser: LanguageParser = parser
        self._embeddings: EmbeddingProvider | None = embeddings
        self._progress: Callable[[IngestionProgress], None] = (
            progress or self._ignore_progress
        )
        self._policy: IngestionPolicy = policy or IngestionPolicy()

    @staticmethod
    def _ignore_progress(_progress: IngestionProgress) -> None:
        """Provide a typed no-op when no progress observer is configured."""

    def _report(
        self, phase: str, current: int, total: int, *, path: str = "", message: str = ""
    ) -> None:
        self._progress(IngestionProgress(phase, current, total, path, message))

    def create_force_full_snapshot(self, repo_path: Path) -> list[dict[str, object]]:
        """Freeze the current discoverable file set for one force-full job.

        The returned records deliberately contain only path, hash, language,
        and byte size.  They are safe to persist in the jobs queue and let a
        retry reject source drift instead of silently indexing newer bytes
        under an older force attempt.
        """

        canonical = canonicalize_repo_path(repo_path)
        assert_ingestable_repo_path(self._graph, canonical)
        return [
            {
                "rel_path": entry.rel_path,
                "content_hash": entry.content_hash,
                "language": entry.language,
                "byte_size": entry.byte_size,
            }
            for entry in discover(
                Path(canonical),
                self._parser.supported_extensions(),
                policy=self._policy,
            )
        ]

    def acknowledged_force_snapshot_paths(
        self, repo_path: Path, snapshot: Sequence[Mapping[str, object]]
    ) -> list[str]:
        """Return snapshot paths matching the current ingestion hashes.

        These matches describe content equality only. They must not be used as
        force-job completion evidence because an earlier index can have the same
        hashes before the force job starts. Retained for internal compatibility;
        durable force recovery uses only its job manifest acknowledgements.
        """

        canonical = canonicalize_repo_path(repo_path)
        hashes = self._graph.get_all_ingested_files_sync(canonical)
        return sorted(
            rel_path
            for record in snapshot
            if isinstance((rel_path := record.get("rel_path")), str)
            and isinstance((content_hash := record.get("content_hash")), str)
            and hashes.get(rel_path) == content_hash
        )

    def ingest(
        self,
        repo_path: Path,
        *,
        force: bool = False,
        force_snapshot: Sequence[Mapping[str, object]] | None = None,
        on_force_batch_acknowledged: Callable[[list[str]], None] | None = None,
    ) -> IngestionResult:
        """Index an entire repository after an explicit caller request.

        A force snapshot restricts this execution to its frozen targets.  Its
        files must still have their frozen hashes before parsing, so a retry
        can never acknowledge a newer edit as part of an earlier force run.
        """

        canonical = canonicalize_repo_path(repo_path)
        assert_ingestable_repo_path(self._graph, canonical)
        if force_snapshot is None:
            entries = discover(
                Path(canonical),
                self._parser.supported_extensions(),
                policy=self._policy,
            )
            sweep_deleted = True
        else:
            entries = self._entries_from_force_snapshot(Path(canonical), force_snapshot)
            # A snapshot's path set is immutable.  A retry must not delete a
            # file merely because it was created after that force attempt.
            sweep_deleted = False
        return self._ingest_entries(
            canonical,
            entries,
            force=force,
            sweep_deleted=sweep_deleted,
            on_batch_acknowledged=on_force_batch_acknowledged,
        )

    def ingest_files(
        self,
        repo_path: Path,
        file_paths: Sequence[Path],
        deleted_paths: Sequence[str] = (),
    ) -> IngestionResult:
        """Index explicitly changed files while applying explicit deletions."""

        canonical = canonicalize_repo_path(repo_path)
        assert_ingestable_repo_path(self._graph, canonical)
        extensions = self._parser.supported_extensions()
        entries = [
            entry
            for path in file_paths
            if (
                entry := build_file_entry(
                    path, Path(canonical), extensions, policy=self._policy
                )
            )
            is not None
        ]
        result = self._ingest_entries(
            canonical, entries, force=False, sweep_deleted=False
        )
        result.files_deleted += self._delete_paths(canonical, deleted_paths)
        return result

    def cleanup_stale_files(self, repo_path: Path) -> IngestionResult:
        """Delete indexed files no longer discoverable under current ignore rules."""

        canonical = canonicalize_repo_path(repo_path)
        discovered = {
            entry.rel_path
            for entry in discover(
                Path(canonical),
                self._parser.supported_extensions(),
                policy=self._policy,
            )
        }
        indexed = set(self._graph.get_file_paths_for_repo_sync(canonical))
        result = IngestionResult(files_discovered=len(discovered))
        result.files_deleted = self._delete_paths(canonical, indexed - discovered)
        return result

    def _ingest_entries(
        self,
        repo_path: str,
        entries: list[FileEntry],
        *,
        force: bool,
        sweep_deleted: bool,
        on_batch_acknowledged: Callable[[list[str]], None] | None = None,
    ) -> IngestionResult:
        result = IngestionResult(files_discovered=len(entries))
        repo_id = self._graph.get_or_create_repo_sync(repo_path)
        hashes = self._graph.get_all_ingested_files_sync(repo_path)
        missing_embedding_paths: set[str] = (
            self._graph.get_file_paths_with_missing_embeddings_sync(repo_id=repo_id)
            if self._embeddings is not None
            else set()
        )
        changed = [
            entry
            for entry in entries
            if force
            or hashes.get(entry.rel_path) != entry.content_hash
            or entry.rel_path in missing_embedding_paths
        ]
        result.files_changed = len(changed)

        if sweep_deleted:
            discovered_paths = {entry.rel_path for entry in entries}
            indexed_paths = set(
                self._graph.get_file_paths_for_repo_sync(repo_path)
            ) | set(hashes)
            result.files_deleted += self._delete_paths(
                repo_path, indexed_paths - discovered_paths
            )

        if not changed:
            return result

        # A discovered hash is a content contract, not merely a change hint.
        # Reading and hashing before parsing ensures a racing writer cannot
        # make us install structure for bytes we later acknowledge differently.
        self._report("parse", 0, len(changed), message="Parsing changed source files")
        parsed, failed = self._parse_files(changed)
        result.files_failed = len(failed)
        for index, item in enumerate(parsed, start=1):
            self._report("parse", index, len(changed), path=item.entry.rel_path)

        plan = self._build_structural_plan(repo_path, repo_id, parsed)
        retained_inbound_edges = self._retained_inbound_edges(
            plan.replaced_ids, plan.node_ids
        )
        # Replacing the file subgraph, rather than merging nodes in place,
        # guarantees removed calls/imports cannot survive an incremental run.
        # Native foreign keys cascade every incident edge during deletion.
        if plan.replaced_ids:
            self._graph.delete_nodes_sync(sorted(plan.replaced_ids))
        if plan.nodes:
            result.entities_created = self._graph.batch_upsert_nodes_sync(plan.nodes)
        committed_edges = [*plan.edges, *retained_inbound_edges]
        result.edges_created = (
            self._graph.batch_upsert_edges_sync(committed_edges)
            if committed_edges
            else 0
        )
        result.edges_dropped = sum(len(item.edges) for item in parsed) - len(plan.edges)

        batches = self._plan_batches(plan.parsed)
        for batch_number, batch in enumerate(batches, start=1):
            self._report(
                "embed",
                batch_number,
                len(batches),
                path=batch.files[0].entry.rel_path,
                message=(
                    f"Embedding and acknowledging complete-file batch {batch_number}"
                ),
            )
            if not self._embed_batch(batch, plan.entity_node_ids, result):
                result.files_failed += len(batch.files)
                break
            self._graph.flush_vector_index_sync()
            batch_node_ids = [
                plan.entity_node_ids[(item.entry.rel_path, position)]
                for item in batch.files
                for position in range(len(item.entities))
            ]
            if (
                self._embeddings is not None
                and batch_node_ids
                and not self._graph.reopened_vectors_contain_sync(batch_node_ids)
            ):
                result.files_failed += len(batch.files)
                result.semantic_degraded_reason = (
                    "Reopened vector sidecar is missing an acknowledged batch vector"
                )
                break
            if not self._batch_sources_are_stable(batch):
                result.files_failed += len(batch.files)
                result.semantic_degraded_reason = (
                    "Source changed while its embedding batch was in progress"
                )
                break
            # This graph primitive performs a single SQLite transaction.  It
            # is intentionally *after* the vector sidecar flush: an ingested
            # hash is the retry checkpoint proving semantic durability.
            self._graph.acknowledge_ingested_files_batch_sync(
                self._ingestion_records(repo_path, batch)
            )
            if on_batch_acknowledged is not None:
                on_batch_acknowledged([item.entry.rel_path for item in batch.files])
        return result

    def _entries_from_force_snapshot(
        self,
        repo_path: Path,
        snapshot: Sequence[Mapping[str, object]],
    ) -> list[FileEntry]:
        """Rebuild frozen entries and reject every absent or changed target."""

        extensions = self._parser.supported_extensions()
        entries: list[FileEntry] = []
        expected_paths: set[str] = set()
        for record in snapshot:
            rel_path = record.get("rel_path")
            content_hash = record.get("content_hash")
            language = record.get("language")
            byte_size = record.get("byte_size")
            if (
                not isinstance(rel_path, str)
                or not rel_path
                or Path(rel_path).is_absolute()
                or ".." in Path(rel_path).parts
                or not isinstance(content_hash, str)
                or not isinstance(language, str)
                or not isinstance(byte_size, int)
                or rel_path in expected_paths
            ):
                raise ValueError("Invalid force-full snapshot record")
            expected_paths.add(rel_path)
            entry = build_file_entry(
                repo_path / rel_path, repo_path, extensions, policy=self._policy
            )
            if entry is None:
                raise RuntimeError(
                    f"Force-full snapshot source is no longer ingestable: {rel_path}"
                )
            if (
                entry.content_hash != content_hash
                or entry.language != language
                or entry.byte_size != byte_size
            ):
                raise RuntimeError(f"Force-full snapshot source changed: {rel_path}")
            entries.append(entry)
        return sorted(entries, key=lambda entry: entry.rel_path)

    def _delete_paths(self, repo_path: str, paths: Iterable[str]) -> int:
        """Finalize deletions only after each bounded sidecar flush succeeds."""

        deleted_paths = sorted(set(paths))
        for offset in range(0, len(deleted_paths), INGESTION_BATCH_MAX_FILES):
            deleted_batch = deleted_paths[offset : offset + INGESTION_BATCH_MAX_FILES]
            removed_node_ids = [
                node_id
                for rel_path in deleted_batch
                for node_id in self._graph.get_node_ids_for_file_sync(
                    repo_path, rel_path
                )
            ]
            for rel_path in deleted_batch:
                self._graph.remove_file_graph_and_vector_sync(repo_path, rel_path)
            self._graph.flush_vector_index_sync()
            if removed_node_ids and not self._graph.reopened_vectors_absent_sync(
                removed_node_ids
            ):
                raise RuntimeError(
                    "Reopened vector sidecar retains a deleted file's vector"
                )
            self._graph.delete_ingestion_records_batch_sync(repo_path, deleted_batch)
        return len(deleted_paths)

    def _parse_files(
        self, entries: list[FileEntry]
    ) -> tuple[list[_ParsedFile], list[FileEntry]]:
        def parse(entry: FileEntry) -> _ParsedFile:
            source_bytes = entry.abs_path.read_bytes()
            if hashlib.sha256(source_bytes).hexdigest() != entry.content_hash:
                raise RuntimeError("source changed after discovery")
            source = source_bytes.decode("utf-8", errors="replace")
            if entry.language == "text":
                entities = [
                    ParsedEntity(
                        kind=NodeType.FILE,
                        name=entry.rel_path,
                        qualified_name=entry.rel_path,
                        start_line=0,
                        end_line=source.count("\n"),
                        docstring=source,
                        raw_text=source,
                    )
                ]
                edges: list[ParsedEdge] = []
            else:
                entities, edges = self._parser.parse(source, entry.rel_path)
            return _ParsedFile(entry, entities, edges)

        parsed: list[_ParsedFile] = []
        failed: list[FileEntry] = []
        with ThreadPoolExecutor(max_workers=PARSE_WORKERS) as executor:
            futures = [(entry, executor.submit(parse, entry)) for entry in entries]
            for entry, future in futures:
                try:
                    parsed.append(future.result())
                except Exception:
                    failed.append(entry)
                    logger.exception("Failed to parse %s", entry.rel_path)
        return parsed, failed

    def _build_structural_plan(
        self,
        repo_path: str,
        repo_id: int,
        parsed: list[_ParsedFile],
    ) -> _StructuralPlan:
        nodes: list[dict[str, object]] = []
        qualified_to_id: dict[str, str] = {}
        entity_node_ids: dict[tuple[str, int], str] = {}
        replaced_ids: set[str] = set()
        for item in parsed:
            occurrences: dict[tuple[NodeType, str], int] = {}
            for position, entity in enumerate(item.entities):
                identity = (entity.kind, entity.qualified_name)
                occurrence = occurrences.get(identity, 0)
                occurrences[identity] = occurrence + 1
                node_id = _node_id(repo_path, item.entry.rel_path, entity, occurrence)
                qualified_to_id[entity.qualified_name] = node_id
                entity_node_ids[(item.entry.rel_path, position)] = node_id
                metadata: dict[str, object] = {
                    "file_path": item.entry.rel_path,
                    "language": item.entry.language,
                    "qualified_name": entity.qualified_name,
                    "start_line": entity.start_line,
                    "end_line": entity.end_line,
                }
                for key, value in (
                    ("signature", entity.signature),
                    ("docstring", entity.docstring),
                    ("bases", entity.bases),
                    ("imports", entity.imports),
                    ("calls", entity.calls),
                    ("cyclomatic_complexity", entity.cyclomatic_complexity),
                ):
                    if value not in (None, "", []):
                        metadata[key] = value
                nodes.append(
                    {
                        "id": node_id,
                        "type": entity.kind.value,
                        "name": entity.name,
                        "content": entity.raw_text[:RAW_TEXT_LIMIT],
                        "metadata": metadata,
                        "repo_id": repo_id,
                    }
                )
            replaced_ids.update(
                self._graph.get_node_ids_for_file_sync(repo_path, item.entry.rel_path)
            )
        edges = self._prepare_edges(repo_path, parsed, qualified_to_id)
        return _StructuralPlan(
            parsed=tuple(sorted(parsed, key=lambda item: item.entry.rel_path)),
            nodes=nodes,
            node_ids=qualified_to_id,
            entity_node_ids=entity_node_ids,
            replaced_ids=replaced_ids,
            edges=edges,
        )

    def _prepare_edges(
        self,
        repo_path: str,
        parsed: list[_ParsedFile],
        qualified_to_id: dict[str, str],
    ) -> list[dict[str, object]]:
        edges: list[dict[str, object]] = []
        seen: set[tuple[str, str, str]] = set()
        retained_ids: dict[str, str | None] = {}

        def resolve(qualified_name: str) -> str | None:
            """Resolve unchanged symbols once, including negative lookups."""

            changed_id = qualified_to_id.get(qualified_name)
            if changed_id is not None:
                return changed_id
            if qualified_name not in retained_ids:
                retained_ids[qualified_name] = (
                    self._graph.resolve_node_id_by_qualified_name_sync(
                        repo_path, qualified_name
                    )
                )
            return retained_ids[qualified_name]

        for item in parsed:
            for edge in item.edges:
                # A changed endpoint is resolved from the immutable structural
                # plan; an unchanged endpoint is resolved from the retained,
                # repository-scoped graph.  This preserves changed-file edges
                # that target a file outside the current structural plan.
                source = resolve(edge.source_qualified_name)
                target = resolve(edge.target_qualified_name)
                key = (source or "", target or "", edge.relationship.value)
                if source and target and key not in seen:
                    seen.add(key)
                    edges.append(
                        {
                            "source_id": source,
                            "target_id": target,
                            "relationship": edge.relationship.value,
                            "weight": edge.weight,
                        }
                    )
        return edges

    def _retained_inbound_edges(
        self, replaced_ids: set[str], node_ids: dict[str, str]
    ) -> list[dict[str, object]]:
        """Preserve edges owned by an already-acknowledged source file.

        Replacing a target node cascades its incoming rows.  A partial batch
        retry does not reparse earlier acknowledged source files, so preserve
        those inbound relationships before deletion and bind them to the
        deterministic replacement node IDs.
        """

        replacement_ids = set(node_ids.values())
        retained: dict[tuple[str, str, str], dict[str, object]] = {}
        for target_id in sorted(replaced_ids):
            for edge in self._graph.get_edges_sync(target_id, direction="incoming"):
                if (
                    edge.source_id in replaced_ids
                    or edge.target_id not in replacement_ids
                ):
                    continue
                key = (edge.source_id, edge.target_id, edge.relationship.value)
                retained[key] = {
                    "source_id": edge.source_id,
                    "target_id": edge.target_id,
                    "relationship": edge.relationship.value,
                    "weight": edge.weight,
                }
        return list(retained.values())

    @staticmethod
    def _plan_batches(parsed: tuple[_ParsedFile, ...]) -> list[_IngestionBatch]:
        """Partition a sorted structural plan without ever splitting a file."""

        batches: list[_IngestionBatch] = []
        current: list[_ParsedFile] = []
        current_entities = 0
        for item in parsed:
            entities = len(item.entities)
            exceeds_files = len(current) >= INGESTION_BATCH_MAX_FILES
            exceeds_entities = (
                current and current_entities + entities > INGESTION_BATCH_MAX_ENTITIES
            )
            if exceeds_files or exceeds_entities:
                batches.append(_IngestionBatch(tuple(current)))
                current = []
                current_entities = 0
            current.append(item)
            current_entities += entities
        if current:
            batches.append(_IngestionBatch(tuple(current)))
        return batches

    @staticmethod
    def _batch_sources_are_stable(batch: _IngestionBatch) -> bool:
        """Require acknowledgement to match the exact bytes that were parsed."""

        for item in batch.files:
            try:
                digest = hashlib.sha256(item.entry.abs_path.read_bytes()).hexdigest()
            except OSError:
                return False
            if digest != item.entry.content_hash:
                return False
        return True

    @staticmethod
    def _ingestion_records(
        repo_path: str, batch: _IngestionBatch
    ) -> list[dict[str, object]]:
        """Serialize a complete batch for atomic native acknowledgement."""

        return [
            {
                "file_id": _file_record_id(repo_path, item.entry.rel_path),
                "repo_path": repo_path,
                "rel_path": item.entry.rel_path,
                "language": item.entry.language,
                "content_hash": item.entry.content_hash,
                "byte_size": item.entry.byte_size,
            }
            for item in batch.files
        ]

    def _embed_batch(
        self,
        batch: _IngestionBatch,
        entity_node_ids: dict[tuple[str, int], str],
        result: IngestionResult,
    ) -> bool:
        if self._embeddings is None:
            # Structural-only deployments still need durable source hashes;
            # readiness is separately derived from provider metadata by routes.
            return True
        entities = [entity for item in batch.files for entity in item.entities]
        texts = [
            entity.embed_text() for item in batch.files for entity in item.entities
        ]
        try:
            vectors: list[list[float]] = []
            for offset in range(0, len(texts), EMBED_BATCH_SIZE):
                vectors.extend(
                    _run(
                        self._embeddings.embed_documents(
                            texts[offset : offset + EMBED_BATCH_SIZE]
                        )
                    )
                )
            if len(vectors) != len(entities):
                raise ProviderUnavailableError(
                    "Embedding provider returned a vector count that does not match inputs"
                )
        except (ProviderUnavailableError, OSError, RuntimeError) as exc:
            result.semantic_degraded_reason = str(exc)
            return False
        pairs = [
            (
                entity_node_ids[(item.entry.rel_path, position)],
                vector,
            )
            for (item, position), vector in zip(
                (
                    (item, position)
                    for item in batch.files
                    for position in range(len(item.entities))
                ),
                vectors,
                strict=True,
            )
        ]
        result.embeddings_created = (
            self._graph.batch_upsert_embeddings_sync(pairs) if pairs else 0
        )
        # A file can validly produce no embeddable entities. Its structural
        # acknowledgement must still complete rather than retry indefinitely.
        return True
