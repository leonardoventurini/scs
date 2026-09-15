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
from scs.metrics import AggregateMetrics
from scs.providers.base import EmbeddingProvider, RerankingProvider
from scs.providers.mlx import MLXEmbeddingProvider
from scs.providers.omlx_reranking import OMLXRerankingProvider
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


def build_reranker(settings: SCSSettings) -> RerankingProvider | None:
    """Compose reranking only when an operator configures a model."""

    model_name = settings.reranking_model
    if model_name is None:
        return None

    return OMLXRerankingProvider(
        base_url=settings.omlx_base_url,
        model_name=model_name,
    )


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
        self._router: Router = Router(observer=self._record_metric)
        self._server: WireServer | None = None
        self._identity: IdentityPublisher | None = None
        self._lock: ProcessLock | None = None
        self._jobs: IngestionJobStore | None = None
        self._graph: NativeGraph | None = None
        self._stores: ProjectStoreRegistry | None = None
        self._runner: IngestionJobRunner | None = None
        self._embeddings: EmbeddingProvider | None = None
        self._reranker: RerankingProvider | None = None
        self._metrics: AggregateMetrics | None = None
        self._watchers: dict[str, RepositoryWatcher] = {}
        self._repository_mutation_locks: dict[str, asyncio.Lock] = {}
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
            reranker=lambda: self._reranker,
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
            self._metrics = await asyncio.to_thread(
                self._open_metrics_fail_open,
                paths.metrics_database,
                paths.metrics_key,
            )
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
            reranker = build_reranker(self.settings)
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
                    cancellation_requested=lambda: jobs.cancellation_requested(job.id),
                )

            def delete_repository(job: IngestionJob) -> dict[str, object]:
                """Retire the job-bound project store without reading source."""

                if job.store_id is None:
                    raise RuntimeError("repository deletion job has no store identity")
                # The unscoped compatibility graph must not retain an open
                # handle after the registry evicts the job-bound graph.
                self._graph = None
                return stores.delete_repository(
                    job.repo_path,
                    store_id=job.store_id,
                    store_generation=job.store_generation,
                    deletion_id=job.id,
                )

            async def restore_failed_deletion(job: IngestionJob) -> None:
                """Resume automatic reconciliation after terminal delete failure."""

                if job.mode == "drop_index":
                    await self._ensure_watcher(job.repo_path, jobs=jobs)

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
                on_failed=restore_failed_deletion,
                repository_deleter=delete_repository,
                event_sink=BrokerEventSink(self._events),
            )
            self._stores = stores
            self._jobs = jobs
            await runner.start()
            for record in await asyncio.to_thread(stores.records):
                if record.active_generation is None:
                    continue
                if await asyncio.to_thread(
                    jobs.active_deletion, record.canonical_root
                ) is not None:
                    continue
                await self._ensure_watcher(record.canonical_root, jobs=jobs)
            server = WireServer(
                self._router,
                socket_path=paths.runtime / "scs.sock",
                client_count_changed=self._client_count_changed,
            )
            await server.start()
            self._graph = None
            self._runner = runner
            self._embeddings = embeddings
            self._reranker = reranker
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
            self._reranker = None
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
        if identity is not None:
            identity.remove_owned()
        self._jobs = None
        self._embeddings = None
        self._reranker = None
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
        jobs = self._jobs
        if jobs is not None and await asyncio.to_thread(jobs.has_active):
            # Keep the control plane observable while background work drains.
            self._shutdown_task = asyncio.create_task(
                self._shutdown_after(
                    CLIENT_HANDOFF_SECONDS,
                    unattached=unattached,
                )
            )
            return
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

        @self._router.method("system.shutdown")
        async def system_shutdown(params: dict[str, object]) -> dict[str, object]:
            if params.get("generation") != self._generation:
                raise ValueError("daemon generation changed before shutdown")
            cancelled_jobs = 0
            if params.get("cancel_active") is True:
                cancelled_jobs = len(
                    await asyncio.to_thread(self._require_jobs().request_cancel_all)
                )
            self.request_shutdown()
            return {
                "accepted": True,
                "generation": self._generation,
                "cancelled_jobs": cancelled_jobs,
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
            async with self._repository_mutation_lock(canonical):
                active = await asyncio.to_thread(jobs.active_deletion, canonical)
                if active is not None:
                    return {
                        "accepted": True,
                        "already_absent": False,
                        "job": job_to_dict(active),
                    }
                target = await asyncio.to_thread(stores.deletion_target, canonical)
                if target is None:
                    return {
                        "accepted": True,
                        "already_absent": True,
                        "job": None,
                    }
                job = await asyncio.to_thread(
                    jobs.enqueue,
                    repo_path=canonical,
                    store_id=target.store_id,
                    store_generation=target.store_generation,
                    mode="drop_index",
                    reason="explicit_drop_index",
                )
                watcher = self._watchers.pop(canonical, None)
                if watcher is not None:
                    await watcher.stop()
                return {
                    "accepted": True,
                    "already_absent": False,
                    "job": job_to_dict(job),
                }

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

        @self._router.method("metrics.report")
        async def metrics_report(params: dict[str, object]) -> dict[str, object]:
            raw_days = params.get("days", 7)
            if not isinstance(raw_days, int) or isinstance(raw_days, bool):
                raise ValueError("days must be an integer")
            metrics = self._metrics
            if metrics is None:
                return {
                    "days": max(1, min(raw_days, 30)),
                    "totals": {"calls": 0, "errors": 0},
                    "operations": [],
                }
            return await asyncio.to_thread(metrics.report, days=raw_days)

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

    def _record_metric(
        self,
        method: str,
        params: dict[str, object],
        status: str,
        duration_ms: float,
    ) -> None:
        """Persist daemon-wide aggregates without affecting request outcomes."""

        metrics = self._metrics
        if metrics is not None:
            metrics.record(
                method,
                params,
                status=status,
                duration_ms=duration_ms,
            )

    @staticmethod
    def _open_metrics_fail_open(
        database_path: Path,
        key_path: Path,
    ) -> AggregateMetrics | None:
        """Keep optional observation from becoming a daemon dependency."""

        try:
            return AggregateMetrics(database_path, key_path)
        except Exception:
            return None

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
        canonical = str(repo_path)
        async with self._repository_mutation_lock(canonical):
            if await asyncio.to_thread(jobs.active_deletion, canonical) is not None:
                raise ValueError("repository deletion is in progress")
            record, graph = await asyncio.to_thread(stores.ensure_graph, canonical)
            generation = record.active_generation
            if generation is None:
                raise RuntimeError(
                    "explicit project store creation did not activate a generation"
                )
            self._graph = graph
            job = await asyncio.to_thread(
                jobs.enqueue,
                repo_path=canonical,
                store_id=record.store_id,
                store_generation=generation,
                mode="force_full" if force else "full",
                reason="explicit_reindex" if force else "explicit_index",
            )
            await self._ensure_watcher(canonical)
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
        jobs = self._jobs
        if jobs is not None and jobs.active_deletion(repo_path) is not None:
            raise ValueError("repository deletion is in progress")
        record = stores.catalog.lookup(repo_path)
        if record is None or record.active_generation is None:
            return None
        return str(record.store_id), str(record.active_generation)

    def _repository_mutation_lock(self, repo_path: str) -> asyncio.Lock:
        """Serialize enrollment and deletion decisions for one root."""

        lock = self._repository_mutation_locks.get(repo_path)
        if lock is None:
            lock = asyncio.Lock()
            self._repository_mutation_locks[repo_path] = lock
        return lock

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
