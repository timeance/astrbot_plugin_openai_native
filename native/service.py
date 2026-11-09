from __future__ import annotations

import asyncio
import hashlib
import math
import os
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from .adapters import ImageAdapter, SearchAdapter
from .assets import AssetStore, atomic_write
from .capabilities import CapabilityRegistry
from .config import Settings
from .diagnostics import Diagnostics, connection_info, safe_error
from .identity import actor_from_event
from .models import (
    Artifact,
    ImageRequest,
    InputAsset,
    NativeError,
    SearchHit,
    SearchRequest,
    SearchResult,
    TaskHandle,
)
from .providers import ProviderResolver
from .store import ACTIVE, Store, encode
from .transport import HTTPTransport


def _hash(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def _error(exc, *, ambiguous=False):
    if isinstance(exc, NativeError):
        return exc
    return NativeError(
        "internal_error", "Operation failed; consult the administrator using its task ID", ambiguous=ambiguous
    )


class NativeService:
    def __init__(self, context, settings: Settings, data_dir: Path, *, transport=None, delivery=None):
        self.context, self.settings, self.root = context, settings, Path(data_dir)
        self.delivery = delivery
        self.tools_callback = None
        self.diagnostics = Diagnostics()
        self.last_readiness = {}
        self.last_connections = {}
        self.command_conflicts = []
        self.command_conflicts_callback = None
        self.store = Store(self.root / "operations.sqlite3", settings)
        self.transport = HTTPTransport(settings, transport=transport)
        self.search_adapter = SearchAdapter(self.transport, settings)
        self.image_adapter = ImageAdapter(self.transport, settings)
        self.capabilities = CapabilityRegistry(self.root / "capabilities.json")
        self.resolver = None
        self.assets = None
        self.queue = asyncio.Queue(maxsize=settings.limits["queue_size"])
        self.search_semaphore = asyncio.Semaphore(settings.limits["search_concurrency"])
        self.image_semaphore = asyncio.Semaphore(settings.limits["image_concurrency"])
        self.workers = []
        self.search_jobs = {}
        self.recovery_jobs = set()
        self.events = {}
        self.notifications = {}
        self.submit_lock = asyncio.Lock()
        self._closing = False
        self._initialized = False
        from .api import PublicAPI

        self.public_api = PublicAPI(self)

    async def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        key_path = self.root / ".fingerprint-key"
        if not key_path.exists():
            atomic_write(key_path, os.urandom(32))
        self.resolver = ProviderResolver(self.context, self.settings, key_path.read_bytes())
        await self.store.initialize()
        await self.capabilities.initialize()
        self.assets = AssetStore(self.root, self.settings, self.transport, self.store)
        # A send interrupted by process exit is never replayed automatically.
        await self.store.db.execute(
            "UPDATE operations SET delivery_status='unknown' WHERE delivery_status='sending'"
        )
        for op in await self.store.list_recovery():
            receipts = await self.assets.discover_receipts(op["id"])
            for receipt in receipts:
                await self.store.append(op["id"], "receipts", receipt)
            op = await self.store.get(op["id"])
            if op["kind"] == "search":
                actual = max(op["actual"], int(bool(op["result"])))
                uncertain = int(bool(op["submitted_count"]) and not actual)
                state = (
                    "succeeded"
                    if op["result"]
                    else (
                        "result_unavailable"
                        if actual
                        else ("needs_reconciliation" if uncertain else "cancelled")
                    )
                )
                await self.store.settle(
                    op["id"],
                    actual,
                    uncertain,
                    status=state,
                    error=None
                    if op["result"]
                    else {
                        "category": "interrupted",
                        "message": "Search interrupted by restart; no resubmission",
                    },
                )
            elif op["receipts"]:
                task = asyncio.create_task(self._recover_images(op["id"]), name="openai-native-recover")
                self.recovery_jobs.add(task)
                task.add_done_callback(self.recovery_jobs.discard)
            elif op["submitted_count"]:
                uncertain = min(1, max(0, op["submitted_count"] - op["next_index"]), op["pending"])
                state = "needs_reconciliation" if uncertain else "result_unavailable"
                await self.store.settle(
                    op["id"],
                    op["actual"],
                    uncertain,
                    status=state,
                    error={
                        "category": "interrupted",
                        "message": "Generation completion unknown after restart",
                    },
                )
            else:
                await self.store.settle(op["id"], 0, 0, status="cancelled")
        for _ in range(self.settings.limits["image_concurrency"]):
            self.workers.append(asyncio.create_task(self._worker(), name="openai-native-worker"))
        self.maintenance = asyncio.create_task(self._maintenance(), name="openai-native-cleanup")
        self._initialized = True

    def _ready(self):
        if not self._initialized or self._closing:
            raise NativeError("unavailable", "Plugin is starting or shutting down")

    def _notice(self, operation_id):
        return self.notifications.setdefault(operation_id, asyncio.Event())

    def _finish_notice(self, operation_id):
        notice = self.notifications.pop(operation_id, None)
        if notice is not None:
            notice.set()

    async def _connection(self, event, feature):
        if not self.settings.feature(feature)["enabled"]:
            raise NativeError("disabled", "This feature is disabled")
        connection = await self.resolver.resolve(event, feature)
        if event is not None:
            self.last_connections[feature] = connection
        self.diagnostics.add(event, kind="connection", feature=feature, **connection_info(connection))
        return connection

    async def _observe(self, connection, capability, state, reason, error=None):
        try:
            await self.capabilities.record(connection, capability, state, reason, error)
        except (NativeError, OSError):
            # Advisory cache persistence cannot undo completed paid work.
            pass

    async def _record_error(self, connection, capability, error):
        category = error.category
        if category in {
            "unsupported_endpoint",
            "unsupported_channel",
            "unsupported_model",
            "unsupported_format",
            "unsupported_capability",
            "model_not_supported",
            "endpoint_not_supported",
            "model_not_found",
        }:
            state = "unsupported"
        elif category in ("authentication", "auth_failed", "account_permission"):
            state = "auth_blocked"
        else:
            state = "temporary_failure"
        details = safe_error(error, connection)
        await self._observe(connection, capability, state, category, details)
        self.diagnostics.add(
            kind="upstream_error",
            capability=capability,
            connection=connection_info(connection),
            error=details,
        )

    def _search_request(self, request):
        if not isinstance(request, SearchRequest) or not isinstance(request.queries, list):
            raise NativeError("invalid_request", "Expected SearchRequest with a query list")
        queries = [q.strip() for q in request.queries if isinstance(q, str) and q.strip()]
        cfg = self.settings.feature("search")
        if not queries or len(queries) != len(request.queries) or len(queries) > cfg["max_queries"]:
            raise NativeError("invalid_request", "Invalid search query count")
        if any(len(q) > 4000 for q in queries):
            raise NativeError("invalid_request", "Search query is too long")
        if type(request.max_results) is not int or not 1 <= request.max_results <= cfg["max_results"]:
            raise NativeError("invalid_request", "Invalid search result limit")
        if request.recency is not None and (type(request.recency) is not int or request.recency < 0):
            raise NativeError("invalid_request", "Recency must be a nonnegative day count")
        if not isinstance(request.domains, list) or len(request.domains) > 20:
            raise NativeError("invalid_request", "Invalid domain filters")
        for domain in request.domains:
            if not isinstance(domain, str) or not domain or any(c in domain for c in "/:@ "):
                raise NativeError("invalid_request", "Domain filters must contain host names")
        return replace(request, queries=queries, recency=request.recency or None)

    async def search(self, event, request):
        self._ready()
        actor = actor_from_event(event)
        request = self._search_request(request)
        connection = await self._connection(event, "search")
        await self.capabilities.ensure(connection, "search")
        payload = asdict(request)
        fingerprint = _hash({"request": payload, "provider": connection.fingerprint})
        key = "message:" + actor.message_id + ":" + fingerprint if actor.message_id else uuid.uuid4().hex
        async with self.submit_lock:
            self._ready()
            op, created = await self.store.reserve(
                operation_id=uuid.uuid4().hex,
                kind="search",
                actor=actor,
                request=payload,
                connection=connection,
                units=1,
                idempotency_key=key,
                request_hash=fingerprint,
            )
            if created:
                task = asyncio.create_task(self._execute_search(op["id"], connection, request))
                self.search_jobs[op["id"]] = task
                task.add_done_callback(lambda done, identity=op["id"]: self.search_jobs.pop(identity, None))
        task = self.search_jobs.get(op["id"])
        if task:
            await asyncio.shield(task)
        else:
            await self._wait(op["id"], connection.timeout + 5)
        op = await self.store.get(op["id"])
        if op["result"]:
            result = op["result"]
            return SearchResult(**{**result, "sources": [SearchHit(**hit) for hit in result["sources"]]})
        err = op["error"] or {"category": "unavailable", "message": "Search has not completed"}
        error = NativeError(
            err["category"],
            err["message"],
            ambiguous=bool(op["pending"]),
            status_code=err.get("status_code"),
            upstream_type=err.get("upstream_type"),
            upstream_code=err.get("upstream_code"),
            request_id=err.get("request_id"),
        )
        error.operation_id = op["id"]
        raise error

    async def _execute_search(self, operation_id, connection, request):
        submitted = completed = False
        try:
            async with self.search_semaphore:
                if not await self.store.claim(operation_id):
                    return
                await self.store.submitted(operation_id)
                submitted = True
                result = await self.search_adapter.search(connection, request, operation_id)
                completed = True
                await self.store.update(operation_id, result=asdict(result))
                await self.store.settle(operation_id, 1, 0, status="succeeded")
                await self._observe(connection, "search", "available", "successful_request")
                self.diagnostics.add(
                    kind="search_result",
                    operation_id=operation_id,
                    status="succeeded",
                    connection=connection_info(connection),
                )
        except asyncio.CancelledError:
            await self.store.settle(
                operation_id,
                int(completed),
                int(submitted and not completed),
                status="result_unavailable"
                if completed
                else ("needs_reconciliation" if submitted else "cancelled"),
                error={"category": "interrupted", "message": "Search interrupted; no automatic resubmission"},
            )
            raise
        except Exception as exc:
            error = _error(exc, ambiguous=submitted)
            pending = int(submitted and not completed and error.ambiguous)
            await self.store.settle(
                operation_id,
                int(completed),
                pending,
                status="result_unavailable"
                if completed
                else ("needs_reconciliation" if pending else "failed"),
                error=error.to_dict(),
            )
            await self._record_error(connection, "search", error)
        finally:
            self._finish_notice(operation_id)

    def _image_request(self, request):
        if not isinstance(request, ImageRequest):
            raise NativeError("invalid_request", "Expected ImageRequest")
        cfg = self.settings.feature("image")
        if not isinstance(request.prompt, str) or not request.prompt.strip() or len(request.prompt) > 32000:
            raise NativeError("invalid_request", "Image prompt is empty or too long")
        if request.action not in ("auto", "generate", "edit"):
            raise NativeError("invalid_request", "Unknown image action")
        if type(request.n) is not int or not 1 <= request.n <= cfg["max_outputs"]:
            raise NativeError("invalid_request", "Invalid output count")
        if not isinstance(request.output_format, str) or not isinstance(request.references, list):
            raise NativeError("invalid_request", "Expected a format string and reference list")
        if len(request.references) > cfg["max_references"]:
            raise NativeError("invalid_request", "Too many reference images")
        output_format = (request.output_format or cfg["default_format"]).lower()
        if output_format not in cfg["formats"] or output_format not in ("png", "jpeg", "webp", "gif"):
            raise NativeError("invalid_request", "Output format is not enabled")
        size = request.size or cfg["default_size"]
        if request.aspect_ratio or request.resolution:
            if request.size and request.size != "auto":
                raise NativeError("invalid_request", "Choose size or aspect ratio/resolution, not both")
            try:
                w, h = map(int, (request.aspect_ratio or "1:1").split(":"))
                edge = {"1K": 1024, "1.5K": 1536, "2K": 2048, "4K": 4096}[
                    (request.resolution or "1K").upper()
                ]
                if min(w, h) <= 0 or max(w, h) / min(w, h) > 8:
                    raise ValueError()
                width, height = (edge, edge * h / w) if w >= h else (edge * w / h, edge)
                size = f"{max(16, round(width / 16) * 16)}x{max(16, round(height / 16) * 16)}"
            except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
                raise NativeError("invalid_request", "Invalid aspect ratio or resolution preset") from exc
        frames = request.frame_count if request.frame_count is not None else self.settings.gif["frame_count"]
        duration = (
            request.frame_duration_ms
            if request.frame_duration_ms is not None
            else self.settings.gif["frame_duration_ms"]
        )
        if output_format == "gif":
            if not self.settings.gif["enabled"]:
                raise NativeError("disabled", "GIF must first be enabled by an administrator")
            if (
                type(frames) is not int
                or not 2 <= frames <= 16
                or type(duration) is not int
                or not 20 <= duration <= 2000
            ):
                raise NativeError("invalid_request", "GIF requires 2-16 frames and 20-2000ms duration")
            duration = int(math.floor((duration + 5) / 10) * 10)
        elif request.frame_count is not None or request.frame_duration_ms is not None:
            raise NativeError("invalid_request", "Frame settings apply only to GIF output")
        else:
            frames = duration = None
        if size != "auto":
            try:
                width, height = map(int, size.lower().split("x"))
                if min(width, height) <= 0 or width * height > cfg["max_pixels"]:
                    raise ValueError()
                if output_format == "gif" and (
                    width % 16 or height % 16 or width * height * frames > cfg["max_frames_pixels"]
                ):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError) as exc:
                raise NativeError("invalid_request", "Invalid image dimensions or pixel budget") from exc
        if cfg["sizes"] and size not in cfg["sizes"]:
            raise NativeError("invalid_request", "Size is not allowed by this model profile")
        if request.quality not in ("auto", "low", "medium", "high"):
            raise NativeError("invalid_request", "Invalid image quality")
        if not isinstance(request.complexity, str) or len(request.complexity) > 32:
            raise NativeError("invalid_request", "Invalid image complexity")
        return replace(
            request,
            prompt=request.prompt.strip(),
            output_format=output_format,
            size=size,
            frame_count=frames,
            frame_duration_ms=duration,
        )

    async def submit_image(
        self, event, request, idempotency_key=None, *, deliver=False, allow_local_paths=False
    ):
        self._ready()
        actor = actor_from_event(event)
        request = self._image_request(request)
        connection = await self._connection(event, "image")
        if request.output_format == "gif" and connection.protocol != "images":
            raise NativeError("unsupported_protocol", "Native GIF requires the Images API")
        assets = await self.assets.prepare(event, actor, request, allow_local_paths=allow_local_paths)
        identity_request = asdict(replace(request, references=[]))
        identity_assets = [a.sha256 for a in assets] if request.references else "implicit-message-context"
        action = request.action if request.action != "auto" else ("edit" if assets else "generate")
        if action == "generate" and assets:
            raise NativeError("invalid_request", "Reference images require edit or auto action")
        request = replace(request, action=action, references=[])
        capability = action + ":" + request.output_format
        await self.capabilities.ensure(connection, capability)
        fingerprint = _hash(
            {
                "request": identity_request,
                "references": identity_assets,
                "provider": connection.fingerprint,
            }
        )
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256
        ):
            raise NativeError("invalid_request", "Invalid idempotency key")
        key = idempotency_key or (
            "message:" + actor.message_id + ":" + fingerprint if actor.message_id else uuid.uuid4().hex
        )
        payload = {"options": asdict(request), "assets": [asdict(a) for a in assets]}
        async with self.submit_lock:
            self._ready()
            op, created = await self.store.reserve(
                operation_id=uuid.uuid4().hex,
                kind="image",
                actor=actor,
                request=payload,
                connection=connection,
                units=request.n,
                idempotency_key=key,
                request_hash=fingerprint,
                deliver=deliver,
            )
            if created:
                self.events[op["id"]] = event if deliver else None
                try:
                    self.queue.put_nowait((op["id"], connection))
                except asyncio.QueueFull as exc:
                    self.events.pop(op["id"], None)
                    await self.store.settle(
                        op["id"],
                        0,
                        0,
                        status="failed",
                        error={"category": "queue_full", "message": "Image queue is full"},
                    )
                    raise NativeError("queue_full", "Image queue is full") from exc
        self.diagnostics.add(
            event,
            kind="image_submission",
            task_id=op["id"],
            status=op["status"],
            created=created,
            connection=connection_info(connection),
        )
        return TaskHandle(op["id"], op["status"], asdict(request))

    async def _worker(self):
        while True:
            operation_id, connection = await self.queue.get()
            try:
                async with self.image_semaphore:
                    if await self.store.claim(operation_id):
                        await self._run_image(operation_id, connection)
            finally:
                self.events.pop(operation_id, None)
                self.queue.task_done()

    async def _run_image(self, operation_id, connection):
        op = await self.store.get(operation_id)
        request = ImageRequest(**op["request"]["options"])
        assets = [InputAsset(**item) for item in op["request"]["assets"]]
        capability = request.action + ":" + request.output_format
        actual = op["actual"]
        unresolved = False
        try:
            for index in range(request.n):
                op = await self.store.get(operation_id)
                if op["cancel_requested"]:
                    await self.store.settle(operation_id, actual, 0, status="cancelled")
                    return
                await self.store.submitted(operation_id)
                unresolved = True
                batch = await self.image_adapter.generate(connection, replace(request, n=1), assets)
                unresolved = False
                actual += len(batch.outputs)
                if not batch.outputs:
                    raise NativeError("invalid_result", "Generation completed without an image")
                # Confirmed result count is charged even if persistence/download subsequently fails.
                await self.store.settle(
                    operation_id,
                    actual,
                    max(0, request.n - index - 1),
                    status="receiving",
                    next_index=index + 1,
                )
                receipt = await self.assets.receipt(operation_id, index, batch)
                await self.store.append(operation_id, "receipts", receipt)
                await self._observe(connection, capability, "available", "successful_request")
                await self._materialize(operation_id)
            op = await self.store.get(operation_id)
            await self.store.settle(
                operation_id, actual, 0, status="cancelled" if op["cancel_requested"] else "succeeded"
            )
            await self._deliver(operation_id)
            final = await self.store.get(operation_id)
            self.diagnostics.add(
                kind="image_result",
                task_id=operation_id,
                status=final["status"],
                generated=final["actual"],
                delivery_status=final["delivery_status"],
                connection=connection_info(connection),
            )
        except asyncio.CancelledError:
            state = (
                "needs_reconciliation" if unresolved else ("result_unavailable" if actual else "cancelled")
            )
            await self.store.settle(
                operation_id,
                actual,
                int(unresolved),
                status=state,
                error={
                    "category": "interrupted",
                    "message": "Task interrupted; paid requests will not be repeated",
                },
            )
            raise
        except Exception as exc:
            error = _error(exc, ambiguous=unresolved)
            pending = int(unresolved and error.ambiguous)
            op = await self.store.get(operation_id)
            state = (
                "needs_reconciliation"
                if pending
                else (
                    "result_unavailable"
                    if actual > len(op["artifacts"])
                    else ("partial" if actual else "failed")
                )
            )
            resolved = op["submitted_count"] if not pending else op["next_index"]
            await self.store.settle(
                operation_id, actual, pending, status=state, error=error.to_dict(), next_index=resolved
            )
            self.diagnostics.add(
                kind="image_result",
                task_id=operation_id,
                status=state,
                generated=actual,
                error=safe_error(error, connection),
                connection=connection_info(connection),
            )
            if unresolved or not actual:
                await self._record_error(connection, capability, error)
            if op["artifacts"] and not pending:
                await self._deliver(operation_id)
        finally:
            self._finish_notice(operation_id)

    async def _materialize(self, operation_id):
        op = await self.store.get(operation_id)
        expected = op["request"]["options"]["output_format"]
        existing = {item["artifact_id"] for item in op["artifacts"]}
        from .models import Actor

        actor = Actor(**op["actor"])
        for receipt in op["receipts"]:
            for index, item in enumerate(receipt["outputs"]):
                identity = f"{operation_id}-{receipt['index']}-{index}"
                if identity in existing:
                    continue
                artifact = await self.assets.materialize(
                    operation_id, receipt["index"], index, item, expected
                )
                await self.store.append(operation_id, "artifacts", asdict(artifact))
                await self.assets.remember_result(actor, artifact)

    async def _recover_images(self, operation_id):
        async with self.image_semaphore:
            op = await self.store.get(operation_id)
            actual = sum(len(r["outputs"]) for r in op["receipts"])
            # Any submitted child without a complete receipt remains uncertain, not regenerated.
            pending = min(1, max(0, op["submitted_count"] - op["next_index"]), op["pending"])
            actual = max(actual, op["actual"])
            try:
                await self.store.settle(operation_id, actual, pending, status="receiving")
                await self._materialize(operation_id)
                op = await self.store.get(operation_id)
                if pending:
                    state = "needs_reconciliation"
                elif len(op["artifacts"]) < actual:
                    state = "result_unavailable"
                else:
                    state = (
                        "cancelled"
                        if op["cancel_requested"]
                        else ("succeeded" if actual >= op["requested"] else "partial")
                    )
                await self.store.settle(operation_id, actual, pending, status=state)
                # Events and platform send acknowledgements cannot be reconstructed safely.
                if op["deliver"] and op["delivery_status"] in ("none", "pending"):
                    await self.store.update(operation_id, delivery_status="unavailable_after_restart")
            except asyncio.CancelledError:
                await self.store.settle(
                    operation_id,
                    actual,
                    pending,
                    status="needs_reconciliation" if pending else "result_unavailable",
                )
                raise
            except Exception as exc:
                await self.store.settle(
                    operation_id,
                    actual,
                    pending,
                    status="needs_reconciliation" if pending else "result_unavailable",
                    error=_error(exc).to_dict(),
                )
            finally:
                self._finish_notice(operation_id)

    async def _deliver(self, operation_id):
        op = await self.store.get(operation_id)
        if (
            not op["deliver"]
            or op["cancel_requested"]
            or not op["artifacts"]
            or op["delivery_status"] not in ("none", "pending")
        ):
            return
        event = self.events.get(operation_id)
        if event is None or self.delivery is None:
            await self.store.update(operation_id, delivery_status="unavailable_after_restart")
            return
        await self.store.update(operation_id, delivery_status="sending")
        try:
            await self.delivery(event, [Artifact(**item) for item in op["artifacts"]])
        except asyncio.CancelledError:
            await self.store.update(operation_id, delivery_status="unknown")
            raise
        except Exception:
            await self.store.update(operation_id, delivery_status="unknown")
        else:
            await self.store.update(operation_id, delivery_status="sent")

    async def _authorized(self, event, operation_id):
        self._ready()
        actor = actor_from_event(event)
        op = await self.store.get(operation_id)
        if op is None or (op["owner"] != actor.owner_key and not actor.is_admin):
            raise NativeError("not_found", "Task not found or inaccessible")
        return op

    async def get_task(self, event, task_id):
        op = await self._authorized(event, task_id)
        return {
            "task_id": op["id"],
            "kind": op["kind"],
            "status": op["status"],
            "generation_status": "unknown"
            if op["pending"] and op["status"] == "needs_reconciliation"
            else (
                "completed"
                if op["actual"] >= op["requested"]
                else ("partial" if op["actual"] else op["status"])
            ),
            "file_status": "saved"
            if op["actual"] and len(op["artifacts"]) >= op["actual"]
            else ("partially_saved" if op["artifacts"] else ("unavailable" if op["actual"] else "none")),
            "quota_status": "partially_settled"
            if op["actual"] and op["pending"]
            else ("held" if op["pending"] else ("settled" if op["actual"] else "released")),
            "requested": op["requested"],
            "generated": op["actual"],
            "held": op["pending"],
            "delivery_status": op["delivery_status"],
            "cancel_requested": bool(op["cancel_requested"]),
            "artifacts": op["artifacts"],
            "error": op["error"],
            "created": op["created"],
            "updated": op["updated"],
        }

    async def _wait(self, task_id, timeout):
        if timeout < 0 or not math.isfinite(timeout):
            raise NativeError("invalid_request", "Invalid wait timeout")
        notice = self._notice(task_id)
        op = await self.store.get(task_id)
        if op["status"] not in ACTIVE:
            self.notifications.pop(task_id, None)
            return
        try:
            await asyncio.wait_for(notice.wait(), timeout)
        except TimeoutError:
            pass

    async def wait_task(self, event, task_id, timeout=30):
        await self._authorized(event, task_id)
        await self._wait(task_id, float(timeout))
        return await self.get_task(event, task_id)

    async def get_results(self, event, task_id):
        op = await self._authorized(event, task_id)
        if any(not Path(item["path"]).is_file() for item in op["artifacts"]):
            raise NativeError("result_expired", "Saved result has expired or is missing")
        return [Artifact(**item) for item in op["artifacts"]]

    async def cancel_task(self, event, task_id):
        op = await self._authorized(event, task_id)
        if op["kind"] != "image":
            raise NativeError("invalid_request", "Only image tasks support cancellation")
        previous = await self.store.request_cancel(task_id)
        if previous == "queued":
            await self.store.settle(task_id, 0, 0, status="cancelled")
            self._finish_notice(task_id)
        return {
            "task_id": task_id,
            "accepted": previous in ACTIVE,
            "remote_cancel_supported": False,
            "message": "Submitted work may finish and consume quota; future children and automatic delivery are stopped",
        }

    async def retry_download(self, event, task_id):
        op = await self._authorized(event, task_id)
        if op["kind"] != "image" or op["status"] in ACTIVE or not op["receipts"]:
            raise NativeError("invalid_request", "No completed response is available for download recovery")
        await self._recover_images(task_id)
        return await self.get_task(event, task_id)

    async def reconcile(self, event, task_id, actual_units):
        actor = actor_from_event(event)
        if not actor.is_admin:
            raise NativeError("permission_denied", "Administrator required")
        op = await self._authorized(event, task_id)
        if (
            op["status"] != "needs_reconciliation"
            or type(actual_units) is not int
            or not op["actual"] <= actual_units <= op["actual"] + op["pending"]
        ):
            raise NativeError(
                "invalid_request", "Specify the verified total within the known plus uncertain count"
            )
        status = "partial" if op["artifacts"] else "failed"
        await self.store.settle(
            task_id,
            actual_units,
            0,
            status=status,
            error={
                "category": "reconciled",
                "message": "Administrator reconciled the actual executed count; no regeneration",
            },
        )
        return await self.get_task(event, task_id)

    async def quota(self, event):
        self._ready()
        return await self.store.quota(actor_from_event(event))

    async def remember_images(self, event):
        self._ready()
        actor = actor_from_event(event)
        await self.assets.from_event(event, actor, current_only=True)

    async def list_image_tasks(self, event, limit=20):
        self._ready()
        actor = actor_from_event(event)
        rows = await self.store.list_images(actor, limit)
        return [
            {
                "task_id": row["id"],
                "status": row["status"],
                "generated": row["actual"],
                "requested": row["requested"],
                "created": row["created"],
            }
            for row in rows
        ]

    async def _feature_readiness(self, event, feature, connection=None):
        cfg = self.settings.feature(feature)
        detail = {
            "mode": cfg["mode"],
            "configuration_ready": False,
            "ready": False,
            "tested": False,
            "capabilities": {},
            "reason": "",
            "reason_code": "",
        }
        if not cfg["enabled"]:
            return {**detail, "reason_code": "disabled", "reason": "此功能已关闭"}
        if feature == "image" and (not isinstance(cfg["model"], str) or not cfg["model"].strip()):
            return {**detail, "reason_code": "missing_image_model", "reason": "生图尚未就绪：请填写图片模型"}
        if cfg["mode"] == "current" and event is None and connection is None:
            return {**detail, "reason_code": "awaiting_session", "reason": "等待会话验证"}
        try:
            connection = connection or await self._connection(event, feature)
        except NativeError as exc:
            return {**detail, "reason_code": exc.category, "reason": exc.message, "error": safe_error(exc)}
        detail.update(connection_info(connection))
        detail["configuration_ready"] = True
        formats, actions = [], []
        names = (
            ["search"]
            if feature == "search"
            else [action + ":" + fmt for action in ("generate", "edit") for fmt in cfg["formats"]]
        )
        for name in names:
            if name.endswith(":gif") and (
                not self.settings.gif["enabled"] or connection.protocol != "images"
            ):
                detail["capabilities"][name] = {
                    "status": "disabled",
                    "reason": "GIF 未启用" if not self.settings.gif["enabled"] else "GIF 需要 Images 协议",
                }
                continue
            row = await self.capabilities.get(connection, name)
            detail["capabilities"][name] = row
            if row["status"] in {"unsupported", "auth_blocked"}:
                continue
            detail["ready"] = True
            if feature == "image":
                action, fmt = name.split(":")
                if action not in actions:
                    actions.append(action)
                if fmt not in formats:
                    formats.append(fmt)
        detail["tested"] = any(row["status"] == "available" for row in detail["capabilities"].values())
        if not detail["ready"]:
            blocked = [
                row
                for row in detail["capabilities"].values()
                if row["status"] in {"unsupported", "auth_blocked"}
            ]
            first = blocked[0] if blocked else {}
            message = first.get("error", {}).get("message") or first.get("reason")
            detail.update(
                reason_code=first.get("status", "capability_blocked"),
                reason=("能力不可用：" + message)
                if message
                else "没有启用的可用格式，请检查格式、GIF 开关和协议",
            )
        elif not detail["tested"]:
            detail.update(reason_code="unverified", reason="配置就绪，账号能力尚未实测")
        detail.update(formats=formats, actions=actions)
        return detail

    async def available_tools(self, event):
        self._ready()
        actor = actor_from_event(event)
        details = {}
        for feature in ("search", "image"):
            try:
                details[feature] = await self._feature_readiness(event, feature)
            except NativeError as exc:
                details[feature] = {
                    "ready": False,
                    "configuration_ready": False,
                    "tested": False,
                    "reason_code": exc.category,
                    "reason": exc.message,
                    "error": safe_error(exc),
                }
            if not details[feature].get("configuration_ready"):
                self.last_connections.pop(feature, None)
            self.last_readiness[feature] = {**details[feature], "observed_at": time.time()}
        tasks = await self.store.list_images(actor, 1)
        return {
            "search": details["search"]["ready"],
            "image": details["image"]["ready"],
            "image_task": bool(tasks),
            "formats": details["image"].get("formats", []),
            "actions": details["image"].get("actions", []),
            "details": details,
        }

    def record_tool_call(self, event, name, state, *, result=None, error=None):
        record = {"kind": "tool_execution", "tool": name, "state": state}
        if result is not None:
            data = result.to_dict() if hasattr(result, "to_dict") else result
            if isinstance(data, dict):
                record.update(
                    {key: data[key] for key in ("task_id", "operation_id", "status") if key in data}
                )
        if error is not None:
            record["error"] = safe_error(error)
        self.diagnostics.add(event, **record)

    def record_request(self, event, before, after, available, inactive=()):
        reasons = {}
        for name, feature in (
            ("openai_web_search", "search"),
            ("openai_image", "image"),
            ("openai_image_task", "image_task"),
        ):
            if name not in before:
                reasons[name] = {"code": "not_in_request", "message": "未进入当前请求，请检查人格和工具开关"}
            elif name in inactive:
                reasons[name] = {"code": "inactive", "message": "AstrBot 已停用此工具"}
            elif name not in after:
                detail = available.get("details", {}).get(feature, {})
                reasons[name] = {
                    "code": detail.get(
                        "reason_code", "no_current_tasks" if feature == "image_task" else "unavailable"
                    ),
                    "message": detail.get(
                        "reason",
                        "当前用户在此会话没有图片任务" if feature == "image_task" else "功能尚未就绪",
                    ),
                }
        self.diagnostics.add(
            event,
            kind="request",
            tools_before=before,
            tools_after=after,
            hidden=reasons,
            features=available.get("details", {}),
        )

    async def diagnostics_snapshot(self):
        self._ready()
        if callable(self.command_conflicts_callback):
            self.command_conflicts_callback()
        features = {}
        for feature in ("search", "image"):
            if self.settings.feature(feature)["mode"] == "current" and feature in self.last_readiness:
                latest = self.last_readiness[feature]
                conn = self.last_connections.get(feature)
                features[feature] = (
                    {
                        **(await self._feature_readiness(None, feature, conn)),
                        "observed_at": latest["observed_at"],
                    }
                    if conn
                    else latest
                )
            else:
                features[feature] = await self._feature_readiness(None, feature)
        return {
            "features": features,
            "recent": self.diagnostics.snapshot(),
            "command_conflicts": self.command_conflicts,
            "notice": "配置检查不调用付费接口；真实账号通过情况来自已完成请求。",
        }

    async def status(self, event):
        self._ready()
        actor = actor_from_event(event)
        if not actor.is_admin:
            raise NativeError("permission_denied", "Administrator required")
        result = {}
        for feature in ("search", "image"):
            try:
                connection = await self._connection(event, feature)
                capabilities = (
                    ["search"]
                    if feature == "search"
                    else [
                        action + ":" + fmt
                        for action in ("generate", "edit")
                        for fmt in self.settings.feature("image")["formats"]
                    ]
                )
                result[feature] = {
                    "provider_id": connection.provider_id,
                    "model": connection.model,
                    "protocol": connection.protocol,
                    "service_type": connection.service_type,
                    "identification": self.resolver.identification(connection),
                    "capabilities": {
                        name: await self.capabilities.get(connection, name) for name in capabilities
                    },
                }
            except NativeError as exc:
                result[feature] = {"error": exc.to_dict()}
        result["diagnostics"] = await self.diagnostics_snapshot()
        return result

    async def probe(self, event, capability, confirm_paid=False):
        if not actor_from_event(event).is_admin:
            raise NativeError("permission_denied", "Administrator required")
        if not confirm_paid:
            raise NativeError(
                "confirmation_required", "This probe makes a paid request; explicitly confirm first"
            )
        if capability == "search":
            connection = await self._connection(event, "search")
            await self.capabilities.reset(connection, "search")
            return (
                await self.search(event, SearchRequest(["OpenAI official website"], max_results=1))
            ).to_dict()
        parts = capability.split(":")
        if (
            len(parts) != 2
            or parts[0] not in ("generate", "edit")
            or parts[1] not in ("png", "jpeg", "webp", "gif")
        ):
            raise NativeError("invalid_request", "Use search, generate:FORMAT or edit:FORMAT")
        connection = await self._connection(event, "image")
        await self.capabilities.reset(connection, capability)
        request = ImageRequest(
            "A blue circle on a white background"
            + (", moving gently in a seamless loop" if parts[1] == "gif" else ""),
            action=parts[0],
            output_format=parts[1],
            size="auto",
            frame_count=2 if parts[1] == "gif" else None,
            frame_duration_ms=200 if parts[1] == "gif" else None,
        )
        return (await self.submit_image(event, request, deliver=False)).to_dict()

    async def _maintenance(self):
        while True:
            await asyncio.sleep(60)
            try:
                await self.assets.cleanup()
            except (OSError, NativeError):
                # Keep active work alive when a cleanup target is temporarily unavailable.
                continue

    async def close(self):
        async with self.submit_lock:
            self._closing = True
        tasks = self.workers + list(self.search_jobs.values()) + list(self.recovery_jobs)
        if hasattr(self, "maintenance"):
            tasks.append(self.maintenance)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.store.db:
            for op in await self.store.list_recovery():
                if op["status"] == "queued":
                    await self.store.settle(op["id"], 0, 0, status="cancelled")
        await self.transport.close()
        await self.store.close()
        self._initialized = False
