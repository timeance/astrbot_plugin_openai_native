from unittest.mock import AsyncMock

from test_service import make_service

from native.diagnostics import Diagnostics
from native.models import ImageBatch, ImageRequest, NativeError, SearchHit, SearchRequest, SearchResult


async def test_readiness_records_independent_failures_and_task_scope(tmp_path, event_factory, png_data):
    service = await make_service(tmp_path)
    event = event_factory()
    service.image_adapter.generate = AsyncMock(return_value=ImageBatch([]))
    try:
        assert not (await service.available_tools(event))["image_task"]
        original = service.resolver.resolve.side_effect

        async def resolve(event, feature):
            if feature == "search":
                raise NativeError("configuration", "Search provider is missing")
            return original(event, feature)

        service.resolver.resolve.side_effect = resolve
        ready = await service.available_tools(event)
        assert not ready["search"] and ready["image"]
        assert ready["details"]["search"]["reason_code"] == "configuration"
        assert not ready["details"]["image"]["tested"]
        task = await service.submit_image(event, ImageRequest("test", action="generate"))
        await service.wait_task(event, task.task_id, 3)
        service.settings.data["image"]["enabled"] = False
        ready = await service.available_tools(event)
        assert not ready["image"] and ready["image_task"]
        assert ready["details"]["image"]["reason_code"] == "disabled"
        assert not (await service.available_tools(event_factory("someone")))["image_task"]
        assert not (await service.available_tools(event_factory(group="another")))["image_task"]
    finally:
        await service.close()


async def test_current_session_diagnostic_waits_then_updates_live_evidence(tmp_path, event_factory):
    service = await make_service(tmp_path)
    service.search_adapter.search = AsyncMock(
        side_effect=lambda c, r, oid: SearchResult(oid, r.queries, [SearchHit("A", "https://example.test")])
    )
    try:
        snapshot = await service.diagnostics_snapshot()
        assert snapshot["features"]["search"]["reason_code"] == "awaiting_session"
        event = event_factory()
        assert (await service.available_tools(event))["search"]
        result = await service.search(event, SearchRequest(["a"]))
        snapshot = await service.diagnostics_snapshot()
        assert snapshot["features"]["search"]["configuration_ready"]
        assert snapshot["features"]["search"]["tested"]
        assert any(row.get("operation_id") == result.operation_id for row in snapshot["recent"])
    finally:
        await service.close()


async def test_request_diagnostic_keeps_exclusions_and_error_secrets_out(tmp_path, event_factory):
    service = await make_service(tmp_path)
    try:
        event = event_factory()
        ready = await service.available_tools(event)
        service.record_request(event, ["openai_image"], ["openai_image"], ready)
        record = service.diagnostics.snapshot()[0]
        assert record["hidden"]["openai_web_search"]["code"] == "not_in_request"
        connection = await service.resolver.resolve(event, "image")
        error = NativeError("upstream", "secret-not-for-storage https://host.test/x?token=secret")
        await service._record_error(connection, "generate:png", error)
        assert "secret-not-for-storage" not in str(service.diagnostics.snapshot())
        assert "token=secret" not in str(service.diagnostics.snapshot())
    finally:
        await service.close()


def test_buffer_is_bounded_and_never_keeps_chat():
    diagnostics = Diagnostics(capacity=3)
    for index in range(10):
        diagnostics.add(kind="tool_execution", tool="search", operation_id=str(index))
    assert len(diagnostics.snapshot()) == 3
    assert diagnostics.snapshot()[0]["operation_id"] == "9"
