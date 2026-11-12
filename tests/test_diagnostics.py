import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
from test_service import make_service

from native.config import Settings
from native.diagnostics import Diagnostics, message_info
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



def disk_rows(directory):
    return [json.loads(line) for path in directory.glob("diagnostics*.jsonl")
            for line in path.read_text("utf-8").splitlines()]


def test_disk_logging_is_opt_in_and_does_not_backfill_memory(tmp_path):
    directory = tmp_path / "logs"
    diagnostics = Diagnostics(directory=directory)
    diagnostics.add(kind="before_enable", prompt="private chat")
    assert not directory.exists()
    assert diagnostics.snapshot()[0]["kind"] == "before_enable"
    diagnostics.configure(enabled=True, max_bytes=100 * 1024 * 1024)
    diagnostics.add(kind="after_enable")
    assert [row["kind"] for row in disk_rows(directory)] == ["after_enable"]
    assert diagnostics.logging_status()["max_mb"] == 100


def test_rotation_counts_utf8_bytes_and_survives_restart(tmp_path):
    diagnostics = Diagnostics(directory=tmp_path)
    diagnostics.configure(enabled=True, max_bytes=8000)
    for sequence in range(100):
        diagnostics.add(kind="tool_execution", sequence=sequence, error={"message": "测" * 200})
        assert sum(path.stat().st_size for path in tmp_path.glob("diagnostics*.jsonl")) <= 8000
    assert len(list(tmp_path.glob("diagnostics*.jsonl"))) <= 5
    rows = disk_rows(tmp_path)
    assert any(row["sequence"] == 99 for row in rows)
    assert min(row["sequence"] for row in rows) > 0
    restarted = Diagnostics(directory=tmp_path)
    restarted.configure(enabled=True, max_bytes=8000)
    restarted.add(kind="restarted")
    assert any(row.get("sequence") == 99 for row in disk_rows(tmp_path))
    assert restarted.logging_status()["error"] is None


def test_disable_preserves_files_and_reduced_budget_prunes_oldest(tmp_path):
    diagnostics = Diagnostics(directory=tmp_path)
    diagnostics.configure(enabled=True, max_bytes=8000)
    for sequence in range(40):
        diagnostics.add(kind="task", sequence=sequence, error={"message": "x" * 600})
    before = {path.name: path.read_bytes() for path in tmp_path.glob("diagnostics*.jsonl")}
    diagnostics.configure(enabled=False, max_bytes=8000)
    diagnostics.add(kind="after_disable")
    assert before == {path.name: path.read_bytes() for path in tmp_path.glob("diagnostics*.jsonl")}
    diagnostics.configure(enabled=True, max_bytes=2000)
    assert diagnostics.logging_status()["total_bytes"] <= 2000
    diagnostics.add(kind="newest_after_shrink")
    assert any(row["kind"] == "newest_after_shrink" for row in disk_rows(tmp_path))
    assert diagnostics.logging_status()["total_bytes"] <= 2000


def test_disk_records_drop_chat_and_redact_credentials(tmp_path):
    diagnostics = Diagnostics(directory=tmp_path)
    diagnostics.configure(enabled=True, max_bytes=1024 * 1024)
    diagnostics.remember_connection(SimpleNamespace(
        key="ordinary-provider-credential", headers={"X-Secret": "header-credential"}, proxy=""
    ))
    diagnostics.add(
        kind="tool_execution", prompt="PRIVATE_CHAT", messages=["PRIVATE_HISTORY"],
        metadata={"api_key": "API_SECRET", "client_secret": "CLIENT_SECRET"},
        error={"message": "ordinary-provider-credential header-credential Bearer BEARER_SECRET "
               "sk-EXAMPLE_SECRET https://host.test/v1?token=URL_SECRET token=ASSIGNED_SECRET"},
    )
    saved = (tmp_path / "diagnostics.jsonl").read_text("utf-8")
    memory = json.dumps(diagnostics.snapshot())
    for secret in ("PRIVATE_CHAT", "PRIVATE_HISTORY", "API_SECRET", "CLIENT_SECRET",
                   "ordinary-provider-credential", "header-credential", "BEARER_SECRET",
                   "sk-EXAMPLE_SECRET", "URL_SECRET", "ASSIGNED_SECRET"):
        assert secret not in saved and secret not in memory
    assert disk_rows(tmp_path)[0]["kind"] == "tool_execution"


def test_oversized_records_are_bounded_and_write_errors_do_not_raise(tmp_path):
    diagnostics = Diagnostics(directory=tmp_path / "logs")
    diagnostics.configure(enabled=True, max_bytes=1024 * 1024)
    diagnostics.add(kind="large", entries=["测" * 1000 for _ in range(64)])
    assert disk_rows(tmp_path / "logs")[0]["truncated"] is True
    blocked = tmp_path / "blocked"
    blocked.write_text("existing file", "utf-8")
    diagnostics = Diagnostics(directory=blocked)
    diagnostics.configure(enabled=True, max_bytes=1024 * 1024)
    diagnostics.add(kind="still_in_memory")
    assert diagnostics.snapshot()[0]["kind"] == "still_in_memory"
    assert diagnostics.logging_status()["error"]
    assert blocked.read_text("utf-8") == "existing file"


@pytest.mark.parametrize("values", [
    {"enabled": 1}, {"max_mb": 0}, {"max_mb": True}, {"max_mb": 1.5}, {"max_mb": 10001},
])
def test_diagnostic_setting_bounds(values):
    with pytest.raises(NativeError, match="Invalid plugin configuration"):
        Settings({"diagnostics": values})


def test_message_evidence_keeps_command_boundary_and_handlers_without_text():
    extras = {"activated_handlers": [SimpleNamespace(handler_full_name="native.image_command")]}
    event = SimpleNamespace(
        get_message_str=lambda: "生图，根据PRIVATE_PROMPT生成图片",
        get_extra=lambda name: extras.get(name),
        get_messages=lambda: [type("Image", (), {})(), type("Reply", (), {})()],
        is_at_or_wake_command=True, is_wake=True, trace=SimpleNamespace(trace_id="trace-123"),
    )
    result = message_info(event)
    assert result["command_hint"] == "生图"
    assert result["command_boundary"] == "punctuation"
    assert result["separator_codepoint"] == "U+FF0C"
    assert result["image_count"] == 1 and result["has_reply"]
    assert result["activated_handlers"] == ["native.image_command"]
    assert result["trace_id"] == "trace-123"
    assert "PRIVATE_PROMPT" not in json.dumps(result)


async def test_service_hot_logging_records_requests_and_known_secret_redaction(tmp_path, event_factory):
    service = await make_service(tmp_path)
    try:
        service.configure_diagnostics({"enabled": True, "max_mb": 1})
        event = event_factory()
        ready = await service.available_tools(event)
        service.record_request(event, ["openai_image"], ["openai_image"], ready)
        service.record_tool_call(event, "openai_image", "failed", error=NativeError(
            "upstream", "secret-not-for-storage failed"
        ))
        snapshot = await service.diagnostics_snapshot()
        assert snapshot["logging"]["enabled"]
        assert snapshot["logging"]["max_mb"] == 1
        assert snapshot["runtime"]["plugin_version"]
        assert len(snapshot["runtime"]["bridge_sha256"]) == 16
        rows = disk_rows(tmp_path / "diagnostics")
        assert any(row["kind"] == "runtime" for row in rows)
        assert any(row["kind"] == "request" for row in rows)
        assert "secret-not-for-storage" not in json.dumps(rows)
    finally:
        await service.close()


def test_export_archive_contains_all_rotated_logs_after_restart_and_disable(tmp_path):
    diagnostics = Diagnostics(directory=tmp_path)
    diagnostics.configure(enabled=True, max_bytes=8000)
    for sequence in range(30):
        diagnostics.add(kind="task", sequence=sequence, error={"message": "x" * 600})
    before = {path.name: path.read_bytes() for path in tmp_path.glob("diagnostics*.jsonl")}
    assert len(before) > 1
    restarted = Diagnostics(directory=tmp_path)
    with restarted.export_archive() as stream, ZipFile(stream) as archive:
        assert set(archive.namelist()) == {*before, "runtime.json"}
        for name, data in before.items():
            assert archive.read(name) == data
        assert json.loads(archive.read("runtime.json"))["runtime"]["plugin_version"]
    assert before == {path.name: path.read_bytes() for path in tmp_path.glob("diagnostics*.jsonl")}


def test_export_archive_without_logs_reports_missing(tmp_path):
    diagnostics = Diagnostics(directory=tmp_path)
    with pytest.raises(FileNotFoundError):
        diagnostics.export_archive()


def test_export_archive_excludes_other_files(tmp_path):
    diagnostics = Diagnostics(directory=tmp_path)
    diagnostics.configure(enabled=True, max_bytes=1024 * 1024)
    diagnostics.add(kind="task", error={"message": "Bearer PRIVATE_TOKEN"})
    (tmp_path / "unrelated.txt").write_text("PRIVATE_FILE", "utf-8")
    with diagnostics.export_archive() as stream, ZipFile(stream) as archive:
        assert set(archive.namelist()) == {"diagnostics.jsonl", "runtime.json"}
        assert b"PRIVATE_TOKEN" not in archive.read("diagnostics.jsonl")
