import asyncio
from unittest.mock import AsyncMock

import pytest
from test_service import make_service

from native.config import Settings
from native.identity import actor_from_event
from native.models import Connection, ImageRequest, NativeError
from native.store import Store


async def test_concurrent_reservations_across_connections_are_atomic(tmp_path, event_factory):
    settings = Settings({"limits": {"min_interval_seconds": 0, "user_search": 1}})
    first, second = Store(tmp_path / "ledger.sqlite", settings), Store(tmp_path / "ledger.sqlite", settings)
    await first.initialize()
    await second.initialize()
    actor = actor_from_event(event_factory())
    conn = Connection("p", "https://example.com/v1", "secret", fingerprint="fp")

    async def reserve(store, identity):
        return await store.reserve(
            operation_id=identity,
            kind="search",
            actor=actor,
            request={},
            connection=conn,
            units=1,
            idempotency_key=identity,
            request_hash=identity,
        )

    try:
        results = await asyncio.gather(reserve(first, "a"), reserve(second, "b"), return_exceptions=True)
        assert sum(isinstance(result, NativeError) for result in results) == 1
        quota = await first.quota(actor)
        assert quota["search"][0]["held"] == 1
        assert quota["search"][1]["held"] == 1
        identity = next(result[0]["id"] for result in results if not isinstance(result, Exception))
        first.day = lambda: "2099-01-01"
        await first.settle(identity, 1, 0, status="succeeded")
        await first.settle(identity, 1, 0, status="succeeded")
        assert all(row["used"] == row["held"] == 0 for row in (await first.quota(actor))["search"])
        async with first.db.execute("SELECT used,held FROM quota") as cursor:
            assert all(tuple(row) == (1, 0) for row in await cursor.fetchall())
    finally:
        await first.close()
        await second.close()


async def test_reference_cache_is_scoped_and_explicit_reply_is_allowed(tmp_path, event_factory, png_data):
    service = await make_service(tmp_path)
    actor = actor_from_event(event_factory())
    try:
        asset = await service.assets.import_data(actor, png_data)
        request = ImageRequest("edit", action="edit")
        assert (await service.assets.prepare(event_factory(), actor, request))[0].sha256 == asset.sha256
        for event in (event_factory("other"), event_factory(group="group2")):
            with pytest.raises(NativeError):
                await service.assets.prepare(event, actor_from_event(event), request)
            with pytest.raises(NativeError):
                await service.assets.explicit(
                    actor_from_event(event), asset.asset_id, allow_local_paths=False
                )
        Image = type("Image", (), {})
        component = Image()
        component.convert_to_file_path = AsyncMock(return_value=asset.path)
        Reply = type("Reply", (), {})
        reply = Reply()
        reply.chain = [component]
        event = event_factory("other", components=[reply])
        assert len(await service.assets.prepare(event, actor_from_event(event), request)) == 1
        reply.chain = []
        service.assets._quoted_images = AsyncMock(return_value=[asset.path])
        assert len(await service.assets.from_event(event, actor_from_event(event))) == 1
        service.assets._quoted_images.assert_awaited_once_with(event, reply)
    finally:
        await service.close()


async def test_current_image_cache_preserves_reference_before_source_is_removed(
    tmp_path, event_factory, png_data
):
    service = await make_service(tmp_path)
    original = tmp_path / "platform-temp.png"
    original.write_bytes(png_data)
    component = type("Image", (), {})()
    component.convert_to_file_path = AsyncMock(return_value=str(original))
    event = event_factory(components=[component])
    try:
        await service.remember_images(event)
        original.unlink()
        later = event_factory(message="next-message")
        refs = await service.assets.prepare(
            later, actor_from_event(later), ImageRequest("watercolor", action="edit")
        )
        assert len(refs) == 1 and refs[0].path != str(original)
    finally:
        await service.close()


@pytest.mark.parametrize(
    "section,value",
    [
        ("limits", {"queue_size": 0}),
        ("storage", {"artifact_days": 31}),
        ("search", {"max_results": 3}),
        ("image", {"max_outputs": 0}),
        ("network", {"download_timeout": float("nan")}),
    ],
)
def test_invalid_configuration_fails_before_execution(section, value):
    with pytest.raises(NativeError) as caught:
        Settings({section: value})
    assert caught.value.category == "configuration"
