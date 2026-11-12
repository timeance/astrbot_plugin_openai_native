# SPDX-License-Identifier: Apache-2.0
"""Authenticated settings endpoints; only diagnostic logging applies immediately."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import io
import ipaddress
import json
import math
import re
import secrets
import warnings
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

from .assets import MIME
from .config import DEFAULTS, Settings
from .diagnostics import safe_error
from .models import NativeError
from .store import ACTIVE, TERMINAL

_ENV = re.compile(r"(?:\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*|env:[A-Za-z_][A-Za-z0-9_]*)\Z")
_SECRET_NAME = re.compile(r"key|token|secret|password|credential|authorization|cookie", re.I)
_FEATURES = ("search", "image")
_SECRET_FIELDS = {f"{feature}.{key}" for feature in _FEATURES for key in ("keys", "headers")}
_REPLACEABLE = {
    f"{feature}.{key}"
    for feature in _FEATURES
    for key in ("keys", "headers", "base_url", "proxy", "extra_body", "paths")
}
_NUMBERS = {
    "search.timeout",
    "image.timeout",
    "limits.min_interval_seconds",
    "network.connect_timeout",
    "network.download_timeout",
}
_PREVIEW_READ_BYTES = 32 * 1024 * 1024
_PREVIEW_BYTES = 8 * 1024 * 1024


class PageError(Exception):
    """A fixed, credential-free error safe to return through the page bridge."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _encoded(values) -> bytes:
    return json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sensitive_url(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if _ENV.fullmatch(value):
        return True
    try:
        parsed = urlsplit(value)
        return bool(parsed.username or parsed.password or parsed.query or parsed.fragment)
    except ValueError:
        # Malformed legacy URLs must not expose a partially parsed credential.
        return True


def _sensitive_mapping(value, *, root=True) -> bool:
    if isinstance(value, str) and root:
        if _ENV.fullmatch(value):
            return True
        try:
            value = json.loads(value)
        except ValueError:
            return True
    if isinstance(value, Mapping):
        return any(
            _SECRET_NAME.search(str(key)) or _sensitive_mapping(item, root=False)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_sensitive_mapping(item, root=False) for item in value)
    return isinstance(value, str) and _sensitive_url(value)


def _secret(path: str, value) -> bool:
    if path in _SECRET_FIELDS:
        return True
    if path in {f"{feature}.{key}" for feature in _FEATURES for key in ("base_url", "proxy")}:
        return _sensitive_url(value)
    if path in {f"{feature}.{key}" for feature in _FEATURES for key in ("extra_body", "paths")}:
        return _sensitive_mapping(value)
    return False


def _count(value) -> int:
    if isinstance(value, (list, dict)):
        return sum(bool(item) for item in (value.values() if isinstance(value, dict) else value))
    return int(bool(value))


def _merge_mapping(current: dict, patch: dict) -> dict:
    result = deepcopy(current)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_mapping(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _object(value):
    """Preserve the resolver's supported legacy JSON-object and env forms."""
    if isinstance(value, str):
        if _ENV.fullmatch(value):
            return None
        try:
            value = json.loads(value)
        except ValueError:
            raise PageError("validation", "字典配置必须是 JSON 对象。") from None
    if not isinstance(value, dict):
        raise PageError("validation", "字典配置必须是对象。")
    return value


def _validate_url(value: str, *, proxy: bool) -> None:
    if not value or _ENV.fullmatch(value):
        return
    schemes = ("http", "https", "socks5", "socks5h") if proxy else ("http", "https")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        valid = parsed.scheme in schemes and bool(host)
        valid = valid and not parsed.netloc.rsplit("@", 1)[-1].endswith(":")
        valid = valid and (parsed.port is None or 1 <= parsed.port <= 65535)
        if host and (":" in host or re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", host)):
            ipaddress.ip_address(host)
        elif host:
            ascii_host = host.encode("idna").decode("ascii").removesuffix(".")
            valid = (
                valid
                and len(ascii_host) <= 253
                and all(
                    re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                    for label in ascii_host.split(".")
                )
            )
    except (ValueError, UnicodeError):
        valid = False
    if (
        not valid
        or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in value)
        or re.search(r'[<>"{}|^`\\]', value)
        or re.search(r"%(?![0-9A-Fa-f]{2})", value)
    ):
        raise PageError("validation", "API 或代理地址的格式或字符无效。")


def _size_pixels(value: str) -> int | None:
    if value == "auto":
        return None
    match = re.fullmatch(r"([0-9]+)x([0-9]+)", value)
    if not match:
        raise PageError("validation", "图片尺寸必须是 auto 或正整数宽x高。")
    width, height = map(int, match.groups())
    if min(width, height) <= 0:
        raise PageError("validation", "图片尺寸必须是 auto 或正整数宽x高。")
    return width * height


def _validate(values: dict) -> dict:
    try:
        effective = Settings(values).data
        _encoded(effective)
    except (NativeError, ValueError, TypeError, OverflowError):
        raise PageError("validation", "配置类型、范围或时区无效。") from None
    for section, defaults in DEFAULTS.items():
        for key, default in defaults.items():
            path = f"{section}.{key}"
            value = effective[section][key]
            if path in _NUMBERS:
                valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
            elif type(default) is bool:
                valid = type(value) is bool
            elif type(default) is int:
                valid = type(value) is int
            elif isinstance(default, str):
                valid = isinstance(value, str)
            elif isinstance(default, list):
                # Older configs accepted a single key or a whole-value env reference.
                valid = (key == "keys" and isinstance(value, str)) or (
                    isinstance(value, list) and all(isinstance(item, str) for item in value)
                )
            else:
                obj = _object(value)
                valid = (
                    obj is None or key == "extra_body" or all(isinstance(item, str) for item in obj.values())
                )
            if not valid:
                raise PageError("validation", "配置字段类型或范围无效。")
    for feature in _FEATURES:
        cfg = effective[feature]
        if cfg["mode"] not in ("current", "provider", "manual"):
            raise PageError("validation", "API 来源无效。")
        protocols = ("auto", "responses", "alpha" if feature == "search" else "images")
        if cfg["protocol"] not in protocols:
            raise PageError("validation", "接口协议无效。")
        if cfg["service_type"] not in ("auto", "openai", "newapi", "sub2api", "unknown"):
            raise PageError("validation", "入口服务类型无效。")
        for key in ("base_url", "proxy"):
            _validate_url(cfg[key], proxy=key == "proxy")
        headers = _object(cfg["headers"])
        if headers and any(
            not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or "\r" in item or "\n" in item
            for name, item in headers.items()
        ):
            raise PageError("validation", "请求头名称或内容无效。")
        paths = _object(cfg["paths"])
        for value in (paths or {}).values():
            if _ENV.fullmatch(value):
                continue
            parsed = urlsplit(value)
            if (
                not value
                or parsed.scheme
                or parsed.netloc
                or parsed.query
                or parsed.fragment
                or "\\" in value
                or any(part in (".", "..") for part in parsed.path.split("/"))
            ):
                raise PageError("validation", "接口路径无效。")
    image, gif = effective["image"], effective["gif"]
    default_pixels = _size_pixels(image["default_size"])
    for size in image["sizes"]:
        _size_pixels(size)
    if default_pixels is not None and default_pixels > image["max_pixels"]:
        raise PageError("validation", "默认图片尺寸超过单帧像素上限。")
    if image["default_format"] not in ("png", "jpeg", "webp", "gif"):
        raise PageError("validation", "默认图片格式无效。")
    if not 2 <= gif["frame_count"] <= 16 or not 20 <= gif["frame_duration_ms"] <= 2000:
        raise PageError("validation", "GIF 帧数或帧时长超出范围。")
    return effective


def _http_response(data, *, status_code=200, headers=None):
    try:
        from astrbot.api.web import json_response
    except ImportError:
        from quart import jsonify

        response = jsonify(data)
        response.status_code = status_code
        response.headers.update(headers or {})
        return response
    return json_response(data, status_code=status_code, headers=headers)


def _request_context():
    """Read only host-bound identity, never headers, query arguments or JSON flags."""
    try:
        from astrbot.api.web import PluginRequest, request
    except ImportError:
        pass
    else:
        try:
            bound = request._get_current()
        except RuntimeError:
            pass
        else:
            if not isinstance(bound, PluginRequest):
                raise PageError("unauthorized", "需要登录 AstrBot 管理面板。", 401)
            return bound.username, bound.json
    # Lazy imports keep unit tests independent of Quart and AstrBot startup.
    try:
        from quart import g, request

        return getattr(g, "username", None), request.get_json
    except (ImportError, RuntimeError):
        raise PageError("unauthorized", "需要登录 AstrBot 管理面板。", 401) from None


class PageAPI:
    def __init__(self, context, config, active_settings: Settings, service):
        self.context, self.config, self.service = context, config, service
        self._active = deepcopy(active_settings.data)
        self._revision_key = secrets.token_bytes(32)
        self._lock = asyncio.Lock()
        self._export_lock = asyncio.Lock()
        schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text("utf-8"))
        self._fields = {
            f"{section}.{key}": {
                name: deepcopy(value) for name, value in entry.items() if name != "invisible"
            }
            for section, definition in schema.items()
            for key, entry in definition["items"].items()
        }

    def _revision(self, values: dict) -> str:
        return hmac.new(self._revision_key, _encoded(values), hashlib.sha256).hexdigest()

    @staticmethod
    def _public(values: dict) -> dict:
        return {
            section: {
                key: deepcopy(values[section][key])
                for key in defaults
                if not _secret(f"{section}.{key}", values[section][key])
            }
            for section, defaults in DEFAULTS.items()
        }

    def snapshot(self) -> dict:
        raw = deepcopy(dict(self.config))
        effective = Settings(raw).data
        changed = [
            f"{section}.{key}"
            for section, defaults in DEFAULTS.items()
            for key in defaults
            if effective[section][key] != self._active[section][key]
        ]
        secret_states = {}
        fields = deepcopy(self._fields)
        for section, defaults in DEFAULTS.items():
            for key in defaults:
                path, value = f"{section}.{key}", effective[section][key]
                hidden = _secret(path, value) or _secret(path, self._active[section][key])
                if hidden:
                    secret_states[path] = {"configured": bool(_count(value)), "count": _count(value)}
                    fields[path]["secret"] = True
                    fields[path]["secret_update"] = True
        return {
            "values": self._public(effective),
            "active_values": self._public(self._active),
            "revision": self._revision(raw),
            "needs_reload": bool(changed),
            "changed_paths": changed,
            "secrets": secret_states,
            "fields": fields,
        }

    async def apply_patch(self, payload: dict) -> dict:
        if (
            not isinstance(payload, dict)
            or set(payload) - {"revision", "patch", "secret_updates"}
            or not isinstance(payload.get("revision"), str)
            or not isinstance(payload.get("patch"), dict)
            or not isinstance(payload.get("secret_updates", {}), dict)
        ):
            raise PageError("validation", "需要 revision、局部 patch 和可选 secret_updates。")
        try:
            _encoded(payload)
        except (ValueError, TypeError, OverflowError):
            raise PageError("validation", "配置必须是有效 JSON 值。") from None
        async with self._lock:
            before = deepcopy(dict(self.config))
            if not re.fullmatch(r"[0-9a-f]{64}", payload["revision"]) or not hmac.compare_digest(
                payload["revision"], self._revision(before)
            ):
                raise PageError("conflict", "配置版本冲突，请刷新后重试。", 409)
            candidate = deepcopy(before)
            effective = Settings(before).data
            for section, patch in payload["patch"].items():
                if section not in DEFAULTS or not isinstance(patch, dict):
                    raise PageError("validation", "存在未知配置字段或错误的配置分组。")
                for key, value in patch.items():
                    if key not in DEFAULTS[section]:
                        raise PageError("validation", "存在未知配置字段。")
                    path, current = f"{section}.{key}", effective[section][key]
                    if _secret(path, current) or _secret(path, value):
                        raise PageError("secret_patch", "秘密字段必须通过 secret_updates 替换。")
                    if isinstance(DEFAULTS[section][key], dict):
                        if not isinstance(value, dict):
                            raise PageError("validation", "字典配置必须是对象。")
                        if path != "image.presets":
                            value = _merge_mapping(current if isinstance(current, dict) else {}, value)
                        if _secret(path, value):
                            raise PageError("secret_patch", "秘密字段必须通过 secret_updates 替换。")
                    if _encoded(value) != _encoded(current):
                        candidate.setdefault(section, {})[key] = deepcopy(value)
            for path, value in payload.get("secret_updates", {}).items():
                if path not in _REPLACEABLE:
                    raise PageError("validation", "不支持替换此秘密字段。")
                section, key = path.split(".")
                default = DEFAULTS[section][key]
                if key == "keys":
                    valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
                else:
                    valid = isinstance(value, type(default))
                if not valid:
                    raise PageError("validation", "秘密更新类型无效。")
                if _encoded(value) != _encoded(effective[section][key]):
                    candidate.setdefault(section, {})[key] = deepcopy(value)
            try:
                _validate(candidate)
            except (ValueError, TypeError, OverflowError):
                raise PageError("validation", "配置字段类型或范围无效。") from None
            if candidate == before:
                return self.snapshot()
            try:
                # AstrBot save_config merges into memory before writing to disk.
                # Pass only the actual candidate; keep absent defaults and legacy keys intact.
                if candidate.get("image", {}).get("presets") != before.get("image", {}).get("presets"):
                    self.config.setdefault("image", {}).pop("presets", None)
                self.config.save_config(deepcopy(candidate))
                self.config.update(deepcopy(candidate))
            except Exception:
                self.config.clear()
                self.config.update(before)
                raise PageError("save_failed", "配置保存失败，内存配置已恢复。", 500) from None
            diagnostic = Settings(candidate).data["diagnostics"]
            if diagnostic != self._active["diagnostics"]:
                self.service.configure_diagnostics(diagnostic)
                self._active["diagnostics"] = deepcopy(diagnostic)
            return self.snapshot()

    async def providers(self) -> dict:
        providers = self.context.get_all_providers()
        if inspect.isawaitable(providers):
            providers = await providers
        result = []
        for provider in providers:
            meta = provider.meta()
            if inspect.isawaitable(meta):
                meta = await meta
            model = provider.get_model()
            if inspect.isawaitable(model):
                model = await model
            provider_id = meta.id
            if not isinstance(provider_id, str) or not isinstance(model, str):
                raise PageError("providers_unavailable", "供应商元数据不可用。", 503)
            result.append({"id": provider_id, "label": provider_id, "model": model})
        return {"providers": result}

    async def diagnostics(self) -> dict:
        return await self.service.diagnostics_snapshot()

    async def download_diagnostics(self, headers):
        from astrbot.api.web import stream_response

        try:
            async with self._export_lock:
                archive = await asyncio.to_thread(self.service.diagnostics.export_archive)
        except FileNotFoundError:
            raise PageError("logs_unavailable", "暂无已保存的诊断日志，请开启保存并触发一次调用。", 404) from None
        except OSError:
            raise PageError("logs_unavailable", "诊断日志导出失败，请检查磁盘空间与日志目录权限后重试。", 503) from None

        async def chunks():
            try:
                while chunk := await asyncio.to_thread(archive.read, 64 * 1024):
                    yield chunk
            finally:
                archive.close()

        try:
            return stream_response(chunks(), content_type="application/zip", headers={
                **headers, "Content-Disposition": 'attachment; filename="openai-native-diagnostics.zip"',
            })
        except BaseException:
            archive.close()
            raise

    async def reset_capabilities(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or payload:
            raise PageError("validation", "重置请求应为空对象。")
        await self.service.capabilities.clear()
        return {"reset": True}

    @staticmethod
    def _pagination(payload, *, default_limit, fields=()):
        if not isinstance(payload, dict) or set(payload) - {"offset", "limit", *fields}:
            raise PageError("validation", "查询字段无效。")
        offset, limit = payload.get("offset", 0), payload.get("limit", default_limit)
        if type(offset) is not int or not 0 <= offset <= 2**63 - 1:
            raise PageError("validation", "offset 必须是非负整数。")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise PageError("validation", "limit 必须是 1 到 50 的整数。")
        return offset, limit

    @staticmethod
    def _prompt(kind, request):
        if kind == "search":
            queries = request.get("queries", [])[:10]
            return " / ".join(query[:500] for query in queries if isinstance(query, str))[:500]
        return str(request.get("options", request).get("prompt", ""))[:500]

    async def tasks(self, payload: dict) -> dict:
        offset, limit = self._pagination(payload, default_limit=20, fields=("kind", "status"))
        kind, status = payload.get("kind", "all"), payload.get("status", "all")
        if kind not in ("all", "search", "image") or status not in ("all", *ACTIVE, *TERMINAL):
            raise PageError("validation", "任务类型或状态无效。")
        result = await self.service.store.page_tasks(offset, limit, kind, status)
        for item in result["items"]:
            item["prompt"] = self._prompt(item["kind"], item.pop("request"))
            error = item.pop("error")
            if error:
                # Saved errors may predate connection-aware redaction. Never return their raw message.
                details = {"category": "task_error", "message": "任务未完成，详细错误已隐藏。"}
                category = error.get("category")
                if isinstance(category, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", category):
                    details["category"] = category
                if type(error.get("status_code")) is int and 100 <= error["status_code"] <= 599:
                    details["status_code"] = error["status_code"]
                if type(error.get("ambiguous")) is bool:
                    details["ambiguous"] = error["ambiguous"]
                item["error"] = safe_error(details)
        return result

    def _artifact_path(self, artifact):
        try:
            root = self.service.assets.root / "artifacts"
            path = Path(artifact["path"])
            if not path.is_absolute() or ".." in path.parts or root.resolve(strict=True) != root:
                raise ValueError
            path = path.resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError
            return path
        except (KeyError, TypeError, ValueError, OSError, RuntimeError):
            raise PageError("artifact_unavailable", "图片文件不存在或不可访问。", 404) from None

    async def gallery(self, payload: dict) -> dict:
        offset, limit = self._pagination(payload, default_limit=12)
        result = await self.service.store.page_gallery(offset, limit)

        def public_items():
            items = []
            for row in result["items"]:
                artifact = row["artifact"]
                try:
                    self._artifact_path(artifact)
                    available = True
                except PageError:
                    available = False
                items.append({
                    "task_id": row["task_id"],
                    "artifact_id": artifact["artifact_id"],
                    "prompt": self._prompt("image", row["request"]),
                    "created": row["created"],
                    "mime": artifact["mime"],
                    "width": artifact["width"],
                    "height": artifact["height"],
                    "frame_count": artifact.get("frame_count", 1),
                    "available": available,
                })
            return items

        result["items"] = await asyncio.to_thread(public_items)
        return result

    def _preview(self, artifact, thumbnail):
        path = self._artifact_path(artifact)
        read_limit = min(_PREVIEW_READ_BYTES, self._active["storage"]["max_output_bytes"])
        try:
            if path.stat().st_size > read_limit:
                raise PageError("preview_too_large", "图片超出预览读取大小限制。", 413)
            with path.open("rb") as stream:
                data = stream.read(read_limit + 1)
            if len(data) > read_limit:
                raise PageError("preview_too_large", "图片超出预览读取大小限制。", 413)
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    mime = MIME.get(image.format)
                    frames = getattr(image, "n_frames", 1)
                    pixels = image.width * image.height
                    if not mime or pixels > 16777216 or frames * pixels > 67108864:
                        raise PageError("preview_too_large", "图片超出预览像素限制。", 413)
                    if not thumbnail and mime == "image/gif" and len(data) <= _PREVIEW_BYTES:
                        self.service.assets.inspect(data)
                        return {
                            "data_url": "data:" + mime + ";base64," + base64.b64encode(data).decode(),
                            "mime": mime,
                            "first_frame": False,
                        }
                    image.seek(0)
                    image.load()
                    image = image.convert("RGBA")
                    side = 320 if thumbnail else 1280
                    image.thumbnail((side, side), Image.Resampling.LANCZOS)
                    output = io.BytesIO()
                    image.save(output, format="PNG")
                    data = output.getvalue()
            if len(data) > _PREVIEW_BYTES:
                raise PageError("preview_too_large", "图片超出预览响应大小限制。", 413)
            return {
                "data_url": "data:" + "image/png;base64," + base64.b64encode(data).decode(),
                "mime": "image/png",
                "first_frame": frames > 1,
            }
        except PageError:
            raise
        except OSError:
            raise PageError("artifact_unavailable", "图片文件不存在或不可访问。", 404) from None
        except Exception:
            raise PageError("invalid_image", "图片无法解码。") from None

    async def preview(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) - {"task_id", "artifact_id", "thumbnail"}:
            raise PageError("validation", "预览字段无效。")
        for field in ("task_id", "artifact_id"):
            if not isinstance(payload.get(field), str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,200}", payload[field]
            ):
                raise PageError("validation", "需要有效的任务和图片 ID。")
        thumbnail = payload.get("thumbnail", True)
        if type(thumbnail) is not bool:
            raise PageError("validation", "thumbnail 必须是布尔值。")
        artifact = await self.service.store.page_artifact(payload["task_id"], payload["artifact_id"])
        if artifact is None:
            raise PageError("artifact_unavailable", "图片文件不存在或不可访问。", 404)
        return await asyncio.to_thread(self._preview, artifact, thumbnail)

    def _protected(self, operation: str):
        async def handler():
            headers = {"Cache-Control": "no-store"}
            try:
                username, read_json = _request_context()
                if not isinstance(username, str) or not username.strip():
                    raise PageError("unauthorized", "需要登录 AstrBot 管理面板。", 401)
                if operation == "settings":
                    result = self.snapshot()
                elif operation == "providers":
                    result = await self.providers()
                elif operation == "diagnostics":
                    result = await self.diagnostics()
                elif operation == "download_diagnostics":
                    return await self.download_diagnostics(headers)
                else:
                    try:
                        payload = await read_json()
                    except Exception:
                        raise PageError("validation", "请求必须包含有效 JSON 对象。") from None
                    actions = {
                        "save": self.apply_patch,
                        "reset": self.reset_capabilities,
                        "tasks": self.tasks,
                        "gallery": self.gallery,
                        "preview": self.preview,
                    }
                    result = await actions[operation](payload)
            except PageError as exc:
                return _http_response(
                    {"status": "error", "message": exc.message, "code": exc.code},
                    status_code=exc.status,
                    headers=headers,
                )
            except Exception:
                return _http_response(
                    {
                        "status": "error",
                        "message": "页面操作失败，详细错误已隐藏。",
                        "code": "internal_error",
                    },
                    status_code=500,
                    headers=headers,
                )
            return _http_response(result, headers=headers)

        return handler

    def register(self) -> None:
        """Register protected APIs and aliases used by AstrBot's plugin page bridge.

        The bridge prefixes the actual plugin name before endpoint strings.
        Frontend endpoints remain ``openai-native/settings`` and its siblings.
        """
        routes = (
            ("settings", "GET", "settings"),
            ("settings", "POST", "save"),
            ("providers", "GET", "providers"),
            ("diagnostics", "GET", "diagnostics"),
            ("diagnostics/download", "GET", "download_diagnostics"),
            ("capabilities/reset", "POST", "reset"),
            ("tasks/query", "POST", "tasks"),
            ("gallery/query", "POST", "gallery"),
            ("gallery/preview", "POST", "preview"),
        )
        for suffix, method, operation in routes:
            handler = self._protected(operation)
            for prefix in ("/openai-native", "/astrbot_plugin_openai_native/openai-native"):
                self.context.register_web_api(
                    f"{prefix}/{suffix}", handler, [method], "OpenAI Native 管理页面接口"
                )
