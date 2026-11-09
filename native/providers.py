# SPDX-License-Identifier: Apache-2.0
"""Resolve loaded AstrBot providers without mutating their configuration."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import math
import os
import re
import time
from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .models import Connection, NativeError

_ENV = re.compile(
    r"(?:\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)|env:([A-Za-z_][A-Za-z0-9_]*))"
)
_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_BLOCKED_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "host",
    "content-length",
    "content-type",
    "connection",
    "transfer-encoding",
    "upgrade",
    "te",
    "trailer",
    "keep-alive",
    "x-api-key",
    "api-key",
}
_SEARCH_BODY = {
    "temperature",
    "top_p",
    "max_output_tokens",
    "reasoning",
    "reasoning_effort",
    "service_tier",
    "metadata",
    "user",
}
_IMAGE_BODY = {"background", "moderation", "output_compression", "user"}
_SERVICES = {"auto", "openai", "newapi", "sub2api", "unknown"}


def _env(value: Any) -> Any:
    """Expand whole-value references only; never interpolate arbitrary text."""
    if isinstance(value, str):
        match = _ENV.fullmatch(value)
        if match:
            name = next(group for group in match.groups() if group is not None)
            resolved = os.environ.get(name)
            if resolved is None:
                raise NativeError("configuration", f"Environment variable {name} is not set")
            return resolved
    return value


def _object(value: Any, label: str) -> dict[str, Any]:
    value = _env(value)
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise NativeError("configuration", f"{label} must be a JSON object") from None
    if not isinstance(value, Mapping):
        raise NativeError("configuration", f"{label} must be an object")
    return deepcopy(dict(value))


def _text(value: Any, label: str) -> str:
    value = _env(value)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise NativeError("configuration", f"{label} must be a string")
    return value.strip()


def _base_url(value: Any) -> str:
    value = _text(value, "base_url")
    try:
        parsed = urlsplit(value)
        valid = (
            not any(ord(char) < 32 or ord(char) == 127 for char in value)
            and parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise NativeError("configuration", "base_url must be an HTTP(S) URL without credentials or query")
    return value.rstrip("/")


def _headers(*values: Any) -> dict[str, str]:
    result: dict[str, tuple[str, str]] = {}
    for value in values:
        for name, item in _object(value, "headers").items():
            if not isinstance(name, str) or not _HEADER.fullmatch(name):
                raise NativeError("configuration", "Invalid HTTP header name")
            lowered = name.lower()
            if lowered in _BLOCKED_HEADERS or lowered.startswith("proxy-"):
                continue
            item = _text(item, "header value")
            if "\r" in item or "\n" in item:
                raise NativeError("configuration", "HTTP header values must not contain newlines")
            result[lowered] = (name, item)
    return dict(result.values())


def _body(value: Any, allowed: set[str]) -> dict[str, Any]:
    def expand(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: expand(val) for key, val in item.items()}
        if isinstance(item, list):
            return [expand(val) for val in item]
        return _env(item)

    return {key: expand(item) for key, item in _object(value, "extra_body").items() if key in allowed}


def _paths(value: Any) -> dict[str, str]:
    result = {}
    for name, path in _object(value, "paths").items():
        path = _text(path, "endpoint path")
        parsed = urlsplit(path)
        if (
            not isinstance(name, str)
            or not path
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or "\\" in path
            or any(segment in {".", ".."} for segment in parsed.path.split("/"))
        ):
            raise NativeError("configuration", "Endpoint paths must be local paths without traversal")
        result[name] = path
    return result


def _timeout(value: Any) -> float:
    try:
        result = float(_env(value))
    except (ValueError, TypeError):
        raise NativeError("configuration", "timeout must be a positive number") from None
    if not math.isfinite(result) or result <= 0:
        raise NativeError("configuration", "timeout must be a positive number")
    return result


async def _call(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class ProviderResolver:
    """Current mode follows UMO configuration, never selected_provider or fallback.

    transport is an optional httpx.AsyncBaseTransport (e.g. MockTransport).
    Identification sends only unauthenticated GETs and never invokes a model.
    """

    def __init__(self, context, settings, fingerprint_key: bytes, *, transport=None):
        if not isinstance(fingerprint_key, bytes) or not fingerprint_key:
            raise ValueError("fingerprint_key must be nonempty bytes")
        self.context = context
        self.settings = settings
        self._fingerprint_key = fingerprint_key
        self._transport = transport
        self._identifications: dict[str, tuple[float, dict[str, Any]]] = {}
        self._connection_identifications: dict[str, dict[str, Any]] = {}
        self._identification_lock = asyncio.Lock()

    async def resolve(self, event, feature: str) -> Connection:
        try:
            return await self._resolve(event, feature)
        except NativeError:
            raise
        except Exception:
            # AstrBot lookup errors may contain raw provider configuration.
            raise NativeError("configuration", "Could not resolve loaded provider configuration") from None

    async def _resolve(self, event, feature: str) -> Connection:
        if feature not in {"search", "image"}:
            raise NativeError("configuration", "Unknown native feature")
        cfg = deepcopy(self.settings.feature(feature))
        if not cfg.get("enabled", True):
            raise NativeError("disabled", f"{feature} is disabled")
        mode = cfg.get("mode", "current")
        provider = None
        provider_id = ""
        source: dict[str, Any] = {}
        if mode in {"current", "provider"}:
            if mode == "current":
                provider_id = await _call(self.context.get_current_chat_provider_id(event.unified_msg_origin))
            else:
                provider_id = _text(cfg.get("provider_id"), "provider_id")
            if not provider_id:
                raise NativeError("configuration", "No provider is configured for this feature")
            provider = await _call(self.context.get_provider_by_id(provider_id))
            if provider is None:
                raise NativeError("configuration", "The configured provider is not loaded")
            source = _object(getattr(provider, "provider_config", None), "provider_config")
            if not callable(getattr(provider, "get_model", None)):
                raise NativeError("configuration", "The selected provider has no chat model interface")
        elif mode != "manual":
            raise NativeError("configuration", "mode must be current, provider, or manual")

        # Provider modes inherit that provider's endpoint/key; manual fields cannot reroute A.
        base_url = _base_url(
            cfg.get("base_url") if mode == "manual" else source.get("api_base") or "https://api.openai.com/v1"
        )
        if mode == "manual":
            key = self._first_key(cfg.get("keys"))
        else:
            key = ""
            getter = getattr(provider, "get_current_key", None)
            if callable(getter):
                key = _text(await _call(getter()), "provider key")
            if not key:
                getter = getattr(provider, "get_keys", None)
                keys = await _call(getter()) if callable(getter) else source.get("key")
                key = self._first_key(keys)
        if not key:
            raise NativeError("configuration", "No API key is configured")

        provider_model = _text(await _call(provider.get_model()), "provider model") if provider else ""
        model = _text(cfg.get("model"), "model")
        if feature == "image" and not model:
            raise NativeError("configuration", "Image model must be configured explicitly")
        model = model or provider_model
        if not model:
            raise NativeError("configuration", "Search model must be configured")
        host_model = _text(cfg.get("host_model"), "host_model") or provider_model
        if feature == "search":
            host_model = host_model or model
        network = self.settings.network
        headers = _headers(source.get("custom_headers"), cfg.get("headers"))
        proxy = _text(cfg.get("proxy") or source.get("proxy") or network.get("proxy"), "proxy") or None
        if proxy:
            try:
                parsed_proxy = urlsplit(proxy)
                valid_proxy = (
                    parsed_proxy.scheme in {"http", "https", "socks5", "socks5h"}
                    and parsed_proxy.hostname
                    and not parsed_proxy.fragment
                    and not parsed_proxy.query
                )
                parsed_proxy.port
            except ValueError:
                valid_proxy = False
            if not valid_proxy:
                raise NativeError("configuration", "Invalid proxy URL")
        timeout = _timeout(cfg.get("timeout") or source.get("timeout") or network.get("timeout") or 120)
        service_type = _text(cfg.get("service_type", "auto"), "service_type").lower()
        if service_type not in _SERVICES:
            raise NativeError("configuration", "Unknown service_type")
        if service_type == "auto":
            identification = await self._identify(base_url, proxy, timeout)
            service_type = identification["type"]
        else:
            identification = {
                "type": service_type,
                "confidence": "manual",
                "evidence": ["manual:service_type"],
            }
        protocol = _text(cfg.get("protocol", "auto"), "protocol").lower()
        if protocol == "auto":
            protocol = (
                "alpha"
                if feature == "search" and service_type in {"sub2api", "newapi"}
                else "responses"
                if feature == "search"
                else "images"
            )
        if protocol not in ({"alpha", "responses"} if feature == "search" else {"images", "responses"}):
            raise NativeError("configuration", f"Invalid {feature} protocol")
        if feature == "image" and protocol == "responses" and not host_model:
            raise NativeError("configuration", "Responses image protocol requires an explicit host_model")
        allowed = _SEARCH_BODY if feature == "search" or protocol == "responses" else _IMAGE_BODY
        extra_body = {
            **_body(source.get("custom_extra_body"), allowed),
            **_body(cfg.get("extra_body"), allowed),
        }
        paths = _paths(cfg.get("paths"))
        identity = {
            "provider_id": provider_id,
            "base_url": base_url,
            "key": key,
            "model": model,
            "host_model": host_model,
            "protocol": protocol,
            "service_type": service_type,
            "headers": {name.lower(): val for name, val in headers.items()},
            "extra_body": extra_body,
            "proxy": proxy,
            "timeout": timeout,
            "paths": paths,
        }
        try:
            encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        except (ValueError, TypeError):
            raise NativeError("configuration", "Connection configuration must be JSON serializable") from None
        fingerprint = hmac.new(self._fingerprint_key, encoded, hashlib.sha256).hexdigest()
        self._connection_identifications[fingerprint] = deepcopy(identification)
        return Connection(
            provider_id=provider_id,
            base_url=base_url,
            key=key,
            model=model,
            host_model=host_model,
            protocol=protocol,
            service_type=service_type,
            headers=headers,
            extra_body=extra_body,
            proxy=proxy,
            timeout=timeout,
            paths=paths,
            fingerprint=fingerprint,
        )

    @staticmethod
    def _first_key(values: Any) -> str:
        values = _env(values)
        if isinstance(values, str):
            values = [values]
        if values is None:
            return ""
        if not isinstance(values, (list, tuple)):
            raise NativeError("configuration", "keys must be a list of strings")
        for value in values:
            key = _text(value, "API key")
            if key:
                return key
        return ""

    def identification(self, conn: Connection) -> dict[str, Any]:
        """Return advisory status evidence without network access or connection secrets."""
        return deepcopy(
            self._connection_identifications.get(
                conn.fingerprint,
                {
                    "type": conn.service_type,
                    "confidence": "low",
                    "evidence": ["not_resolved"],
                },
            )
        )

    async def _identify(self, base_url: str, proxy: str | None, timeout: float) -> dict[str, Any]:
        if urlsplit(base_url).hostname == "api.openai.com":
            return {"type": "openai", "confidence": "high", "evidence": ["domain:api.openai.com"]}
        cache_key = hashlib.sha256(json.dumps([base_url, proxy], separators=(",", ":")).encode()).hexdigest()
        async with self._identification_lock:
            cached = self._identifications.get(cache_key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            result = await self._probe(base_url, proxy, min(timeout, 5))
            self._identifications[cache_key] = (time.monotonic() + 300, result)
            return result

    async def _probe(self, base_url: str, proxy: str | None, timeout: float) -> dict[str, Any]:
        parsed = urlsplit(base_url)
        prefix = re.sub(r"/v1/?$", "", parsed.path.rstrip("/"))
        root = urlunsplit((parsed.scheme, parsed.netloc, prefix, "", "")).rstrip("/")
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                proxy=proxy if self._transport is None else None,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                for endpoint in ("/api/status", "/api/v1/settings/public"):
                    async with client.stream("GET", root + endpoint) as response:
                        for name in ("new-api-version", "x-new-api-version"):
                            if response.headers.get(name, "").strip():
                                return {
                                    "type": "newapi",
                                    "confidence": "high",
                                    "evidence": [f"header:{name}"],
                                }
                        if endpoint != "/api/v1/settings/public" or response.status_code != 200:
                            continue
                        chunks = bytearray()
                        async for chunk in response.aiter_bytes():
                            chunks.extend(chunk)
                            if len(chunks) > 65536:
                                break
                        else:
                            try:
                                payload = json.loads(chunks)
                            except (ValueError, UnicodeDecodeError):
                                continue
                            if self._sub2api_settings(payload):
                                return {
                                    "type": "sub2api",
                                    "confidence": "high",
                                    "evidence": ["structure:/api/v1/settings/public:PublicSettings"],
                                }
        except (httpx.HTTPError, OSError, ValueError):
            pass  # Advisory identification never implies model support or authentication failure.
        return {"type": "unknown", "confidence": "low", "evidence": ["no_matching_public_evidence"]}

    @staticmethod
    def _sub2api_settings(payload: Any) -> bool:
        if not isinstance(payload, dict) or payload.get("code") != 0:
            return False
        data = payload.get("data")
        if not isinstance(data, dict):
            return False
        # PublicSettings structural signature, independent of customizable site branding.
        boolean_fields = {
            "registration_enabled",
            "email_verify_enabled",
            "linuxdo_oauth_enabled",
            "purchase_subscription_enabled",
            "backend_mode_enabled",
        }
        return (
            all(isinstance(data.get(name), bool) for name in boolean_fields)
            and isinstance(data.get("api_base_url"), str)
            and isinstance(data.get("version"), str)
            and bool(data["version"])
        )
