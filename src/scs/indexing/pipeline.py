"""Authoritative incremental code-indexing pipeline for SCS."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar, cast

from scs.graph.models import NodeType
from scs.indexing.discovery import FileEntry, build_file_entry, discover
from scs.indexing.parser.base import LanguageParser, ParsedEdge, ParsedEntity
from scs.indexing.repository_paths import (
    assert_ingestable_repo_path,
    canonicalize_repo_path,
)
from scs.providers.base import (
    EmbeddingProvider,
    FileSummarizer,
    ProviderUnavailableError,
)

logger = logging.getLogger(__name__)

RAW_TEXT_LIMIT = 2_048
PARSE_WORKERS = 4
EMBED_BATCH_SIZE = 128
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
    def batch_upsert_nodes_sync(self, nodes: list[dict[str, object]]) -> int: ...
    def batch_upsert_edges_sync(self, edges: list[dict[str, object]]) -> int: ...
    def batch_upsert_embeddings_sync(
        self, embeddings: list[tuple[str, list[float]]]
    ) -> int: ...
    def flush_vector_index_sync(self) -> bool: ...
    def delete_node_sync(self, node_id: str) -> bool: ...
    def delete_ingested_file_sync(self, repo_path: str, rel_path: str) -> int: ...
    def delete_ingestion_record_sync(self, repo_path: str, rel_path: str) -> bool: ...
    def upsert_ingested_file_sync(self, **kwargs: object) -> None: ...
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
    summaries_generated: int = 0
    semantic_degraded_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _ParsedFile:
    entry: FileEntry
    entities: list[ParsedEntity]
    edges: list[ParsedEdge]


def _run(coroutine: Coroutine[object, object, T]) -> T:
    """Run provider work from the pipeline's dedicated background thread."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    raise RuntimeError("IngestionPipeline must run off the event-loop thread")


def _node_id(repo_path: str, rel_path: str, entity: ParsedEntity) -> str:
    identity = f"{repo_path}:{rel_path}:{entity.kind.value}:{entity.qualified_name}"
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
        summarizer: FileSummarizer | None = None,
        progress: Callable[[IngestionProgress], None] | None = None,
    ) -> None:
        self._graph: GraphStore = graph
        self._parser: LanguageParser = parser
        self._embeddings: EmbeddingProvider | None = embeddings
        self._summarizer: FileSummarizer | None = summarizer
        self._progress: Callable[[IngestionProgress], None] = (
            progress or self._ignore_progress
        )

    @staticmethod
    def _ignore_progress(_progress: IngestionProgress) -> None:
        """Provide a typed no-op when no progress observer is configured."""

    def _report(
        self, phase: str, current: int, total: int, *, path: str = "", message: str = ""
    ) -> None:
        self._progress(IngestionProgress(phase, current, total, path, message))

    def ingest(self, repo_path: Path, *, force: bool = False) -> IngestionResult:
        """Index an entire repository after an explicit caller request."""

        canonical = canonicalize_repo_path(repo_path)
        assert_ingestable_repo_path(self._graph, canonical)
        extensions = self._parser.supported_extensions()
        entries = discover(Path(canonical), extensions)
        return self._ingest_entries(canonical, entries, force=force, sweep_deleted=True)

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
            if (entry := build_file_entry(path, Path(canonical), extensions))
            is not None
        ]
        result = self._ingest_entries(
            canonical, entries, force=False, sweep_deleted=False
        )
        for rel_path in sorted(set(deleted_paths)):
            self._graph.delete_ingested_file_sync(canonical, rel_path)
            result.files_deleted += 1
        return result

    def cleanup_stale_files(self, repo_path: Path) -> IngestionResult:
        """Delete indexed files no longer discoverable under current ignore rules."""

        canonical = canonicalize_repo_path(repo_path)
        discovered = {
            entry.rel_path
            for entry in discover(Path(canonical), self._parser.supported_extensions())
        }
        indexed = set(self._graph.get_file_paths_for_repo_sync(canonical))
        result = IngestionResult(files_discovered=len(discovered))
        for rel_path in sorted(indexed - discovered):
            self._graph.delete_ingested_file_sync(canonical, rel_path)
            result.files_deleted += 1
        return result

    def _ingest_entries(
        self,
        repo_path: str,
        entries: list[FileEntry],
        *,
        force: bool,
        sweep_deleted: bool,
    ) -> IngestionResult:
        result = IngestionResult(files_discovered=len(entries))
        repo_id = self._graph.get_or_create_repo_sync(repo_path)
        hashes = self._graph.get_all_ingested_files_sync(repo_path)
        changed = [
            entry
            for entry in entries
            if force or hashes.get(entry.rel_path) != entry.content_hash
        ]
        result.files_changed = len(changed)

        if sweep_deleted:
            discovered_paths = {entry.rel_path for entry in entries}
            indexed_paths = set(
                self._graph.get_file_paths_for_repo_sync(repo_path)
            ) | set(hashes)
            for rel_path in sorted(indexed_paths - discovered_paths):
                self._graph.delete_ingested_file_sync(repo_path, rel_path)
                result.files_deleted += 1

        if not changed:
            return result

        self._report("parse", 0, len(changed), message="Parsing changed source files")
        parsed, failed = self._parse_files(changed)
        result.files_failed = len(failed)
        for index, item in enumerate(parsed, start=1):
            self._report("parse", index, len(changed), path=item.entry.rel_path)

        nodes, node_ids, replaced_ids = self._prepare_nodes(repo_path, repo_id, parsed)
        # Replacing the file subgraph, rather than merging nodes in place,
        # guarantees removed calls/imports cannot survive an incremental run.
        # Native foreign keys cascade every incident edge during deletion.
        for node_id in sorted(replaced_ids):
            self._graph.delete_node_sync(node_id)
        if nodes:
            result.entities_created = self._graph.batch_upsert_nodes_sync(nodes)

        edges = self._prepare_edges(parsed, node_ids)
        result.edges_created = (
            self._graph.batch_upsert_edges_sync(edges) if edges else 0
        )
        result.edges_dropped = sum(len(item.edges) for item in parsed) - len(edges)

        summaries = self._summarize(parsed, result)
        if summaries:
            summary_updates = self._summary_updates(nodes, summaries)
            if summary_updates:
                self._graph.batch_upsert_nodes_sync(summary_updates)

        embeddings_durable = self._embed(parsed, node_ids, result, summaries)
        if embeddings_durable:
            self._graph.flush_vector_index_sync()

        # This is the final pipeline boundary. A failed parse never reaches it;
        # a completed job cannot be acknowledged until these records persist.
        for item in parsed:
            entry = item.entry
            self._graph.upsert_ingested_file_sync(
                file_id=_file_record_id(repo_path, entry.rel_path),
                repo_path=repo_path,
                rel_path=entry.rel_path,
                language=entry.language,
                content_hash=entry.content_hash,
                byte_size=entry.byte_size,
            )
        return result

    def _parse_files(
        self, entries: list[FileEntry]
    ) -> tuple[list[_ParsedFile], list[FileEntry]]:
        def parse(entry: FileEntry) -> _ParsedFile:
            source = entry.abs_path.read_text(encoding="utf-8", errors="replace")
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

    def _prepare_nodes(
        self,
        repo_path: str,
        repo_id: int,
        parsed: list[_ParsedFile],
    ) -> tuple[list[dict[str, object]], dict[str, str], set[str]]:
        nodes: list[dict[str, object]] = []
        qualified_to_id: dict[str, str] = {}
        replaced_ids: set[str] = set()
        for item in parsed:
            for entity in item.entities:
                node_id = _node_id(repo_path, item.entry.rel_path, entity)
                qualified_to_id[entity.qualified_name] = node_id
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
            self._graph.delete_ingestion_record_sync(repo_path, item.entry.rel_path)
        return nodes, qualified_to_id, replaced_ids

    @staticmethod
    def _prepare_edges(
        parsed: list[_ParsedFile],
        qualified_to_id: dict[str, str],
    ) -> list[dict[str, object]]:
        edges: list[dict[str, object]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in parsed:
            for edge in item.edges:
                source = qualified_to_id.get(edge.source_qualified_name)
                target = qualified_to_id.get(edge.target_qualified_name)
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

    def _summarize(
        self, parsed: list[_ParsedFile], result: IngestionResult
    ) -> dict[str, str]:
        if self._summarizer is None:
            return {}
        files = {
            item.entry.rel_path: "\n".join(
                entity.embed_text() for entity in item.entities
            )
            for item in parsed
        }
        try:
            summaries = _run(self._summarizer.summarize_files(files))
        except (ProviderUnavailableError, OSError, RuntimeError) as exc:
            result.semantic_degraded_reason = str(exc)
            return {}
        result.summaries_generated = len(summaries)
        return summaries

    @staticmethod
    def _summary_updates(
        nodes: list[dict[str, object]], summaries: dict[str, str]
    ) -> list[dict[str, object]]:
        updates: list[dict[str, object]] = []
        for node in nodes:
            metadata = dict(cast(dict[str, object], node["metadata"]))
            path = metadata.get("file_path")
            if (
                node["type"] == NodeType.FILE.value
                and isinstance(path, str)
                and path in summaries
            ):
                metadata["summary"] = summaries[path]
                updates.append({**node, "metadata": metadata})
        return updates

    def _embed(
        self,
        parsed: list[_ParsedFile],
        node_ids: dict[str, str],
        result: IngestionResult,
        summaries: dict[str, str],
    ) -> bool:
        if self._embeddings is None or not self._embeddings.metadata.available:
            if self._embeddings is not None:
                result.semantic_degraded_reason = self._embeddings.metadata.reason
            return False
        entities = [entity for item in parsed for entity in item.entities]
        texts = [
            f"{summaries.get(item.entry.rel_path, '')}. {entity.embed_text()}".lstrip(
                ". "
            )
            for item in parsed
            for entity in item.entities
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
        except (ProviderUnavailableError, OSError, RuntimeError) as exc:
            result.semantic_degraded_reason = str(exc)
            return False
        pairs = [
            (node_ids[entity.qualified_name], vector)
            for entity, vector in zip(entities, vectors, strict=True)
        ]
        result.embeddings_created = (
            self._graph.batch_upsert_embeddings_sync(pairs) if pairs else 0
        )
        return bool(pairs)
