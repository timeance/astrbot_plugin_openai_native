# SPDX-License-Identifier: Apache-2.0
"""Persist capability observations with bounded, redacted upstream diagnostics."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, unquote, urlsplit

from .models import Connection, NativeError

_STATUSES = {"available", "unverified", "unsupported", "auth_blocked", "temporary_failure"}
_BLOCKING = {"unsupported", "auth_blocked"}
_CAPABILITY = re.compile(r"(?:search|(?:generate|edit):(?:png|jpeg|webp|gif))\Z")
_SAFE_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")
_TEMPORARY_TTL = 60.0
_MAX_REGISTRY_BYTES = 8 * 1024 * 1024
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"']+", re.IGNORECASE)


def _secrets(conn: Connection) -> set[str]:
    proxy = urlsplit(conn.proxy or "")
    values = [conn.key, *conn.headers.values(), conn.proxy, proxy.username, proxy.password]
    result = set()
    for value in values:
        if isinstance(value, str) and value:
            decoded = unquote(value)
            result.update(
                (
                    value,
                    decoded,
                    quote(value, safe=""),
                    quote_plus(value),
                    quote(decoded, safe=""),
                    quote_plus(decoded),
                )
            )
    return result


def _redact(value: str, secrets: set[str], limit: int) -> str:
    # Redact before truncation so even a secret crossing the limit cannot leak.
    value = _URL.sub("[redacted-url]", value)
    if secrets:
        pattern = "|".join(re.escape(secret) for secret in sorted(secrets, key=len, reverse=True))
        value = re.sub(pattern, "[redacted]", value, flags=re.IGNORECASE)
    value = " ".join(value.split())
    return value[:limit]


class CapabilityRegistry:
    """Owns a JSON document independent of other plugin storage.

    Unknown/transient observations permit a real request. Only explicit negative
    observations block; an HTTP status alone never manufactures such an observation.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._initialized = False

    @staticmethod
    def _identity(conn: Connection, capability: str) -> str:
        if not _CAPABILITY.fullmatch(capability):
            raise NativeError("configuration", "Invalid capability key")
        if not conn.fingerprint:
            raise NativeError("configuration", "Connection fingerprint is required")
        material = json.dumps(
            [conn.fingerprint, conn.model, conn.host_model, conn.protocol, capability],
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(material).hexdigest()

    async def initialize(self) -> None:
        async with self._lock:
            await self._load_locked()

    async def _load_locked(self) -> None:
        if self._initialized:
            return
        try:
            document = await asyncio.to_thread(self._read)
        except FileNotFoundError:
            document = {"version": 1, "records": {}}
        except (OSError, ValueError, UnicodeDecodeError):
            raise NativeError("storage", "Capability registry could not be read") from None
        if (
            not isinstance(document, dict)
            or document.get("version") != 1
            or not isinstance(document.get("records"), dict)
        ):
            raise NativeError("storage", "Invalid capability registry format")
        records = document["records"]
        for key, row in records.items():
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"[0-9a-f]{64}", key)
                or not self._valid_record(row)
            ):
                raise NativeError("storage", "Invalid capability record")
        self._records = records
        self._initialized = True

    def _read(self):
        with self.path.open("rb") as handle:
            content = handle.read(_MAX_REGISTRY_BYTES + 1)
        if len(content) > _MAX_REGISTRY_BYTES:
            raise ValueError("oversize")
        return json.loads(content)

    @staticmethod
    def _valid_record(row: Any) -> bool:
        if not isinstance(row, dict) or row.get("status") not in _STATUSES:
            return False
        if not isinstance(row.get("reason"), str):
            return False
        for key in ("updated_at", "expires_at"):
            value = row.get(key)
            if value is None and key == "expires_at":
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return False
        return True

    async def get(self, conn: Connection, capability: str) -> dict:
        key = self._identity(conn, capability)
        async with self._lock:
            await self._load_locked()
            row = self._records.get(key)
            if row and (row["expires_at"] is None or row["expires_at"] > time.time()):
                return json.loads(json.dumps(row))
            return {
                "status": "unverified",
                "reason": "not_verified",
                "updated_at": None,
                "expires_at": None,
            }

    async def record(
        self,
        conn: Connection,
        capability: str,
        status: str,
        reason: str,
        error: NativeError | dict | None = None,
    ) -> None:
        if status not in _STATUSES:
            raise NativeError("configuration", "Invalid capability status")
        key = self._identity(conn, capability)
        reason = reason if isinstance(reason, str) and _SAFE_CODE.fullmatch(reason) else "observation"
        secrets = _secrets(conn)
        if any(secret.lower() in reason.lower() for secret in secrets):
            reason = "observation"
        now = time.time()
        row: dict[str, Any] = {
            "status": status,
            "reason": reason,
            "updated_at": now,
            "expires_at": now + _TEMPORARY_TTL if status == "temporary_failure" else None,
        }
        if error is not None:
            # Keep the upstream reason useful while removing connection secrets and URLs.
            details = error.to_dict() if isinstance(error, NativeError) else error
            if not isinstance(details, dict):
                raise NativeError("configuration", "Invalid capability error")
            category = details.get("category")
            safe_categories = _STATUSES | {
                "configuration",
                "upstream",
                "upstream_error",
                "network",
                "timeout",
                "rate_limited",
                "ambiguous",
                "authentication_error",
                "permission_denied",
                "invalid_request",
                "not_found",
                "quota_exceeded",
                "model_not_found",
                "policy_rejected",
                "unsupported_parameter",
                "unsupported_value",
                "protocol_error",
                "request_rejected",
            }
            row["error"] = {
                "category": category
                if isinstance(category, str) and category in safe_categories
                else "upstream",
                "ambiguous": bool(details.get("ambiguous", False)),
                "status_code": details.get("status_code")
                if type(details.get("status_code")) is int
                else None,
            }
            for field, limit in (
                ("message", 1000),
                ("upstream_type", 200),
                ("upstream_code", 200),
                ("request_id", 200),
            ):
                value = details.get(field)
                if isinstance(value, str):
                    row["error"][field] = _redact(value, secrets, limit)
        async with self._lock:
            await self._load_locked()
            records = {
                item_key: value
                for item_key, value in self._records.items()
                if value["expires_at"] is None or value["expires_at"] > now
            }
            records[key] = row
            try:
                save = asyncio.create_task(asyncio.to_thread(self._write, records))
                try:
                    await asyncio.shield(save)
                except asyncio.CancelledError:
                    # Hold the lock until the atomic write finishes, then reflect it
                    # in memory before propagating cancellation.
                    await save
                    self._records = records
                    raise
            except (OSError, ValueError):
                raise NativeError("storage", "Capability registry could not be saved") from None
            self._records = records

    def _write(self, records: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            {"version": 1, "records": records},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        if len(encoded) > _MAX_REGISTRY_BYTES:
            raise ValueError("oversize")
        descriptor, temporary = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    async def reset(self, conn: Connection, capability: str) -> None:
        """Clear a blocking observation before an administrator's explicit paid probe.

        Authorization and the probe itself belong to the service layer.
        """
        await self.record(conn, capability, "unverified", "reset")

    async def ensure(self, conn: Connection, capability: str) -> None:
        row = await self.get(conn, capability)
        if row["status"] in _BLOCKING:
            raise NativeError(row["status"], f"Capability {capability} is {row['status']}: {row['reason']}")
