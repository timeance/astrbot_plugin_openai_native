import asyncio
import base64

import httpx
import pytest
from test_service import make_service

from native.models import ImageRequest, NativeError, SearchRequest


async def _assert_quota(service, event, kind, used, held):
    buckets = (await service.quota(event))[kind]
    assert {bucket["scope"] for bucket in buckets} == {"user", "group"}
    assert all(bucket["used"] == used and bucket["held"] == held for bucket in buckets)


@pytest.mark.asyncio
async def test_search_cache_write_failure_does_not_refund_success(tmp_path, event_factory, monkeypatch):
    service = await make_service(tmp_path)
    event = event_factory()
    posts = []

    def respond(request):
        assert request.method == "POST" and request.url.path == "/v1/responses"
        posts.append(request)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "web_search_call",
                        "status": "completed",
                        "action": {"sources": [{"title": "Source", "url": "https://source.example/a"}]},
                    }
                ],
            },
        )

    def fail_cache_write(records):
        raise OSError("Simulated capability cache write failure")

    monkeypatch.setattr(service.transport, "_injected_transport", httpx.MockTransport(respond))
    monkeypatch.setattr(service.capabilities, "_write", fail_cache_write)
    try:
        request = SearchRequest(["query"])
        result = await service.search(event, request)
        duplicate = await service.search(event, request)

        assert result.sources[0].url == "https://source.example/a"
        assert duplicate.operation_id == result.operation_id
        op = await service.store.get(result.operation_id)
        assert op["status"] == "succeeded"
        assert (op["actual"], op["pending"]) == (1, 0)
        assert op["result"] and op["error"] is None
        await _assert_quota(service, event, "search", 1, 0)
        assert len(posts) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_restart_without_receipt_does_not_hold_paid_image_twice(
    tmp_path,
    event_factory,
    png_data,
    monkeypatch,
):
    service = await make_service(tmp_path)
    event = event_factory()
    posts = []

    def respond(request):
        assert request.method == "POST" and request.url.path == "/v1/images/generations"
        posts.append(request)
        return httpx.Response(
            200,
            json={
                "data": [{"b64_json": base64.b64encode(png_data).decode("ascii")}],
            },
        )

    async def fail_receipt(operation_id, index, batch):
        op = await service.store.get(operation_id)
        assert (op["actual"], op["pending"], op["next_index"]) == (1, 0, 1)
        raise NativeError("storage_full", "Simulated receipt write failure")

    monkeypatch.setattr(service.transport, "_injected_transport", httpx.MockTransport(respond))
    monkeypatch.setattr(service.assets, "receipt", fail_receipt)
    try:
        handle = await service.submit_image(event, ImageRequest("circle", action="generate"))
        await asyncio.wait_for(service.queue.join(), 5)
        op = await service.store.get(handle.task_id)
        assert op["status"] == "result_unavailable"
        assert op["receipts"] == []
        assert (op["actual"], op["pending"]) == (1, 0)
        await _assert_quota(service, event, "image", 1, 0)
    finally:
        await service.close()

    restarted = await make_service(tmp_path)
    monkeypatch.setattr(restarted.transport, "_injected_transport", httpx.MockTransport(respond))
    try:
        op = await restarted.store.get(handle.task_id)
        assert op["status"] == "result_unavailable"
        assert (op["actual"], op["pending"]) == (1, 0)
        assert (op["submitted_count"], op["next_index"]) == (1, 1)
        assert op["receipts"] == []
        await _assert_quota(restarted, event, "image", 1, 0)
        assert len(posts) == 1
    finally:
        await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("second_status", [400, 504], ids=["rejected", "reconciled"])
async def test_download_retry_does_not_rehold_resolved_child(
    tmp_path,
    event_factory,
    png_data,
    monkeypatch,
    second_status,
):
    service = await make_service(tmp_path)
    event = event_factory()
    posts = []

    def respond(request):
        assert request.method == "POST" and request.url.path == "/v1/images/generations"
        posts.append(request)
        if len(posts) == 1:
            return httpx.Response(
                200,
                json={
                    "data": [{"b64_json": base64.b64encode(png_data).decode("ascii")}],
                },
            )
        assert len(posts) == 2, "Recovery must not submit another paid request"
        return httpx.Response(
            second_status,
            json={
                "error": {"message": "Simulated second-child failure"},
            },
        )

    monkeypatch.setattr(service.transport, "_injected_transport", httpx.MockTransport(respond))
    try:
        handle = await service.submit_image(event, ImageRequest("circle", action="generate", n=2))
        await asyncio.wait_for(service.queue.join(), 5)
        op = await service.store.get(handle.task_id)
        assert op["actual"] == 1 and len(op["receipts"]) == 1
        assert op["submitted_count"] == 2
        if second_status == 400:
            assert op["status"] == "partial"
            assert (op["pending"], op["next_index"]) == (0, 2)
        else:
            assert op["status"] == "needs_reconciliation"
            assert (op["pending"], op["next_index"]) == (1, 1)
            await _assert_quota(service, event, "image", 1, 1)
            await service.reconcile(event_factory(admin=True), handle.task_id, 1)
        await _assert_quota(service, event, "image", 1, 0)

        original_results = await service.get_results(event, handle.task_id)
        assert len(original_results) == 1
        for _ in range(2):
            state = await service.retry_download(event, handle.task_id)
            assert state["status"] == "partial"
            assert (state["generated"], state["held"]) == (1, 0)
            assert await service.get_results(event, handle.task_id) == original_results
            await _assert_quota(service, event, "image", 1, 0)
        assert len(posts) == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_completed_search_shutdown_does_not_rehold_quota_after_restart(
    tmp_path,
    event_factory,
    monkeypatch,
):
    service = await make_service(tmp_path)
    event = event_factory()
    observing = asyncio.Event()
    posts = []

    def respond(request):
        assert request.method == "POST" and request.url.path == "/v1/responses"
        posts.append(request)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "web_search_call",
                        "status": "completed",
                        "action": {"type": "search"},
                    }
                ],
            },
        )

    async def pause_observation(*args, **kwargs):
        observing.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service.transport, "_injected_transport", httpx.MockTransport(respond))
    monkeypatch.setattr(service.capabilities, "record", pause_observation)
    caller = asyncio.create_task(service.search(event, SearchRequest(["query"])))
    try:
        await asyncio.wait_for(observing.wait(), 5)
        operation_id = next(iter(service.search_jobs))
        op = await service.store.get(operation_id)
        assert op["status"] == "succeeded"
        assert (op["actual"], op["pending"]) == (1, 0)
        await _assert_quota(service, event, "search", 1, 0)
    finally:
        await service.close()
        await asyncio.gather(caller, return_exceptions=True)

    restarted = await make_service(tmp_path)
    monkeypatch.setattr(restarted.transport, "_injected_transport", httpx.MockTransport(respond))
    try:
        op = await restarted.store.get(operation_id)
        assert op["result"]
        assert (op["actual"], op["pending"]) == (1, 0)
        await _assert_quota(restarted, event, "search", 1, 0)
        assert len(posts) == 1
    finally:
        await restarted.close()
