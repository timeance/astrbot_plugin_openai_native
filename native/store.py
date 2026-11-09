from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from .config import Settings
from .models import Actor, NativeError

ACTIVE = ("queued", "running", "receiving", "cancel_requested")
TERMINAL = ("succeeded", "partial", "failed", "cancelled", "result_unavailable", "needs_reconciliation")
JSON_FIELDS = {"actor", "request", "error", "receipts", "artifacts", "result"}
MUTABLE = {
    "status",
    "error",
    "receipts",
    "artifacts",
    "result",
    "delivery_status",
    "cancel_requested",
    "submitted_count",
    "next_index",
}


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    """One ledger transaction reserves or settles every applicable quota bucket."""

    def __init__(self, path: Path, settings: Settings):
        self.path, self.settings = path, settings
        self.lock = asyncio.Lock()
        self.db: aiosqlite.Connection | None = None

    async def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path, isolation_level=None, timeout=10)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=10000;
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL,
                user_key TEXT NOT NULL, group_key TEXT NOT NULL, day TEXT NOT NULL,
                actor TEXT NOT NULL, request TEXT NOT NULL, fingerprint TEXT NOT NULL,
                model TEXT NOT NULL, protocol TEXT NOT NULL, status TEXT NOT NULL,
                requested INTEGER NOT NULL, actual INTEGER NOT NULL DEFAULT 0,
                pending INTEGER NOT NULL, error TEXT, receipts TEXT NOT NULL DEFAULT '[]',
                artifacts TEXT NOT NULL DEFAULT '[]', result TEXT,
                deliver INTEGER NOT NULL DEFAULT 0, delivery_status TEXT NOT NULL DEFAULT 'none',
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                submitted_count INTEGER NOT NULL DEFAULT 0, next_index INTEGER NOT NULL DEFAULT 0,
                idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL, message_id TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL,
                UNIQUE(owner, kind, idempotency_key)
            );
            CREATE TABLE IF NOT EXISTS quota (
                day TEXT NOT NULL, bucket TEXT NOT NULL, kind TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0, held INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(day, bucket, kind)
            );
            CREATE TABLE IF NOT EXISTS allocations (
                operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
                bucket TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0, held INTEGER NOT NULL,
                PRIMARY KEY(operation_id, bucket)
            );
            CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, metadata TEXT NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS operations_owner ON operations(owner, created);
            CREATE INDEX IF NOT EXISTS operations_status ON operations(status);
            CREATE INDEX IF NOT EXISTS assets_owner ON assets(owner, expires);
        """)

    def day(self):
        return datetime.now(ZoneInfo(self.settings.limits["timezone"])).date().isoformat()

    def buckets(self, actor: Actor, kind: str):
        limits = self.settings.limits
        if actor.is_admin and limits["admin_exempt"]:
            return []
        if actor.group_id:
            return [
                ("user:" + actor.user_key, int(limits["user_" + kind])),
                ("group:" + actor.group_key, int(limits["group_" + kind])),
            ]
        return [("user:" + actor.user_key, int(limits["private_" + kind]))]

    def _row(self, row):
        if row is None:
            return None
        result = dict(row)
        for name in JSON_FIELDS:
            result[name] = json.loads(result[name]) if result.get(name) is not None else None
        return result

    async def _one(self, sql, params=()):
        async with self.db.execute(sql, params) as cur:
            return await cur.fetchone()

    async def get(self, operation_id: str):
        async with self.lock:
            return self._row(await self._one("SELECT * FROM operations WHERE id=?", (operation_id,)))

    async def list_images(self, actor: Actor, limit: int = 20):
        """Only the caller's images in this exact session, including retained results."""
        async with self.lock:
            async with self.db.execute(
                "SELECT * FROM operations WHERE kind='image' AND owner=? ORDER BY created DESC LIMIT ?",
                (actor.owner_key, min(50, max(1, limit))),
            ) as cur:
                return [self._row(row) for row in await cur.fetchall()]

    async def reserve(
        self,
        *,
        operation_id,
        kind,
        actor,
        request,
        connection,
        units,
        idempotency_key,
        request_hash,
        deliver=False,
    ):
        now, day = time.time(), self.day()
        async with self.lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._one(
                    "SELECT * FROM operations WHERE owner=? AND kind=? AND idempotency_key=?",
                    (actor.owner_key, kind, idempotency_key),
                )
                if existing:
                    if existing["request_hash"] != request_hash:
                        raise NativeError(
                            "idempotency_conflict", "The idempotency key belongs to another request"
                        )
                    await self.db.commit()
                    return self._row(existing), False
                if kind == "image":
                    row = await self._one(
                        "SELECT COUNT(*) AS n FROM operations WHERE kind='image' AND status='queued'"
                    )
                    if row["n"] >= self.settings.limits["queue_size"]:
                        raise NativeError("queue_full", "Image waiting queue is full")
                    row = await self._one(
                        "SELECT COUNT(*) AS n FROM operations WHERE kind='image' AND user_key=? AND status IN ('queued','running','receiving','cancel_requested')",
                        (actor.user_key,),
                    )
                    if row["n"] >= self.settings.limits["user_image_concurrency"]:
                        raise NativeError("rate_limited", "This user already has an active image task")
                row = await self._one(
                    "SELECT MAX(created) AS last FROM operations WHERE user_key=? AND kind=?",
                    (actor.user_key, kind),
                )
                if (
                    row["last"] is not None
                    and now - row["last"] < self.settings.limits["min_interval_seconds"]
                ):
                    raise NativeError("rate_limited", "Requests are too frequent")
                if kind == "search" and actor.message_id:
                    row = await self._one(
                        "SELECT COUNT(*) AS n FROM operations WHERE owner=? AND kind='search' AND message_id=?",
                        (actor.owner_key, actor.message_id),
                    )
                    if row["n"] >= self.settings.limits["search_calls_per_message"]:
                        raise NativeError("rate_limited", "Search limit for this message reached")
                buckets = self.buckets(actor, kind)
                for bucket, limit in buckets:
                    row = await self._one(
                        "SELECT used,held FROM quota WHERE day=? AND bucket=? AND kind=?", (day, bucket, kind)
                    )
                    total = row["used"] + row["held"] if row else 0
                    if limit == 0 or (limit > 0 and total + units > limit):
                        raise NativeError("quota_exceeded", "Daily " + kind + " quota exceeded")
                await self.db.execute(
                    """INSERT INTO operations
                    (id,kind,owner,user_key,group_key,day,actor,request,fingerprint,model,protocol,
                     status,requested,pending,deliver,idempotency_key,request_hash,message_id,created,updated)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        operation_id,
                        kind,
                        actor.owner_key,
                        actor.user_key,
                        actor.group_key,
                        day,
                        encode(asdict(actor)),
                        encode(request),
                        connection.fingerprint,
                        connection.model,
                        connection.protocol,
                        "queued",
                        units,
                        units,
                        int(deliver),
                        idempotency_key,
                        request_hash,
                        actor.message_id,
                        now,
                        now,
                    ),
                )
                for bucket, _ in buckets:
                    await self.db.execute(
                        "INSERT INTO quota(day,bucket,kind,held) VALUES (?,?,?,?) ON CONFLICT(day,bucket,kind) DO UPDATE SET held=held+excluded.held",
                        (day, bucket, kind, units),
                    )
                    await self.db.execute(
                        "INSERT INTO allocations(operation_id,bucket,held) VALUES (?,?,?)",
                        (operation_id, bucket, units),
                    )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
            return self._row(await self._one("SELECT * FROM operations WHERE id=?", (operation_id,))), True

    async def update(self, operation_id, **fields):
        if not fields or not set(fields) <= MUTABLE:
            raise ValueError("Invalid operation fields")
        values = [encode(v) if k in JSON_FIELDS and v is not None else v for k, v in fields.items()]
        query = "UPDATE operations SET " + ",".join(k + "=?" for k in fields) + ",updated=? WHERE id=?"
        async with self.lock:
            await self.db.execute(query, (*values, time.time(), operation_id))

    async def claim(self, operation_id):
        async with self.lock:
            cur = await self.db.execute(
                "UPDATE operations SET status='running',updated=? WHERE id=? AND status='queued' AND cancel_requested=0",
                (time.time(), operation_id),
            )
            return cur.rowcount == 1

    async def submitted(self, operation_id):
        # Commit before crossing the paid boundary; restart recovery never resubmits this operation.
        async with self.lock:
            await self.db.execute(
                "UPDATE operations SET submitted_count=submitted_count+1,status='running',updated=? WHERE id=?",
                (time.time(), operation_id),
            )

    async def settle(self, operation_id, actual, pending, *, status=None, error=None, next_index=None):
        if actual < 0 or pending < 0:
            raise ValueError("Negative settlement")
        async with self.lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                op = await self._one("SELECT * FROM operations WHERE id=?", (operation_id,))
                if op is None:
                    raise NativeError("not_found", "Task not found")
                async with self.db.execute(
                    "SELECT * FROM allocations WHERE operation_id=?", (operation_id,)
                ) as cur:
                    allocations = await cur.fetchall()
                for allocation in allocations:
                    await self.db.execute(
                        "UPDATE quota SET used=used+?,held=held+? WHERE day=? AND bucket=? AND kind=?",
                        (
                            actual - allocation["used"],
                            pending - allocation["held"],
                            op["day"],
                            allocation["bucket"],
                            op["kind"],
                        ),
                    )
                await self.db.execute(
                    "UPDATE allocations SET used=?,held=? WHERE operation_id=?",
                    (actual, pending, operation_id),
                )
                await self.db.execute(
                    "UPDATE operations SET actual=?,pending=?,status=?,error=?,updated=? WHERE id=?",
                    (
                        actual,
                        pending,
                        status or op["status"],
                        encode(error) if error else None,
                        time.time(),
                        operation_id,
                    ),
                )
                if next_index is not None:
                    await self.db.execute(
                        "UPDATE operations SET next_index=? WHERE id=?", (next_index, operation_id)
                    )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def append(self, operation_id, name, item):
        if name not in ("receipts", "artifacts"):
            raise ValueError(name)
        async with self.lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                row = await self._one("SELECT " + name + " FROM operations WHERE id=?", (operation_id,))
                items = json.loads(row[name])
                identity = "index" if name == "receipts" else "artifact_id"
                if not any(i.get(identity) == item.get(identity) for i in items):
                    items.append(item)
                    await self.db.execute(
                        "UPDATE operations SET " + name + "=?,updated=? WHERE id=?",
                        (encode(items), time.time(), operation_id),
                    )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def request_cancel(self, operation_id):
        async with self.lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                row = await self._one("SELECT * FROM operations WHERE id=?", (operation_id,))
                previous = row["status"]
                if previous in ACTIVE:
                    state = "cancelled" if previous == "queued" else "cancel_requested"
                    await self.db.execute(
                        "UPDATE operations SET cancel_requested=1,status=?,updated=? WHERE id=?",
                        (state, time.time(), operation_id),
                    )
                await self.db.commit()
                return previous
            except BaseException:
                await self.db.rollback()
                raise

    async def list_recovery(self):
        async with self.lock:
            async with self.db.execute(
                "SELECT * FROM operations WHERE status IN ('queued','running','receiving','cancel_requested','result_unavailable')"
            ) as cur:
                return [self._row(row) for row in await cur.fetchall()]

    async def quota(self, actor):
        result = {"day": self.day(), "timezone": self.settings.limits["timezone"], "search": [], "image": []}
        async with self.lock:
            for kind in ("search", "image"):
                for bucket, limit in self.buckets(actor, kind):
                    row = await self._one(
                        "SELECT used,held FROM quota WHERE day=? AND bucket=? AND kind=?",
                        (result["day"], bucket, kind),
                    )
                    result[kind].append(
                        {
                            "scope": "group" if bucket.startswith("group:") else "user",
                            "limit": limit,
                            "used": row["used"] if row else 0,
                            "held": row["held"] if row else 0,
                        }
                    )
        return result

    async def asset_put(self, actor, asset, ttl):
        async with self.lock:
            await self.db.execute(
                "INSERT OR REPLACE INTO assets VALUES (?,?,?,?,?)",
                (asset.asset_id, actor.owner_key, encode(asdict(asset)), time.time(), time.time() + ttl),
            )

    async def asset_get(self, actor, asset_id=None):
        async with self.lock:
            if asset_id:
                row = await self._one(
                    "SELECT metadata FROM assets WHERE id=? AND owner=? AND expires>?",
                    (asset_id, actor.owner_key, time.time()),
                )
            else:
                row = await self._one(
                    "SELECT metadata FROM assets WHERE owner=? AND expires>? ORDER BY created DESC LIMIT 1",
                    (actor.owner_key, time.time()),
                )
        return json.loads(row["metadata"]) if row else None

    async def expired(self):
        async with self.lock:
            async with self.db.execute(
                "SELECT * FROM operations WHERE status IN ('succeeded','partial','failed','cancelled','result_unavailable') AND pending=0 AND updated<?",
                (time.time() - self.settings.storage["artifact_days"] * 86400,),
            ) as cur:
                return [self._row(r) for r in await cur.fetchall()]

    async def purge(self, operation_id):
        # Quota aggregates remain until their own retention window; records are independent.
        async with self.lock:
            await self.db.execute("DELETE FROM operations WHERE id=? AND pending=0", (operation_id,))

    async def close(self):
        if self.db:
            await self.db.close()
            self.db = None
