import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from native.config import Settings
from native.models import (
    Connection,
    ImageBatch,
    ImageRequest,
    NativeError,
    RemoteImage,
    SearchHit,
    SearchRequest,
    SearchResult,
)
from native.service import NativeService


async def make_service(path, **overrides):
    values = {"limits": {"min_interval_seconds": 0}, "image": {"model": "image-model"}}
    for key, value in overrides.items():
        values.setdefault(key, {}).update(value)
    settings = Settings(values)

    async def no_network(request):
        raise AssertionError("Unexpected network request")

    service = NativeService(None, settings, path, transport=httpx.MockTransport(no_network))
    await service.initialize()
    connection = Connection(
        "provider-A",
        "https://example.com/v1",
        "secret-not-for-storage",
        model="image-model",
        host_model="host-model",
        protocol="images",
        fingerprint="fingerprint-A",
    )
    service.resolver.resolve = AsyncMock(
        side_effect=lambda event, feature: (
            replace(connection, protocol="responses", model="search-model")
            if feature == "search"
            else connection
        )
    )
    return service


@pytest.mark.asyncio
async def test_public_api_never_delivers_and_duplicate_does_not_charge(tmp_path, event_factory, png_data):
    service = await make_service(tmp_path)
    service.delivery = AsyncMock()
    service.image_adapter.generate = AsyncMock(return_value=ImageBatch([RemoteImage(data=png_data)]))
    event = event_factory()
    try:
        first = await service.public_api.submit_image(event, ImageRequest("a blue circle", action="generate"))
        second = await service.public_api.submit_image(
            event, ImageRequest("a blue circle", action="generate")
        )
        assert first.task_id == second.task_id
        await service.wait_task(event, first.task_id, 3)
        await service.queue.join()
        result = await service.public_api.get_results(event, first.task_id)
        assert len(result) == 1
        assert result[0].mime == "image/png"
        service.image_adapter.generate.assert_awaited_once()
        service.delivery.assert_not_awaited()
        quota = await service.quota(event)
        assert all(row["used"] == 1 and row["held"] == 0 for row in quota["image"])
        with pytest.raises(NativeError, match="inaccessible"):
            await service.get_task(event_factory("another"), first.task_id)
        assert "secret-not-for-storage" not in (tmp_path / "operations.sqlite3").read_bytes().decode("latin1")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_download_recovery_never_repeats_paid_post(tmp_path, event_factory, png_data):
    service = await make_service(tmp_path)
    service.image_adapter.generate = AsyncMock(
        return_value=ImageBatch([RemoteImage(url="https://cdn.example/image?signature=secret")])
    )
    service.transport.download = AsyncMock(side_effect=NativeError("download_failed", "Download unavailable"))
    event = event_factory()
    try:
        handle = await service.submit_image(event, ImageRequest("circle", action="generate"))
        state = await service.wait_task(event, handle.task_id, 3)
        assert state["status"] == "result_unavailable"
        assert state["generated"] == 1 and state["held"] == 0
        service.transport.download = AsyncMock(return_value=png_data)
        state = await service.retry_download(event, handle.task_id)
        assert state["status"] == "succeeded"
        service.image_adapter.generate.assert_awaited_once()
        assert "signature" not in str(state)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_unknown_submission_holds_only_uncertain_child_until_reconcile(tmp_path, event_factory):
    service = await make_service(tmp_path)
    service.image_adapter.generate = AsyncMock(
        side_effect=NativeError("timeout", "Completion unknown", ambiguous=True)
    )
    event = event_factory()
    try:
        handle = await service.submit_image(event, ImageRequest("circle", action="generate", n=3))
        state = await service.wait_task(event, handle.task_id, 3)
        assert state["status"] == "needs_reconciliation"
        assert state["held"] == 1 and state["generated"] == 0
        service.image_adapter.generate.assert_awaited_once()
        with pytest.raises(NativeError, match="Administrator"):
            await service.reconcile(event, handle.task_id, 1)
        await service.reconcile(event_factory(admin=True), handle.task_id, 1)
        quota = await service.quota(event)
        assert all(row["used"] == 1 and row["held"] == 0 for row in quota["image"])
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancel_queued_releases_and_running_stops_future_children(tmp_path, event_factory, png_data):
    service = await make_service(tmp_path, limits={"image_concurrency": 1})
    started, release = asyncio.Event(), asyncio.Event()

    async def generate(*args):
        started.set()
        await release.wait()
        return ImageBatch([RemoteImage(data=png_data)])

    service.image_adapter.generate = AsyncMock(side_effect=generate)
    service.delivery = AsyncMock()
    event = event_factory()
    other = event_factory("user2", message="m2")
    try:
        first = await service.submit_image(
            event, ImageRequest("circle", action="generate", n=2), deliver=True
        )
        await asyncio.wait_for(started.wait(), 3)
        queued = await service.submit_image(other, ImageRequest("square", action="generate"))
        await service.cancel_task(other, queued.task_id)
        assert (await service.get_task(other, queued.task_id))["status"] == "cancelled"
        await service.cancel_task(event, first.task_id)
        release.set()
        await service.queue.join()
        state = await service.get_task(event, first.task_id)
        assert state["status"] == "cancelled"
        assert state["generated"] == 1 and state["held"] == 0
        service.image_adapter.generate.assert_awaited_once()
        service.delivery.assert_not_awaited()
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
async def test_user_quota_crosses_groups_private_is_independent(tmp_path, event_factory, png_data):
    service = await make_service(tmp_path, limits={"user_image": 1, "private_image": 1})
    service.image_adapter.generate = AsyncMock(return_value=ImageBatch([RemoteImage(data=png_data)]))
    try:
        first = await service.submit_image(event_factory(), ImageRequest("circle", action="generate"))
        await service.queue.join()
        with pytest.raises(NativeError) as caught:
            await service.submit_image(
                event_factory(group="group2", message="m2"), ImageRequest("square", action="generate")
            )
        assert caught.value.category == "quota_exceeded"
        private = await service.submit_image(
            event_factory(group="", message="m3"), ImageRequest("triangle", action="generate")
        )
        await service.queue.join()
        assert first.task_id != private.task_id
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_gif_keeps_original_bytes_and_per_call_frame_settings(tmp_path, event_factory, gif_data):
    service = await make_service(tmp_path, gif={"enabled": True})
    service.image_adapter.generate = AsyncMock(
        return_value=ImageBatch([RemoteImage(data=gif_data, output_format="gif")])
    )
    try:
        event = event_factory()
        handle = await service.submit_image(
            event,
            ImageRequest(
                "moving circle", output_format="gif", action="generate", frame_count=2, frame_duration_ms=25
            ),
        )
        await service.queue.join()
        artifacts = await service.get_results(event, handle.task_id)
        assert artifacts[0].frame_count == 2
        assert artifacts[0].mime == "image/gif"
        from pathlib import Path

        assert Path(artifacts[0].path).read_bytes() == gif_data
        request = service.image_adapter.generate.await_args.args[1]
        assert request.frame_duration_ms == 30
        with pytest.raises(NativeError) as caught:
            await service.public_api.submit_image(
                event_factory(message="m2"), ImageRequest("edit", action="edit", references=[gif_data])
            )
        assert caught.value.category == "animated_input_unsupported"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_restart_does_not_resubmit_unknown_job(tmp_path, event_factory):
    service = await make_service(tmp_path)
    started = asyncio.Event()

    async def generate(*args):
        started.set()
        await asyncio.Event().wait()

    service.image_adapter.generate = AsyncMock(side_effect=generate)
    event = event_factory()
    handle = await service.submit_image(event, ImageRequest("circle", action="generate"))
    await asyncio.wait_for(started.wait(), 3)
    await service.close()
    service.image_adapter.generate.assert_awaited_once()
    restarted = await make_service(tmp_path)
    restarted.image_adapter.generate = AsyncMock(side_effect=AssertionError("Must not resubmit"))
    try:
        state = await restarted.get_task(event, handle.task_id)
        assert state["status"] == "needs_reconciliation" and state["held"] == 1
        restarted.image_adapter.generate.assert_not_awaited()
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_search_success_and_inflight_caller_cancellation(tmp_path, event_factory):
    service = await make_service(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()

    async def search(conn, request, identity):
        started.set()
        await release.wait()
        return SearchResult(identity, request.queries, [SearchHit("Official", "https://example.com")])

    service.search_adapter.search = AsyncMock(side_effect=search)
    event = event_factory()
    try:
        caller = asyncio.create_task(service.search(event, SearchRequest(["price"])))
        await asyncio.wait_for(started.wait(), 3)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()
        result = await service.search(event, SearchRequest(["price"]))
        assert result.sources[0].title == "Official"
        service.search_adapter.search.assert_awaited_once()
        assert all(row["used"] == 1 and row["held"] == 0 for row in (await service.quota(event))["search"])
    finally:
        release.set()
        await service.close()


async def test_duplicate_auto_request_after_result_cache_changes_reuses_task(
    tmp_path, event_factory, png_data
):
    service = await make_service(tmp_path)
    service.image_adapter.generate = AsyncMock(return_value=ImageBatch([RemoteImage(data=png_data)]))
    event = event_factory()
    try:
        first = await service.submit_image(event, ImageRequest("a blue circle"))
        await service.queue.join()
        repeated = await service.submit_image(event, ImageRequest("a blue circle"))
        assert repeated.task_id == first.task_id
        service.image_adapter.generate.assert_awaited_once()
        assert (await service.get_task(event, first.task_id))["quota_status"] == "settled"
    finally:
        await service.close()
