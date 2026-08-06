"""Standalone SCS daemon composition and lifecycle."""

from __future__ import annotations

import asyncio
import signal
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from scs import __version__
from scs.config import SCSSettings
from scs.graph.native import NativeGraph
from scs.indexing.jobs import IngestionJob, IngestionJobStore, job_to_dict
from scs.indexing.parser.native import NativeParser
from scs.indexing.pipeline import IngestionPipeline, IngestionProgress
from scs.indexing.repository_paths import canonicalize_repo_path
from scs.indexing.runner import IngestionJobRunner
from scs.indexing.watcher import RepositoryWatcher
from scs.identity import IdentityPublisher
from scs.mcp.gateway import SCSWireGateway
from scs.mcp.http import MCPHTTPServer
from scs.mcp.server import build_mcp
from scs.providers.base import EmbeddingProvider
from scs.providers.mlx import MLXEmbeddingProvider
from scs.service import ProcessLock
from scs.services import SCSServiceRoutes
from scs.wire.events import EventBroker
from scs.wire.router import Router
from scs.wire.server import WireServer


def _metadata_integer(values: Mapping[str, object], key: str) -> int:
    """Convert one graph statistic while rejecting structurally invalid metadata."""

    value = values.get(key, 0)
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        raise TypeError(f"{key} must be numeric")
    return int(value)


class BrokerEventSink:
    """Adapt transport-neutral indexing events to the daemon event broker."""

    def __init__(self, broker: EventBroker) -> None:
        self._broker: EventBroker = broker

    async def publish(self, event: str, payload: Mapping[str, object]) -> None:
        """Publish an indexing event on its stable SCSWire topic."""

        await self._broker.publish(event, dict(payload))


class SCSDaemon:
    """Own SCS storage, durable jobs, and the local control socket as one unit."""

    def __init__(self, settings: SCSSettings | None = None) -> None:
        self.settings: SCSSettings = settings or SCSSettings()
        self._generation: str = uuid.uuid4().hex
        self._router: Router = Router()
        self._server: WireServer | None = None
        self._mcp_server: MCPHTTPServer | None = None
        self._identity: IdentityPublisher | None = None
        self._lock: ProcessLock | None = None
        self._jobs: IngestionJobStore | None = None
        self._graph: NativeGraph | None = None
        self._runner: IngestionJobRunner | None = None
        self._embeddings: EmbeddingProvider | None = None
        self._watchers: dict[str, RepositoryWatcher] = {}
        self._events: EventBroker = EventBroker()
        self._started: bool = False
        self._services: SCSServiceRoutes = SCSServiceRoutes(
            graph=self._require_graph,
            jobs=self._require_jobs,
            embeddings=self._require_embeddings,
        )
        self._register_methods()

    @property
    def mcp_address(self) -> tuple[str, int]:
        """Return the bound internal MCP address for health checks and tests."""

        server = self._mcp_server
        if server is None:
            raise RuntimeError("SCS MCP server is not started")
        return server.address

    async def start(self) -> None:
        """Validate isolation, acquire ownership, and begin accepting requests."""

        if self._started:
            raise RuntimeError("SCS daemon is already started")
        paths = self.settings.paths
        paths.ensure()
        process_lock = ProcessLock(paths.home / ".daemon.lock")
        process_lock.acquire()
        runner: IngestionJobRunner | None = None
        server: WireServer | None = None
        mcp_server: MCPHTTPServer | None = None
        identity: IdentityPublisher | None = None
        try:
            embeddings = MLXEmbeddingProvider(
                model_name=self.settings.embedding_model,
                dimension=self.settings.embedding_dimension,
                batch_size=self.settings.embedding_batch_size,
            )
            graph = await asyncio.to_thread(
                NativeGraph,
                database_path=paths.database,
                vector_path=paths.vector_index,
                provider_metadata_path=paths.provider_metadata,
                provider=embeddings.metadata,
            )
            jobs = await asyncio.to_thread(IngestionJobStore, paths.jobs_database)
            parser = NativeParser()
            loop = asyncio.get_running_loop()

            def pipeline_factory(_job: IngestionJob) -> IngestionPipeline:
                def report(progress: IngestionProgress) -> None:
                    payload: dict[str, object] = {
                        "phase": progress.phase,
                        "current": progress.current,
                        "total": progress.total,
                        "file_path": progress.file_path,
                        "message": progress.message,
                    }
                    loop.call_soon_threadsafe(
                        asyncio.create_task,
                        self._events.publish("indexing_progress", payload),
                    )

                return IngestionPipeline(
                    graph=graph,
                    parser=parser,
                    embeddings=embeddings,
                    progress=report,
                )

            runner = IngestionJobRunner(
                store=jobs,
                graph=graph,
                pipeline_factory=pipeline_factory,
                event_sink=BrokerEventSink(self._events),
            )
            await runner.start()
            existing_repositories = await asyncio.to_thread(
                graph.get_ingestion_stats_sync
            )
            for repo_path in existing_repositories:
                await self._ensure_watcher(
                    repo_path,
                    graph=graph,
                    jobs=jobs,
                    parser=parser,
                )
            server = WireServer(self._router, socket_path=paths.runtime / "scs.sock")
            await server.start()
            mcp_server = MCPHTTPServer(
                build_mcp(SCSWireGateway(server.socket_path)),
                host=self.settings.mcp_internal_host,
                port=self.settings.mcp_internal_port,
            )
            self._graph = graph
            self._jobs = jobs
            self._runner = runner
            self._embeddings = embeddings
            self._server = server
            await mcp_server.start()
            identity = IdentityPublisher(
                paths.runtime / "daemon-service.json",
                service="scs-daemon",
                generation=self._generation,
                artifact_path=Path(__file__),
            )
            identity.publish()
        except BaseException:
            if identity is not None:
                identity.remove_owned()
            if mcp_server is not None:
                await mcp_server.stop()
            if server is not None:
                await server.stop()
            watchers, self._watchers = tuple(self._watchers.values()), {}
            for watcher in watchers:
                await watcher.stop()
            if runner is not None:
                await runner.stop()
            process_lock.release()
            self._server = None
            self._graph = None
            self._jobs = None
            self._runner = None
            self._embeddings = None
            raise
        self._mcp_server = mcp_server
        self._identity = identity
        self._lock = process_lock
        self._started = True

    async def stop(self) -> None:
        """Stop new requests before releasing the root-scoped ownership lock."""

        mcp_server = self._mcp_server
        self._mcp_server = None
        if mcp_server is not None:
            await mcp_server.stop()
        server = self._server
        self._server = None
        if server is not None:
            await server.stop()
        identity = self._identity
        self._identity = None
        if identity is not None:
            identity.remove_owned()
        watchers, self._watchers = tuple(self._watchers.values()), {}
        for watcher in watchers:
            await watcher.stop()
        runner = self._runner
        self._runner = None
        if runner is not None:
            await runner.stop()
        graph = self._graph
        self._graph = None
        if graph is not None:
            await asyncio.to_thread(graph.flush_vector_index_sync)
        process_lock = self._lock
        self._lock = None
        if process_lock is not None:
            process_lock.release()
        self._jobs = None
        self._embeddings = None
        self._started = False

    def _register_methods(self) -> None:
        @self._router.method("system.health")
        async def health(_params: dict[str, object]) -> dict[str, object]:
            return {
                "service": "scs",
                "version": __version__,
                "generation": self._generation,
                "ready": self._started,
                "protocol_min": 1,
                "protocol_max": 1,
            }

        @self._router.method("repositories.status")
        async def repository_statuses(params: dict[str, object]) -> dict[str, object]:
            raw_paths = params.get("repo_paths", [])
            if not isinstance(raw_paths, list):
                raise ValueError("repo_paths must be a list of strings")
            path_values = cast(list[object], raw_paths)
            if not all(isinstance(path, str) for path in path_values):
                raise ValueError("repo_paths must be a list of strings")
            repo_paths = [path for path in path_values if isinstance(path, str)]
            graph = self._require_graph()
            jobs = self._require_jobs()
            stats, recent = await asyncio.gather(
                asyncio.to_thread(graph.get_ingestion_stats_sync),
                asyncio.to_thread(jobs.list_recent, limit=200),
            )
            active_by_repo = {
                job.repo_path: job
                for job in recent
                if job.status in {"queued", "retrying", "running", "cancelling"}
            }
            failed_by_repo = {
                job.repo_path: job for job in recent if job.status == "failed"
            }
            repositories: list[dict[str, object]] = []
            for raw_path in repo_paths:
                path = canonicalize_repo_path(raw_path)
                active = active_by_repo.get(path)
                repo_stats = stats.get(path, {})
                if active is not None:
                    state = (
                        "indexing"
                        if active.status in {"running", "cancelling"}
                        else "queued"
                    )
                elif repo_stats:
                    state = "indexed"
                elif path in failed_by_repo:
                    state = "failed"
                else:
                    state = "unindexed"
                repositories.append(
                    {
                        "repo_path": path,
                        "state": state,
                        "file_count": _metadata_integer(repo_stats, "file_count"),
                        "last_indexed": repo_stats.get("last_indexed"),
                        "active_job_id": active.id if active is not None else None,
                    }
                )
            return {"repositories": repositories}

        @self._router.method("repository.index")
        async def index(params: dict[str, object]) -> dict[str, object]:
            return await self._enqueue(params, force=False)

        @self._router.method("repository.reindex")
        async def reindex(params: dict[str, object]) -> dict[str, object]:
            return await self._enqueue(params, force=True)

        @self._router.method("repository.drop_index")
        async def drop_index(params: dict[str, object]) -> dict[str, object]:
            raw_repo_path = params.get("repo_path")
            if not isinstance(raw_repo_path, str) or not raw_repo_path:
                raise ValueError("repo_path must be a non-empty string")
            jobs = self._require_jobs()
            job = await asyncio.to_thread(
                jobs.enqueue,
                repo_path=canonicalize_repo_path(raw_repo_path),
                mode="drop_index",
                reason="explicit_drop_index",
            )
            watcher = self._watchers.pop(canonicalize_repo_path(raw_repo_path), None)
            if watcher is not None:
                await watcher.stop()
            return {"accepted": True, "job": job_to_dict(job)}

        @self._router.method("jobs.recent")
        async def jobs_recent(params: dict[str, object]) -> dict[str, object]:
            jobs = self._require_jobs()
            repo_path = params.get("repo_path")
            if repo_path is not None and not isinstance(repo_path, str):
                raise ValueError("repo_path must be a string")
            raw_limit = params.get("limit", 50)
            if not isinstance(raw_limit, int):
                raise ValueError("limit must be an integer")
            recent = await asyncio.to_thread(
                jobs.list_recent,
                repo_path=repo_path,
                limit=raw_limit,
            )
            return {"jobs": [job_to_dict(job) for job in recent]}

        service_methods = {
            "knowledge.search": self._services.search,
            "knowledge.related": self._services.related,
            "knowledge.graph_context": self._services.graph_context,
            "knowledge.nodes.list": self._services.nodes_list,
            "knowledge.stats": self._services.stats,
            "knowledge.inspect_file": self._services.inspect_file,
            "knowledge.composite.regression_risk": self._services.composite_regression_risk,
            "repository.ingest_files": self._services.ingest_files,
            "lsp.references": self._services.lsp_references,
        }
        for method_name, handler in service_methods.items():
            self._router.method(method_name)(handler)

    async def _enqueue(
        self,
        params: dict[str, object],
        *,
        force: bool,
    ) -> dict[str, object]:
        raw_repo_path = params.get("repo_path")
        if not isinstance(raw_repo_path, str) or not raw_repo_path:
            raise ValueError("repo_path must be a non-empty string")
        repo_path = Path(canonicalize_repo_path(raw_repo_path))
        if not repo_path.is_dir():
            raise ValueError(f"repository directory does not exist: {repo_path}")
        jobs = self._require_jobs()
        job = await asyncio.to_thread(
            jobs.enqueue,
            repo_path=str(repo_path),
            mode="force_full" if force else "full",
            reason="explicit_reindex" if force else "explicit_index",
        )
        await self._ensure_watcher(str(repo_path))
        return {"accepted": True, "job": job_to_dict(job)}

    def _require_jobs(self) -> IngestionJobStore:
        jobs = self._jobs
        if jobs is None:
            raise RuntimeError("SCS daemon is not ready")
        return jobs

    def _require_graph(self) -> NativeGraph:
        graph = self._graph
        if graph is None:
            raise RuntimeError("SCS graph is not ready")
        return graph

    def _require_embeddings(self) -> EmbeddingProvider:
        embeddings = self._embeddings
        if embeddings is None:
            raise RuntimeError("SCS embedding provider is not ready")
        return embeddings

    async def _ensure_watcher(
        self,
        repo_path: str,
        *,
        graph: NativeGraph | None = None,
        jobs: IngestionJobStore | None = None,
        parser: NativeParser | None = None,
    ) -> None:
        canonical = canonicalize_repo_path(repo_path)
        if canonical in self._watchers or not Path(canonical).is_dir():
            return
        active_graph = graph or self._require_graph()
        active_jobs = jobs or self._require_jobs()
        active_parser = parser or NativeParser()
        watcher = RepositoryWatcher(
            graph=active_graph,
            jobs=active_jobs,
            base_dir=Path(canonical),
            supported_extensions=active_parser.supported_extensions(),
        )
        await watcher.start()
        self._watchers[canonical] = watcher


async def serve(settings: SCSSettings | None = None) -> None:
    """Run SCS until SIGINT or SIGTERM and always release owned artifacts."""

    daemon = SCSDaemon(settings)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for received_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(received_signal, stopped.set)
    await daemon.start()
    try:
        await stopped.wait()
    finally:
        await daemon.stop()
