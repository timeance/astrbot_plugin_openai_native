"""Quota semantics shared by command, tool, and public API submissions."""

from pathlib import Path

import pytest

from native.config import Settings
from native.models import Actor, Connection, NativeError
from native.store import Store


@pytest.mark.asyncio
async def test_zero_unlimited_still_checks_other_finite_bucket(tmp_path: Path):
    settings = Settings({"limits": {"user_image": 0, "group_image": 1, "min_interval_seconds": 0,
                                    "user_image_concurrency": 2}})
    store = Store(tmp_path / "quota.sqlite3", settings)
    await store.initialize()
    actor = Actor("qq", "bot", "user", "session", "group")
    conn = Connection("provider", "https://example.test/v1", "key", fingerprint="fp")
    try:
        await store.reserve(operation_id="first", kind="image", actor=actor, request={},
                            connection=conn, units=1, idempotency_key="one", request_hash="one")
        with pytest.raises(NativeError, match="quota exceeded"):
            await store.reserve(operation_id="second", kind="image", actor=actor, request={},
                                connection=conn, units=1, idempotency_key="two", request_hash="two")
        rows = await store.quota(actor)
        assert [(row["limit"], row["held"]) for row in rows["image"]] == [(0, 1), (1, 1)]
    finally:
        await store.close()
