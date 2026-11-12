import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from native.config import Settings
from native.identity import actor_from_event
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
        edited = await service.public_api.submit_image(
            event_factory(message="m2"),
            ImageRequest("edit", action="edit", references=[gif_data], output_format="gif"),
        )
        await service.queue.join()
        assert (await service.get_task(event, edited.task_id))["status"] == "succeeded"
        reference = service.image_adapter.generate.await_args.args[2][0]
        from PIL import Image

        assert reference.mime == "image/png"
        with Image.open(reference.path) as image:
            assert image.format == "PNG" and image.n_frames == 1
            assert image.getpixel((0, 0)) == (255, 0, 0, 255)
        assert Path((await service.get_results(event, edited.task_id))[0].path).read_bytes() == gif_data
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
        caller = asyncio.create_task(service.search(event, SearchRequest(["price"], max_results=3)))
        await asyncio.wait_for(started.wait(), 3)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()
        result = await service.search(event, SearchRequest(["price"], max_results=3))
        assert result.sources[0].title == "Official"
        service.search_adapter.search.assert_awaited_once()
        assert all(row["used"] == 1 and row["held"] == 0 for row in (await service.quota(event))["search"])
    finally:
        release.set()
        await service.close()


async def test_search_reuses_same_message_request_and_limits_distinct_calls(tmp_path, event_factory):
    service = await make_service(tmp_path, limits={"search_calls_per_message": 2})
    service.search_adapter.search = AsyncMock(
        side_effect=lambda conn, request, identity: SearchResult(
            identity, request.queries, [SearchHit("Official", "https://example.com")]
        )
    )
    event = event_factory()
    try:
        first = await service.search(event, SearchRequest(["first"], max_results=1))
        repeated = await service.search(event, SearchRequest(["first"], max_results=1))
        await service.search(event, SearchRequest(["followup"], max_results=1))
        with pytest.raises(NativeError, match="Search limit for this message"):
            await service.search(event, SearchRequest(["third"], max_results=1))
        assert first.operation_id == repeated.operation_id
        assert service.search_adapter.search.await_count == 2
    finally:
        await service.close()


async def test_public_search_stays_strict_under_explicit_tool_policy(tmp_path, event_factory):
    service = await make_service(tmp_path, search={"trigger_policy": "explicit"})
    service.search_adapter.search = AsyncMock(
        side_effect=lambda conn, request, identity: SearchResult(identity, request.queries, [])
    )
    try:
        event = event_factory()
        result = await service.public_api.search(event, SearchRequest(["ordinary caller"], max_results=1))
        assert result.status == "succeeded"
        with pytest.raises(NativeError, match="search query count"):
            await service.public_api.search(event, SearchRequest(["first", "second"], max_results=1))
        with pytest.raises(NativeError, match="search result limit"):
            await service.public_api.search(event, SearchRequest(["first"], max_results=4))
        service.search_adapter.search.assert_awaited_once()
    finally:
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


@pytest.mark.parametrize(
    "action, source, expected",
    [
        ("auto", "history", "generate"),
        ("generate", "history", "generate"),
        ("edit", "history", "edit"),
        ("auto", "current", "edit"),
        ("auto", "reply", "edit"),
        ("auto", "asset_id", "edit"),
        ("generate", "current", "generate"),
        ("generate", "reply", "generate"),
    ],
)
async def test_public_image_action_controls_reference_sources(
    tmp_path, event_factory, png_data, action, source, expected
):
    service = await make_service(tmp_path)
    service.image_adapter.generate = AsyncMock(return_value=ImageBatch([RemoteImage(data=png_data)]))
    event = event_factory(message="new-request")
    try:
        cached = await service.assets.import_data(actor_from_event(event), png_data)
        references = []
        if source in ("current", "reply"):
            image = type("Image", (), {})()
            image.convert_to_file_path = AsyncMock(return_value=cached.path)
            event.components = [image] if source == "current" else [type("Reply", (), {"chain": [image]})()]
        elif source == "asset_id":
            references = [cached.asset_id]
        handle = await service.public_api.submit_image(
            event, ImageRequest("画一只蓝色美西螈", action=action, references=references)
        )
        await service.queue.join()
        assert (await service.get_task(event, handle.task_id))["status"] == "succeeded"
        service.image_adapter.generate.assert_awaited_once()
        _, request, assets = service.image_adapter.generate.await_args.args
        assert request.action == expected
        assert handle.accepted["action"] == expected
        assert handle.accepted["reference_count"] == len(assets) == (1 if expected == "edit" else 0)
        if assets:
            assert assets[0].sha256 == cached.sha256
        assert all(row["used"] == 1 and row["held"] == 0 for row in (await service.quota(event))["image"])
    finally:
        await service.close()


@pytest.mark.parametrize("failure", ["platform", "target", "at_all", "download", "file"])
async def test_explicit_avatar_failure_never_falls_back_to_cached_image(
    tmp_path, event_factory, png_data, failure
):
    service = await make_service(tmp_path)
    mention = (
        type("AtAll", (), {})() if failure == "at_all"
        else type("At", (), {"qq": "all" if failure == "target" else "542140488"})()
    )
    event = event_factory(components=[mention])
    event.get_platform_name = lambda: "discord" if failure == "platform" else "aiocqhttp"
    service.transport.download = AsyncMock(
        side_effect=OSError("unavailable") if failure == "file" else NativeError("download_failed", "unavailable")
    )
    service.image_adapter.generate = AsyncMock()
    try:
        await service.assets.import_data(actor_from_event(event), png_data)
        with pytest.raises(NativeError, match="头像"):
            await service.public_api.submit_image(event, ImageRequest("参考用户头像", action="edit"))
        assert not await service.list_image_tasks(event)
        service.image_adapter.generate.assert_not_awaited()
        if failure not in ("download", "file"):
            service.transport.download.assert_not_awaited()
        assert all(row["used"] == row["held"] == 0 for row in (await service.quota(event))["image"])
    finally:
        await service.close()


async def test_quoted_template_and_mentioned_avatar_are_both_edit_references(
    tmp_path, event_factory, png_data
):
    from io import BytesIO

    from PIL import Image

    service = await make_service(tmp_path)
    avatar = BytesIO()
    Image.new("RGB", (32, 32), "red").save(avatar, format="PNG")
    service.transport.download = AsyncMock(return_value=avatar.getvalue())
    service.image_adapter.generate = AsyncMock(return_value=ImageBatch([RemoteImage(data=png_data)]))
    event = event_factory()
    event.get_platform_name = lambda: "aiocqhttp"
    try:
        template = await service.assets.import_data(actor_from_event(event), png_data)
        image = type("Image", (), {})()
        image.convert_to_file_path = AsyncMock(return_value=template.path)
        event.components = [
            type("Reply", (), {"chain": [image]})(),
            type("At", (), {"qq": event.get_self_id()})(),
            type("At", (), {"qq": "542140488"})(),
        ]
        handle = await service.public_api.submit_image(
            event, ImageRequest("参考引用图片，把角色换成所 @ 用户的头像")
        )
        await service.queue.join()
        _, request, assets = service.image_adapter.generate.await_args.args
        assert request.action == "edit" and handle.accepted["reference_count"] == len(assets) == 2
        assert assets[0].sha256 == template.sha256 and assets[1].sha256 != template.sha256
        service.transport.download.assert_awaited_once_with(
            "https://q1.qlogo.cn/g?b=qq&nk=542140488&s=640", service.settings.storage["max_input_bytes"]
        )
    finally:
        await service.close()


@pytest.mark.parametrize(
    "options, message",
    [
        ({"frame_count": 2, "frame_duration_ms": 500}, "Frame settings apply only to GIF"),
        ({"size": "1024x1024", "aspect_ratio": "1:1"}, "Choose size or aspect ratio"),
    ],
)
async def test_public_api_still_rejects_conflicting_arguments_before_reservation(
    tmp_path, event_factory, options, message
):
    service = await make_service(tmp_path)
    event = event_factory()
    try:
        with pytest.raises(NativeError, match=message):
            await service.public_api.submit_image(
                event, ImageRequest("pelican", output_format="png", **options)
            )
        service.resolver.resolve.assert_not_called()
        assert all(row["used"] == row["held"] == 0 for row in (await service.quota(event))["image"])
        assert not (await service.list_image_tasks(event))
    finally:
        await service.close()
