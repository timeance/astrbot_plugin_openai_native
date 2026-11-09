# SPDX-License-Identifier: Apache-2.0
import asyncio
import json
from dataclasses import replace

import pytest

from native import capabilities
from native.capabilities import CapabilityRegistry
from native.models import Connection, NativeError


def connection(**values):
    return Connection(
        **{
            "provider_id": "A",
            "base_url": "https://gateway.invalid/v1",
            "key": "credential-secret",
            "model": "image-model",
            "host_model": "host-model",
            "protocol": "images",
            "fingerprint": "credential-model-protocol-HMAC",
            **values,
        }
    )


@pytest.mark.asyncio
async def test_unknown_is_unverified_and_does_not_block_or_create_a_file(tmp_path):
    path = tmp_path / "caps.json"
    registry = CapabilityRegistry(path)
    await registry.initialize()
    assert (await registry.get(connection(), "search"))["status"] == "unverified"
    await registry.ensure(connection(), "search")
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["available", "unverified", "temporary_failure"])
async def test_permitted_statuses_survive_restart(tmp_path, status):
    path = tmp_path / "caps.json"
    registry = CapabilityRegistry(path)
    await registry.record(connection(), "generate:png", status, "observed")
    restarted = CapabilityRegistry(path)
    row = await restarted.get(connection(), "generate:png")
    assert row["status"] == status
    await restarted.ensure(connection(), "generate:png")
    row["status"] = "unsupported"
    assert (await restarted.get(connection(), "generate:png"))["status"] == status


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unsupported", "auth_blocked"])
async def test_only_explicit_negative_statuses_block(tmp_path, status):
    registry = CapabilityRegistry(tmp_path / "caps.json")
    await registry.record(connection(), "edit:gif", status, "explicit_rejection")
    with pytest.raises(NativeError) as caught:
        await registry.ensure(connection(), "edit:gif")
    assert caught.value.category == status
    await registry.ensure(connection(), "edit:png")
    await registry.ensure(connection(), "generate:gif")


@pytest.mark.asyncio
async def test_temporary_failures_expire_without_reclassifying_http_errors(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(capabilities.time, "time", lambda: now[0])
    registry = CapabilityRegistry(tmp_path / "caps.json")
    error = NativeError("upstream", "No channel available", status_code=403, upstream_code="no_channel")
    await registry.record(connection(), "search", "temporary_failure", "upstream_error", error)
    await registry.ensure(connection(), "search")
    assert (await registry.get(connection(), "search"))["status"] == "temporary_failure"
    now[0] = 1061
    row = await registry.get(connection(), "search")
    assert row["status"] == "unverified"
    await registry.ensure(connection(), "search")


@pytest.mark.asyncio
async def test_identity_isolates_credentials_models_protocols_and_formats(tmp_path):
    registry = CapabilityRegistry(tmp_path / "caps.json")
    original = connection()
    await registry.record(original, "edit:gif", "unsupported", "format_not_supported")
    for changed in [
        replace(original, key="new-key", fingerprint="new-credential-HMAC"),
        replace(original, model="other-image"),
        replace(original, host_model="other-host"),
        replace(original, protocol="responses"),
    ]:
        assert (await registry.get(changed, "edit:gif"))["status"] == "unverified"
        await registry.ensure(changed, "edit:gif")
    for capability in ["edit:png", "generate:gif", "search"]:
        assert (await registry.get(original, capability))["status"] == "unverified"


@pytest.mark.asyncio
async def test_persistence_contains_no_credentials_but_keeps_redacted_upstream_text(tmp_path):
    path = tmp_path / "caps.json"
    conn = connection(headers={"X-Secret": "header-secret"}, proxy="http://user:proxy-secret@proxy.invalid")
    registry = CapabilityRegistry(path)
    error = NativeError(
        "upstream",
        "echo credential-secret",
        status_code=401,
        upstream_type="header-secret",
        upstream_code="credential-secret",
        request_id="credential-secret",
    )
    await registry.record(conn, "search", "auth_blocked", "credential-secret", error)
    encoded = path.read_text()
    for secret in [
        "credential-secret",
        "header-secret",
        "proxy-secret",
        "gateway.invalid",
        "credential-model-protocol-HMAC",
    ]:
        assert secret not in encoded
    assert (await registry.get(conn, "search"))["reason"] == "observation"
    assert json.loads(encoded)["version"] == 1


@pytest.mark.asyncio
async def test_failed_atomic_replace_keeps_previous_file_and_memory(tmp_path, monkeypatch):
    path = tmp_path / "caps.json"
    registry = CapabilityRegistry(path)
    await registry.record(connection(), "search", "available", "verified")
    before = path.read_bytes()

    def fail_replace(*args):
        raise OSError("mock disk failure")

    monkeypatch.setattr(capabilities.os, "replace", fail_replace)
    with pytest.raises(NativeError) as caught:
        await registry.record(connection(), "search", "unsupported", "explicit_rejection")
    assert caught.value.category == "storage"
    assert path.read_bytes() == before
    assert (await registry.get(connection(), "search"))["status"] == "available"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.asyncio
async def test_concurrent_records_are_not_lost(tmp_path):
    path = tmp_path / "caps.json"
    registry = CapabilityRegistry(path)
    await asyncio.gather(
        *[
            registry.record(connection(model=f"model-{index}"), "search", "available", "verified")
            for index in range(10)
        ]
    )
    restarted = CapabilityRegistry(path)
    for index in range(10):
        assert (await restarted.get(connection(model=f"model-{index}"), "search"))["status"] == "available"
    assert len(json.loads(path.read_text())["records"]) == 10


@pytest.mark.asyncio
async def test_corrupt_storage_is_typed_and_not_overwritten(tmp_path):
    path = tmp_path / "caps.json"
    path.write_text("{broken json")
    registry = CapabilityRegistry(path)
    with pytest.raises(NativeError) as caught:
        await registry.record(connection(), "search", "available", "verified")
    assert caught.value.category == "storage"
    assert path.read_text() == "{broken json"


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["edit:bmp", "generate", "SEARCH", "../search"])
async def test_invalid_capability_keys_are_configuration_errors(tmp_path, capability):
    with pytest.raises(NativeError) as caught:
        await CapabilityRegistry(tmp_path / "caps.json").get(connection(), capability)
    assert caught.value.category == "configuration"


@pytest.mark.asyncio
async def test_error_dict_from_service_is_supported_and_redacted(tmp_path):
    registry = CapabilityRegistry(tmp_path / "caps.json")
    error = NativeError(
        "authentication_error", "credential-secret", status_code=401, upstream_code="invalid_api_key"
    ).to_dict()
    await registry.record(connection(), "search", "auth_blocked", "authentication_error", error)
    row = await registry.get(connection(), "search")
    assert row["error"] == {
        "category": "authentication_error",
        "ambiguous": False,
        "status_code": 401,
        "message": "[redacted]",
        "upstream_code": "invalid_api_key",
    }


@pytest.mark.asyncio
async def test_header_and_proxy_credentials_cannot_be_persisted_as_reasons(tmp_path):
    conn = connection(headers={"X-Secret": "header-secret"}, proxy="http://user:proxy-secret@proxy.invalid")
    registry = CapabilityRegistry(tmp_path / "caps.json")
    for secret in ["header-secret", "proxy-secret"]:
        await registry.record(conn, "search", "temporary_failure", secret)
        assert (await registry.get(conn, "search"))["reason"] == "observation"


@pytest.mark.asyncio
async def test_cancellation_waits_for_atomic_write_before_releasing_lock(tmp_path, monkeypatch):
    import threading

    path = tmp_path / "caps.json"
    registry = CapabilityRegistry(path)
    await registry.initialize()
    started = threading.Event()
    release = threading.Event()
    write = registry._write

    def delayed_write(records):
        started.set()
        assert release.wait(5)
        write(records)

    monkeypatch.setattr(registry, "_write", delayed_write)
    task = asyncio.create_task(registry.record(connection(), "search", "available", "verified"))
    await asyncio.to_thread(started.wait, 5)
    assert started.is_set()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await registry.get(connection(), "search"))["status"] == "available"
    restarted = CapabilityRegistry(path)
    assert (await restarted.get(connection(), "search"))["status"] == "available"


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["unsupported", "auth_blocked"])
async def test_admin_reset_unblocks_only_selected_capability_and_survives_restart(tmp_path, blocked):
    path = tmp_path / "caps.json"
    registry = CapabilityRegistry(path)
    conn = connection()
    await registry.record(
        conn, "edit:gif", blocked, "explicit_rejection", NativeError("upstream", "rejected")
    )
    await registry.record(conn, "generate:gif", "unsupported", "unsupported_format")
    await registry.record(connection(model="other"), "edit:gif", blocked, "explicit_rejection")
    await registry.reset(conn, "edit:gif")
    restarted = CapabilityRegistry(path)
    row = await restarted.get(conn, "edit:gif")
    assert row["status"] == "unverified" and row["reason"] == "reset"
    assert "error" not in row
    await restarted.ensure(conn, "edit:gif")
    assert (await restarted.get(conn, "generate:gif"))["status"] == "unsupported"
    assert (await restarted.get(connection(model="other"), "edit:gif"))["status"] == blocked
    await restarted.record(conn, "edit:gif", "available", "probe_success")
    assert (await restarted.get(conn, "edit:gif"))["status"] == "available"


@pytest.mark.asyncio
async def test_reset_failure_preserves_blocked_observation(tmp_path, monkeypatch):
    path = tmp_path / "caps.json"
    registry = CapabilityRegistry(path)
    await registry.record(connection(), "search", "auth_blocked", "invalid_key")
    before = path.read_bytes()

    def fail_replace(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(capabilities.os, "replace", fail_replace)
    with pytest.raises(NativeError) as caught:
        await registry.reset(connection(), "search")
    assert caught.value.category == "storage"
    assert path.read_bytes() == before
    with pytest.raises(NativeError) as blocked:
        await registry.ensure(connection(), "search")
    assert blocked.value.category == "auth_blocked"


@pytest.mark.asyncio
async def test_error_diagnostics_keep_specific_reason_with_secrets_and_whole_urls_redacted(tmp_path):
    path = tmp_path / "caps.json"
    conn = connection(
        headers={"X-Secret": "header-secret"}, proxy="http://proxy-user:proxy%40secret@proxy.invalid"
    )
    error = NativeError(
        "upstream",
        "No channel available; key=credential-secret; header=header-secret; "
        "proxy-user proxy@secret proxy%40secret; endpoint=https://internal.invalid/v1?token=unknown; retry later",
        status_code=403,
        upstream_type="channel_error",
        upstream_code="no_available_channel",
        request_id="req-123",
    )
    registry = CapabilityRegistry(path)
    await registry.record(conn, "search", "temporary_failure", "upstream_error", error)
    row = await CapabilityRegistry(path).get(conn, "search")
    assert row["status"] == "temporary_failure"
    assert row["error"]["upstream_type"] == "channel_error"
    assert row["error"]["upstream_code"] == "no_available_channel"
    assert row["error"]["request_id"] == "req-123"
    assert row["error"]["status_code"] == 403
    assert "No channel available" in row["error"]["message"]
    assert "retry later" in row["error"]["message"]
    for secret in [
        "credential-secret",
        "header-secret",
        "proxy-user",
        "proxy@secret",
        "proxy%40secret",
        "internal.invalid",
        "token=unknown",
    ]:
        assert secret not in path.read_text()


@pytest.mark.asyncio
async def test_all_upstream_diagnostic_fields_are_redacted_and_bounded(tmp_path):
    path = tmp_path / "caps.json"
    registry = CapabilityRegistry(path)
    conn = connection(headers={"X-Secret": "header-secret"}, proxy="socks5://user:proxy-secret@host.invalid")
    await registry.record(
        conn,
        "search",
        "unsupported",
        "observed",
        {
            "category": "upstream",
            "message": "x" * 995 + "credential-secret" + "x" * 1000,
            "upstream_type": "type:header-secret " + "a" * 500,
            "upstream_code": "code:proxy-secret " + "b" * 500,
            "request_id": "https://req.invalid/id?key=unknown " + "c" * 500,
            "arbitrary": "do-not-store",
        },
    )
    details = (await registry.get(conn, "search"))["error"]
    assert len(details["message"]) == 1000
    assert details["message"].endswith("[reda")
    for name in ["upstream_type", "upstream_code", "request_id"]:
        assert len(details[name]) == 200
        assert "[redacted" in details[name]
    for secret in ["credential-secret", "header-secret", "proxy-secret", "req.invalid", "do-not-store"]:
        assert secret not in path.read_text()


@pytest.mark.asyncio
async def test_unknown_error_category_and_nonstring_details_do_not_change_observation(tmp_path):
    registry = CapabilityRegistry(tmp_path / "caps.json")
    await registry.record(
        connection(),
        "search",
        "temporary_failure",
        "upstream_error",
        {
            "category": ["untrusted"],
            "status_code": "401",
            "message": {"raw": "credential-secret"},
            "upstream_code": ["unsupported"],
            "request_id": None,
        },
    )
    row = await registry.get(connection(), "search")
    assert row["status"] == "temporary_failure"
    assert row["error"] == {"category": "upstream", "ambiguous": False, "status_code": None}
