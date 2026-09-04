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
from scs.indexing.discovery import IngestionPolicy
from scs.indexing.repository_paths import canonicalize_repo_path
from scs.indexing.runner import IngestionJobRunner
from scs.indexing.watcher import RepositoryWatcher
from scs.identity import IdentityPublisher
from scs.providers.base import EmbeddingProvider
from scs.providers.mlx import MLXEmbeddingProvider
from scs.providers.openai_compatible import OpenAICompatibleEmbeddingProvider
from scs.service import ProcessLock
from scs.services import SCSServiceRoutes
from scs.storage import ProjectStoreRegistry, StoreBinding, StoreGeneration, StoreState
from scs.wire.events import EventBroker
from scs.wire.router import Router
from scs.wire.server import WireServer

CLIENT_HANDOFF_SECONDS = 0.5
UNATTACHED_STARTUP_GRACE_SECONDS = 10.0


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
        self._identity: IdentityPublisher | None = None
        self._lock: ProcessLock | None = None
        self._jobs: IngestionJobStore | None = None
        self._graph: NativeGraph | None = None
        self._stores: ProjectStoreRegistry | None = None
        self._runner: IngestionJobRunner | None = None
        self._embeddings: EmbeddingProvider | None = None
        self._watchers: dict[str, RepositoryWatcher] = {}
        self._events: EventBroker = EventBroker()
        self._started: bool = False
        self._shutdown_requested: asyncio.Event = asyncio.Event()
        self._shutdown_task: asyncio.Task[None] | None = None
        self._ever_attached: bool = False
        self._services: SCSServiceRoutes = SCSServiceRoutes(
            graph=self._require_graph,
            jobs=self._require_jobs,
            embeddings=self._require_embeddings,
            graph_for_repository=self._lookup_graph,
            binding_for_repository=self._binding_for_repository,
        )
        self._register_methods()

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
        identity: IdentityPublisher | None = None
        try:
            embeddings: EmbeddingProvider
            if self.settings.embedding_provider in {"openai", "omlx"}:
                is_openai = self.settings.embedding_provider == "openai"
                embeddings = OpenAICompatibleEmbeddingProvider(
                    base_url=(
                        self.settings.openai_base_url
                        if is_openai
                        else self.settings.omlx_base_url
                    ),
                    model_name=self.settings.embedding_model,
                    dimension=self.settings.embedding_dimension,
                    batch_size=self.settings.embedding_batch_size,
                    provider_name="openai" if is_openai else "omlx-openai-compatible",
                    api_key=self.settings.effective_openai_api_key,
                )
            else:
                embeddings = MLXEmbeddingProvider(
                    model_name=self.settings.embedding_model,
                    dimension=self.settings.embedding_dimension,
                    batch_size=self.settings.embedding_batch_size,
                )
            stores = ProjectStoreRegistry(home=paths.home, provider=embeddings.metadata)
            jobs = await asyncio.to_thread(IngestionJobStore, paths.jobs_database)
            parser = NativeParser()
            loop = asyncio.get_running_loop()

            def graph_for_job(job: IngestionJob) -> NativeGraph:
                if job.store_id is None or job.store_generation is None:
                    raise RuntimeError("legacy unbound ingestion job is not executable")
                return stores.graph_for_binding(
                    job.repo_path,
                    StoreBinding(job.store_id, job.store_generation),
                )

            def pipeline_factory(job: IngestionJob) -> IngestionPipeline:
                def report(progress: IngestionProgress) -> None:
                    jobs.update_progress(
                        job.id,
                        phase=progress.phase,
                        current=progress.current,
                        total=progress.total,
                        message=progress.message,
                    )
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
                    graph=graph_for_job(job),
                    parser=parser,
                    embeddings=embeddings,
                    progress=report,
                    policy=IngestionPolicy(
                        text_fallback=self.settings.index_text_fallback,
                        max_file_bytes=self.settings.index_max_file_bytes,
                        text_sample_bytes=self.settings.index_text_sample_bytes,
                        large_dir_file_count=self.settings.index_large_dir_files,
                        large_dir_byte_size=self.settings.index_large_dir_bytes,
                    ),
                )

            def mark_job_store_ready(job: IngestionJob) -> None:
                """Publish readiness only after a bound indexing job has succeeded."""

                if job.mode == "drop_index":
                    return
                if job.store_id is None or job.store_generation is None:
                    raise RuntimeError(
                        "legacy unbound ingestion job cannot publish readiness"
                    )
                stores.mark_semantic_ready(
                    job.repo_path,
                    StoreBinding(job.store_id, job.store_generation),
                )

            def mark_job_store_stale(job: IngestionJob) -> None:
                """Withdraw semantic readiness before a job mutates its graph."""

                if job.mode == "drop_index":
                    return
                if job.store_id is None or job.store_generation is None:
                    raise RuntimeError(
                        "legacy unbound ingestion job cannot publish staleness"
                    )
                stores.catalog.update_state(
                    job.repo_path,
                    expected_generation=StoreGeneration(job.store_generation),
                    state=StoreState.SEMANTIC_STALE,
                )

            runner = IngestionJobRunner(
                store=jobs,
                graph_for_job=graph_for_job,
                pipeline_factory=pipeline_factory,
                on_started=mark_job_store_stale,
                on_completed=mark_job_store_ready,
                event_sink=BrokerEventSink(self._events),
            )
            await runner.start()
            self._stores = stores
            for record in await asyncio.to_thread(stores.records):
                if record.active_generation is None:
                    continue
                if stores.lookup_graph(record.canonical_root) is None:
                    continue
                await self._ensure_watcher(record.canonical_root, jobs=jobs)
            server = WireServer(
                self._router,
                socket_path=paths.runtime / "scs.sock",
                client_count_changed=self._client_count_changed,
            )
            await server.start()
            self._graph = None
            self._jobs = jobs
            self._runner = runner
            self._embeddings = embeddings
            self._server = server
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
            self._stores = None
            self._jobs = None
            self._runner = None
            self._embeddings = None
            raise
        self._identity = identity
        self._lock = process_lock
        self._started = True

    async def stop(self) -> None:
        """Stop new requests before releasing the root-scoped ownership lock."""

        shutdown_task, self._shutdown_task = self._shutdown_task, None
        if shutdown_task is not None:
            shutdown_task.cancel()
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
        stores = self._stores
        self._stores = None
        if stores is not None:
            await asyncio.to_thread(stores.flush)
        process_lock = self._lock
        self._lock = None
        if process_lock is not None:
            process_lock.release()
        self._jobs = None
        self._embeddings = None
        self._started = False

    async def wait_for_shutdown_request(self) -> None:
        """Wait until signals or the final attached client request shutdown."""

        await self._shutdown_requested.wait()

    def request_shutdown(self) -> None:
        """Request orderly daemon teardown."""

        self._shutdown_requested.set()

    def arm_startup_grace(self) -> None:
        """Stop a lazily spawned daemon that never receives a client lease."""

        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(
                self._shutdown_after(UNATTACHED_STARTUP_GRACE_SECONDS, unattached=True)
            )

    async def _client_count_changed(self, count: int) -> None:
        """Debounce zero-client shutdown across bridge handoffs."""

        task, self._shutdown_task = self._shutdown_task, None
        if task is not None:
            task.cancel()
        if count > 0:
            self._ever_attached = True
            return
        if self._ever_attached:
            self._shutdown_task = asyncio.create_task(
                self._shutdown_after(CLIENT_HANDOFF_SECONDS, unattached=False)
            )

    async def _shutdown_after(self, delay: float, *, unattached: bool) -> None:
        await asyncio.sleep(delay)
        if not unattached or not self._ever_attached:
            self.request_shutdown()

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

        @self._router.method("system.client.attach")
        async def client_attach(_params: dict[str, object]) -> dict[str, object]:
            return {"generation": self._generation, "attached": True}

        @self._router.method("repositories.status")
        async def repository_statuses(params: dict[str, object]) -> dict[str, object]:
            raw_paths = params.get("repo_paths", [])
            if not isinstance(raw_paths, list):
                raise ValueError("repo_paths must be a list of strings")
            path_values = cast(list[object], raw_paths)
            if not all(isinstance(path, str) for path in path_values):
                raise ValueError("repo_paths must be a list of strings")
            repo_paths = [path for path in path_values if isinstance(path, str)]
            jobs = self._require_jobs()
            recent = await asyncio.to_thread(jobs.list_recent, limit=200)
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
                graph = self._lookup_graph(path)
                all_store_stats = (
                    await asyncio.to_thread(graph.get_ingestion_stats_sync)
                    if graph is not None
                    else {}
                )
                repo_stats = all_store_stats.get(path, {})
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
            stores = self._require_stores()
            canonical = canonicalize_repo_path(raw_repo_path)
            record = stores.catalog.lookup(canonical)
            if record is None or record.active_generation is None:
                raise ValueError("repository does not have an indexed project store")
            job = await asyncio.to_thread(
                jobs.enqueue,
                repo_path=canonical,
                store_id=record.store_id,
                store_generation=record.active_generation,
                mode="drop_index",
                reason="explicit_drop_index",
            )
            watcher = self._watchers.pop(canonical, None)
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
        stores = self._require_stores()
        record, graph = await asyncio.to_thread(stores.ensure_graph, str(repo_path))
        generation = record.active_generation
        if generation is None:
            raise RuntimeError(
                "explicit project store creation did not activate a generation"
            )
        self._graph = graph
        job = await asyncio.to_thread(
            jobs.enqueue,
            repo_path=str(repo_path),
            store_id=record.store_id,
            store_generation=generation,
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

    def _require_stores(self) -> ProjectStoreRegistry:
        """Return the catalog-routed store registry after daemon startup."""

        stores = self._stores
        if stores is None:
            raise RuntimeError("SCS project-store registry is not ready")
        return stores

    def _lookup_graph(self, repo_path: str) -> NativeGraph | None:
        """Resolve an indexed project without creating a catalog or store."""

        stores = self._stores
        return stores.lookup_graph(repo_path) if stores is not None else None

    def _binding_for_repository(self, repo_path: str) -> tuple[str, str] | None:
        """Resolve the immutable job binding for an existing project store."""

        stores = self._stores
        if stores is None:
            return None
        record = stores.catalog.lookup(repo_path)
        if record is None or record.active_generation is None:
            return None
        return str(record.store_id), str(record.active_generation)

    def _require_embeddings(self) -> EmbeddingProvider:
        embeddings = self._embeddings
        if embeddings is None:
            raise RuntimeError("SCS embedding provider is not ready")
        return embeddings

    async def _ensure_watcher(
        self,
        repo_path: str,
        *,
        jobs: IngestionJobStore | None = None,
    ) -> None:
        canonical = canonicalize_repo_path(repo_path)
        if (
            not self.settings.auto_reindex_enabled
            or canonical in self._watchers
            or not Path(canonical).is_dir()
        ):
            return
        active_jobs = jobs or self._require_jobs()
        record = self._require_stores().catalog.lookup(canonical)
        if record is None or record.active_generation is None:
            return
        watcher = RepositoryWatcher(
            jobs=active_jobs,
            repo_path=Path(canonical),
            store_id=str(record.store_id),
            store_generation=str(record.active_generation),
            active_interval_seconds=self.settings.auto_reindex_active_seconds,
            idle_interval_seconds=self.settings.auto_reindex_idle_seconds,
            debounce_seconds=self.settings.auto_reindex_debounce_seconds,
            git_timeout_seconds=self.settings.auto_reindex_git_timeout_seconds,
        )
        await watcher.start()
        self._watchers[canonical] = watcher


async def serve(settings: SCSSettings | None = None) -> None:
    """Run SCS until SIGINT or SIGTERM and always release owned artifacts."""

    daemon = SCSDaemon(settings)
    loop = asyncio.get_running_loop()
    for received_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(received_signal, daemon.request_shutdown)
    await daemon.start()
    daemon.arm_startup_grace()
    try:
        await daemon.wait_for_shutdown_request()
    finally:
        await daemon.stop()
