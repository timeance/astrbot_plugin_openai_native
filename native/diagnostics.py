# SPDX-License-Identifier: Apache-2.0
"""Bounded execution evidence. Never retain prompts, request bodies or credentials."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import tempfile
import threading
import time
import uuid
from collections import deque
from copy import deepcopy
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .capabilities import _redact, _secrets

CORRELATION = "openai_native.diagnostic_id"
_PRIVATE = re.compile(
    r"^(?:prompt|queries|messages|content|body|request_body|response_body|headers|keys|key|"
    r"token|base_url|proxy|references|.*(?:secret|password|credential|authorization|cookie|api[_-]?key|access[_-]?token).*)$", re.I
)
_TOKEN = re.compile(r"\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]+", re.I)
_CREDENTIAL = re.compile(r"((?:api[_-]?key|authorization|token|password)\s*[:=]\s*)[^\s,;}]+", re.I)


def runtime_info():
    try:
        from astrbot import __version__ as astrbot_version
    except ImportError:
        astrbot_version = None
    root = Path(__file__).resolve().parents[1]
    version = next(
        line.split(":", 1)[1].strip()
        for line in (root / "metadata.yaml").read_text("utf-8").splitlines()
        if line.startswith("version:")
    )
    return {
        "plugin_version": version,
        "astrbot_version": astrbot_version,
        "bridge_sha256": hashlib.sha256((root / "main.py").read_bytes()).hexdigest()[:16],
    }


def message_info(event):
    """Keep routing evidence without retaining message text, IDs or image locations."""
    getter = getattr(event, "get_message_str", None)
    text = getter() if callable(getter) else ""
    text = text if isinstance(text, str) else ""
    match = re.match(r"^/?(生图任务|生图取消|生动图|生图|onative)", text.lstrip())
    separator = text.lstrip()[match.end():match.end() + 1] if match else ""
    extra = getattr(event, "get_extra", None)
    handlers = extra("activated_handlers") if callable(extra) else []
    messages = event.get_messages() if callable(getattr(event, "get_messages", None)) else []
    return {
        "message_length": len(text),
        "command_hint": match.group(1) if match else None,
        "command_boundary": ("end" if not separator else "space" if separator.isspace()
                             else "punctuation" if separator in "，,：:" else "other") if match else None,
        "separator_codepoint": f"U+{ord(separator):04X}" if separator else None,
        "is_wake": bool(getattr(event, "is_wake", False)),
        "is_at_or_wake_command": bool(getattr(event, "is_at_or_wake_command", False)),
        "image_count": sum(type(part).__name__ == "Image" for part in messages),
        "has_reply": any(type(part).__name__ == "Reply" for part in messages),
        "activated_handlers": [getattr(handler, "handler_full_name", "unknown") for handler in handlers or []],
        "trace_id": getattr(getattr(event, "trace", None), "trace_id", None),
    }


def connection_info(connection):
    return {
        name: getattr(connection, name)
        for name in ("provider_id", "model", "host_model", "protocol", "service_type")
    }


def safe_error(error, connection=None):
    details = error.to_dict() if hasattr(error, "to_dict") else dict(error)
    secrets = _secrets(connection) if connection else set()
    return {
        name: (
            _redact(value, secrets, 1000 if name == "message" else 200) if isinstance(value, str) else value
        )
        for name, value in details.items()
        if name
        in {
            "category",
            "message",
            "status_code",
            "upstream_type",
            "upstream_code",
            "request_id",
            "ambiguous",
            "operation_id",
        }
    }


class Diagnostics:
    def __init__(self, capacity=200, *, directory=None):
        self.records = deque(maxlen=capacity)
        self.directory = Path(directory) if directory is not None else None
        self.enabled = False
        self.max_bytes = 100 * 1024 * 1024
        self.disk_error = None
        self._secrets = set()
        self._lock = threading.RLock()

    def remember_connection(self, connection):
        self._secrets.update(_secrets(connection))

    def _clean(self, value, depth=0):
        if depth > 8:
            return "[truncated]"
        if isinstance(value, dict):
            return {
                str(key)[:100]: self._clean(item, depth + 1)
                for key, item in list(value.items())[:64] if not _PRIVATE.fullmatch(str(key))
            }
        if isinstance(value, (list, tuple)):
            return [self._clean(item, depth + 1) for item in value[:64]]
        if isinstance(value, str):
            value = _TOKEN.sub("[redacted]", value)
            value = _CREDENTIAL.sub(r"\1[redacted]", value)
            return _redact(value, self._secrets, 1000).encode("utf-8", "replace").decode("utf-8")
        if value is None or type(value) in (bool, int):
            return value
        if type(value) is float:
            return value if math.isfinite(value) else None
        return "[unsupported]"

    def _paths(self):
        if self.directory is None:
            return []
        return [self.directory / "diagnostics.jsonl", *(
            self.directory / f"diagnostics.{index}.jsonl" for index in range(1, 5)
        )]

    def _prune(self):
        files = [(path, path.stat().st_size) for path in reversed(self._paths()) if path.exists()]
        total = sum(size for _, size in files)
        for path, size in files:
            if total <= self.max_bytes:
                break
            path.unlink()
            total -= size

    def configure(self, *, enabled, max_bytes):
        with self._lock:
            self.enabled, self.max_bytes = enabled, max_bytes
            self.disk_error = None
            if enabled:
                try:
                    self.directory.mkdir(parents=True, exist_ok=True)
                    self._prune()
                except OSError:
                    self.disk_error = "诊断日志目录不可写，请检查插件数据目录权限。"

    def _write(self, row):
        if not self.enabled or self.directory is None:
            return
        with self._lock:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                segment_bytes = self.max_bytes // 5
                encoded = (json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
                if len(encoded) > min(65536, segment_bytes):
                    encoded = (json.dumps({
                        "time": row["time"], "kind": row.get("kind"),
                        "correlation_id": row.get("correlation_id"), "truncated": True,
                    }, ensure_ascii=False) + "\n").encode("utf-8")
                paths = self._paths()
                if paths[0].exists() and paths[0].stat().st_size + len(encoded) > segment_bytes:
                    paths[-1].unlink(missing_ok=True)
                    for index in range(3, -1, -1):
                        if paths[index].exists():
                            paths[index].replace(paths[index + 1])
                with paths[0].open("ab") as handle:
                    handle.write(encoded)
                self._prune()
                self.disk_error = None
            except OSError:
                # Diagnostic persistence must never interrupt chatting or paid work.
                self.disk_error = "诊断日志写入失败，请检查磁盘空间与插件数据目录权限。"

    def logging_status(self):
        with self._lock:
            try:
                files = [{"name": path.name, "bytes": path.stat().st_size}
                         for path in self._paths() if path.exists()]
            except OSError:
                files = []
                self.disk_error = "无法读取诊断日志信息，请检查插件数据目录权限。"
            return {
                "enabled": self.enabled, "max_mb": self.max_bytes // (1024 * 1024),
                "total_bytes": sum(item["bytes"] for item in files), "files": files,
                "directory": "data/plugin_data/astrbot_plugin_openai_native/diagnostics",
                "error": self.disk_error,
            }

    def export_archive(self):
        """Copy a consistent log snapshot, then compress it without holding the writer lock."""
        archive = tempfile.TemporaryFile()
        try:
            with tempfile.TemporaryDirectory(prefix="openai-native-diagnostics-") as snapshot:
                copies = []
                with self._lock:
                    for path in reversed(self._paths()):
                        if path.is_symlink():
                            raise OSError("Diagnostic log must be a regular file")
                        if not path.exists():
                            continue
                        copy = Path(snapshot) / path.name
                        with path.open("rb") as source, copy.open("wb") as target:
                            shutil.copyfileobj(source, target, length=64 * 1024)
                        copies.append(copy)
                    if not copies:
                        raise FileNotFoundError("No saved diagnostic logs")
                    status = self.logging_status()
                with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=1) as bundle:
                    for copy in copies:
                        bundle.write(copy, copy.name)
                    bundle.writestr("runtime.json", json.dumps({
                        "runtime": runtime_info(), "logging": status,
                    }, ensure_ascii=False, indent=2))
            archive.seek(0)
            return archive
        except BaseException:
            archive.close()
            raise

    def correlation(self, event):
        getter, setter = getattr(event, "get_extra", None), getattr(event, "set_extra", None)
        value = getter(CORRELATION) if callable(getter) else None
        if not value:
            value = uuid.uuid4().hex[:16]
            if callable(setter):
                setter(CORRELATION, value)
        return value

    def add(self, event=None, **record):
        row = {"time": time.time(), **record}
        if event is not None:
            row["correlation_id"] = self.correlation(event)
        row = self._clean(row)
        self.records.append(deepcopy(row))
        self._write(row)
        return row

    def snapshot(self):
        return deepcopy(list(reversed(self.records)))
