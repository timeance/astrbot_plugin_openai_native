# SPDX-License-Identifier: Apache-2.0
"""Single-attempt paid requests and isolated, bounded result downloads."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import re
import socket
from typing import Any
from urllib.parse import quote, quote_plus, unquote, urljoin, urlsplit

import httpx

from .models import Connection, NativeError

_ROUTES = {
    "alpha_search": "/v1/alpha/search",
    "responses": "/v1/responses",
    "generations": "/v1/images/generations",
    "edits": "/v1/images/edits",
}
_COMPLETIONS = {"image_generation.completed", "image_edit.completed"}
_TRANSIENT_GET = {408, 425, 429}


def _failure(message: str = "The upstream response was not complete") -> NativeError:
    return NativeError("protocol_error", message, ambiguous=True)


def _enum(value: Any, secrets: list[str]) -> str | None:
    if not isinstance(value, str) or any(secret and secret in value for secret in secrets):
        return None
    return value if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value) else None


def _secrets(conn: Connection) -> list[str]:
    values = [conn.key, *conn.headers.values()]
    for header in conn.headers.values():
        match = re.match(r"(?:Bearer|Basic)\s+(.+)", str(header), re.IGNORECASE)
        if match:
            values.append(match[1])
    if conn.proxy:
        values.append(conn.proxy)
        try:
            proxy = urlsplit(conn.proxy)
            for part in (proxy.username, proxy.password):
                if part:
                    values.extend((part, unquote(part)))
            if proxy.username is not None and proxy.password is not None:
                credentials = unquote(proxy.username) + ":" + unquote(proxy.password)
                values.extend((credentials, base64.b64encode(credentials.encode()).decode("ascii")))
        except ValueError:
            pass
    return [str(value) for value in values if value]


def _safe_message(value: Any, secrets: list[str]) -> str:
    """Keep actionable diagnostics, never credentials or access-bearing URLs."""
    if not isinstance(value, str):
        return ""
    for secret in sorted((str(item) for item in secrets if item), key=len, reverse=True):
        for variant in {secret, quote(secret, safe=""), quote_plus(secret)}:
            value = value.replace(variant, "[redacted]")
    value = re.sub(r"(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>\"']+", "[redacted URL]", value, flags=re.IGNORECASE)
    value = re.sub(
        r"\b(?:authorization|proxy-authorization)\s*[:=]?\s*(?:bearer\s+|basic\s+)?[^\s,;]+",
        "[redacted]",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\b(?:bearer|basic)\s+[A-Za-z0-9_./+~=-]+", "[redacted]", value, flags=re.IGNORECASE)
    value = re.sub(
        r"\b(?:api[_ -]?key|key|access[_ -]?token|token|signature|sig|password|cookie|client_secret)"
        r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        "[redacted]",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\bsk-[A-Za-z0-9_.*…-]+", "[redacted]", value)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[redacted]", value)
    # Providers sometimes echo a masked prefix/suffix instead of the full credential.
    for secret in secrets:
        if len(secret) >= 8:
            value = re.sub(
                re.escape(secret[:4])
                + r"[A-Za-z0-9_-]*(?:\.{2,}|…|\*+)"
                + r"[A-Za-z0-9_-]*"
                + re.escape(secret[-4:]),
                "[redacted]",
                value,
            )
    return " ".join(value.split())[:512]


def _request_id(headers: httpx.Headers, secrets: list[str]) -> str | None:
    value = headers.get("x-request-id") or headers.get("request-id")
    if (
        not value
        or any(secret and str(secret) in value for secret in secrets)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value)
    ):
        return None
    return value


def _execution_error(payload: Any, secrets: list[str]) -> NativeError:
    response = payload.get("response", payload) if isinstance(payload, dict) else {}
    error = response.get("error", response) if isinstance(response, dict) else {}
    error = error if isinstance(error, dict) else {}
    detail = _safe_message(error.get("message"), secrets)
    return NativeError(
        "upstream_error",
        "Upstream execution failed" + (": " + detail if detail else ""),
        ambiguous=True,
        upstream_type=_enum(error.get("type"), secrets),
        upstream_code=_enum(error.get("code"), secrets),
    )


def _http_error(status: int, payload: Any, secrets: list[str], request_id: str | None = None) -> NativeError:
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        error = {}
    code = _enum(error.get("code"), secrets)
    kind = _enum(error.get("type"), secrets)
    ambiguous = status >= 500 or status in {408, 499}
    category = {
        400: "invalid_request",
        401: "authentication",
        403: "permission_denied",
        404: "not_found",
        409: "conflict",
        422: "invalid_request",
        429: "rate_limited",
    }.get(status, "request_rejected")
    if ambiguous or code == "upstream_error" or kind == "upstream_error":
        category, ambiguous = "upstream_error", True
    elif code in {"invalid_api_key", "authentication", "authentication_error"}:
        category = "authentication"
    elif code in {"insufficient_quota", "quota_exceeded"}:
        category = "quota_exceeded"
    elif code == "model_not_found":
        category = "model_not_found"
    elif code in {"content_policy_violation", "moderation_blocked"}:
        category = "policy_rejected"
    elif code in {"unsupported_parameter", "unsupported_value"}:
        category = code
    elif code in {
        "unsupported_endpoint",
        "unsupported_channel",
        "unsupported_model",
        "unsupported_format",
        "unsupported_capability",
        "model_not_supported",
        "endpoint_not_supported",
        "account_permission",
        "auth_failed",
    }:
        # Only explicit capability/account codes may trigger the service's blocking cache.
        category = code
    summary = (
        f"Upstream execution could not be confirmed (HTTP {status})"
        if ambiguous
        else f"Upstream request rejected (HTTP {status})"
    )
    detail = _safe_message(error.get("message"), secrets)
    return NativeError(
        category,
        summary + (": " + detail if detail else ""),
        ambiguous=ambiguous,
        status_code=status,
        upstream_type=kind,
        upstream_code=code,
        request_id=request_id,
    )


class HTTPTransport:
    def __init__(self, settings: Any, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._injected_transport = transport
        self._clients: dict[str | None, httpx.AsyncClient] = {}

    def _client(self, proxy: str | None = None) -> httpx.AsyncClient:
        if proxy not in self._clients:
            try:
                backend = self._injected_transport
                if backend is None:
                    backend = httpx.AsyncHTTPTransport(retries=0, proxy=proxy, trust_env=False)
                self._clients[proxy] = httpx.AsyncClient(
                    transport=backend,
                    follow_redirects=False,
                    trust_env=False,
                )
            except Exception:
                raise NativeError("configuration", "Invalid HTTP transport configuration") from None
        return self._clients[proxy]

    @staticmethod
    def _url(conn: Connection, route: str) -> str:
        if route not in _ROUTES:
            raise NativeError("configuration", "Unknown API route")
        try:
            base = httpx.URL(conn.base_url)
            if (
                base.scheme not in {"http", "https"}
                or not base.host
                or base.userinfo
                or base.query
                or base.fragment
            ):
                raise ValueError
            path = conn.paths.get(route) or _ROUTES[route]
            parsed = urlsplit(path)
            if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
                raise ValueError
            parts = [part for part in path.split("/") if part]
            if any(part in {".", ".."} for part in parts):
                raise ValueError
            prefix = base.path.rstrip("/")
            if prefix.endswith("/v1") and parts and parts[0] == "v1":
                parts = parts[1:]
            return str(base.copy_with(path=prefix + "/" + "/".join(parts)))
        except (ValueError, TypeError, httpx.InvalidURL):
            raise NativeError("configuration", "Invalid API base URL or route path") from None

    async def _read(self, response: httpx.Response, limit: int) -> bytes:
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > limit:
                raise _failure("The upstream response exceeded the configured limit")
            chunks.append(chunk)
        return b"".join(chunks)

    async def post(self, conn: Connection, route: str, body: dict, files=None) -> dict:
        url = self._url(conn, route)
        headers = {str(key): str(value) for key, value in conn.headers.items()}
        if conn.key:
            headers = {key: value for key, value in headers.items() if key.lower() != "authorization"}
            headers["Authorization"] = f"Bearer {conn.key}"
        if files is not None:
            headers = {key: value for key, value in headers.items() if key.lower() != "content-type"}
        secrets = _secrets(conn)
        request_id = None
        try:
            timeout = httpx.Timeout(
                float(conn.timeout),
                connect=min(
                    float(conn.timeout),
                    float(self.settings.network.get("connect_timeout", 10)),
                ),
            )
            kwargs = (
                {"json": body}
                if files is None
                else {
                    "data": {
                        key: json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value)
                        for key, value in body.items()
                    },
                    "files": files,
                }
            )
            request = httpx.Request(
                "POST",
                url,
                headers=headers,
                extensions={"timeout": timeout.as_dict()},
                **kwargs,
            )
            response = await self._client(conn.proxy).send(request, stream=True, follow_redirects=False)
            request_id = _request_id(response.headers, secrets)
            try:
                output_limit = int(self.settings.storage.get("max_output_bytes", 104857600))
                limit = ((output_limit + 2) // 3) * 4 + 65536
                if response.status_code >= 400:
                    raw = await self._read(response, min(limit, 65536))
                    try:
                        error_payload = json.loads(raw)
                    except (ValueError, UnicodeError):
                        error_payload = {}
                    raise _http_error(response.status_code, error_payload, secrets, request_id)
                if not 200 <= response.status_code < 300:
                    raise _failure("Unexpected upstream redirect or HTTP status")
                if "text/event-stream" in response.headers.get("content-type", "").lower():
                    return await self._sse(response, route, limit, secrets)
                raw = await self._read(response, limit)
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise _failure("The upstream returned invalid or truncated JSON") from None
                if not isinstance(payload, dict):
                    raise _failure("The upstream response must be a JSON object")
                if payload.get("error"):
                    raise _execution_error(payload, secrets)
                status = payload.get("status")
                if route == "responses" and status != "completed":
                    raise _failure("The Responses request did not complete")
                if status is not None and status not in {"completed", "succeeded", "success"}:
                    raise _failure()
                return payload
            finally:
                await response.aclose()
        except NativeError as error:
            error.request_id = error.request_id or request_id
            raise
        except httpx.TimeoutException:
            raise NativeError(
                "timeout",
                "Upstream execution timed out; completion is unknown",
                ambiguous=True,
                request_id=request_id,
            ) from None
        except Exception:
            raise NativeError(
                "network_error",
                "Upstream connection failed; completion is unknown",
                ambiguous=True,
                request_id=request_id,
            ) from None

    async def _sse(self, response: httpx.Response, route: str, limit: int, secrets: list[str]) -> dict:
        # Only a complete, delimited terminal event commits a streamed result.
        import codecs

        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        buffer, event_name, lines, size = "", "", [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > limit:
                raise _failure("The streamed response exceeded the configured limit")
            try:
                buffer += decoder.decode(chunk)
            except UnicodeError:
                raise _failure("The streamed response is not valid UTF-8") from None
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.removesuffix("\r")
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    lines.append(line[5:].lstrip())
                elif line == "":
                    if lines:
                        text = "\n".join(lines)
                        if text == "[DONE]":
                            raise _failure("The stream ended without a completed result")
                        try:
                            event = json.loads(text)
                        except ValueError:
                            raise _failure("The stream contained invalid JSON") from None
                        if not isinstance(event, dict):
                            raise _failure()
                        kind = event.get("type") or event_name
                        if event.get("error") or kind in {
                            "error",
                            "upstream_error",
                            "response.failed",
                            "response.incomplete",
                        }:
                            raise _execution_error(event, secrets)
                        if route == "responses" and kind == "response.completed":
                            result = event.get("response")
                            if not isinstance(result, dict) or result.get("status") != "completed":
                                raise _failure()
                            if result.get("error"):
                                raise _execution_error(result, secrets)
                            return result
                        expected = (
                            "image_edit.completed" if route == "edits" else "image_generation.completed"
                        )
                        if route in {"generations", "edits"} and kind in _COMPLETIONS:
                            if kind != expected:
                                raise _failure("The stream completed a different operation")
                            if event.get("status", "completed") != "completed":
                                raise _failure("The image stream did not complete")
                            data = event.get("data")
                            if data is None:
                                data = [
                                    {
                                        key: event[key]
                                        for key in ("b64_json", "url", "revised_prompt")
                                        if key in event
                                    }
                                ]
                            if not isinstance(data, list) or not data:
                                raise _failure("The completed stream contained no images")
                            return {"data": data, "usage": event.get("usage", {})}
                    lines, event_name = [], ""
        raise _failure("The stream disconnected before a completed result")

    async def _resolve_host(self, host: str, port: int) -> list[str]:
        try:
            rows = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                port,
                type=socket.SOCK_STREAM,
            )
            return list(dict.fromkeys(row[4][0] for row in rows))
        except Exception:
            raise NativeError("download_error", "Download host could not be resolved") from None

    async def _download_target(self, url: str) -> tuple[httpx.URL, str, str]:
        try:
            original = httpx.URL(url)
            if (
                original.scheme not in {"http", "https"}
                or not original.host
                or original.userinfo
                or original.fragment
            ):
                raise ValueError
            host = original.host.rstrip(".").lower()
            if "%" in host:
                raise ValueError
            addresses = await self._resolve_host(
                host, original.port or (443 if original.scheme == "https" else 80)
            )
            if not addresses:
                raise ValueError
            allow = {
                str(value).strip().lower().rstrip(".")
                for value in self.settings.network.get("allow_private_download_hosts", [])
            }
            if host not in allow and any(
                not ipaddress.ip_address(addr).is_global or ipaddress.ip_address(addr).is_multicast
                for addr in addresses
            ):
                raise NativeError("download_blocked", "Download host resolves to a non-public address")
            # Pin the validated address. Host and TLS SNI retain the original hostname.
            pinned = original.copy_with(host=addresses[0])
            default_port = 443 if original.scheme == "https" else 80
            authority = f"[{host}]" if ":" in host else host
            if original.port is not None and original.port != default_port:
                authority += f":{original.port}"
            return pinned, authority, host
        except NativeError:
            raise
        except Exception:
            raise NativeError("download_blocked", "Invalid or disallowed download URL") from None

    async def download(self, url: str, max_bytes: int) -> bytes:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise NativeError("validation", "Download byte limit must be positive")
        current = url
        attempts = max(1, min(3, int(self.settings.network.get("download_attempts", 3))))
        for index in range(attempts):
            response = None
            try:
                pinned, authority, hostname = await self._download_target(current)
                timeout = httpx.Timeout(
                    float(self.settings.network.get("download_timeout", 60)),
                    connect=float(self.settings.network.get("connect_timeout", 10)),
                )
                # A fresh Request bypasses client cookies/default headers and all API credentials.
                request = httpx.Request(
                    "GET",
                    pinned,
                    headers={"Host": authority},
                    extensions={"sni_hostname": hostname, "timeout": timeout.as_dict()},
                )
                response = await self._client().send(request, stream=True, follow_redirects=False)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise NativeError("download_error", "Download redirect has no destination")
                    current = urljoin(current, location)
                    continue
                if response.status_code >= 500 or response.status_code in _TRANSIENT_GET:
                    if index + 1 < attempts:
                        continue
                    raise NativeError("download_error", "Download remained unavailable")
                if response.status_code != 200:
                    raise NativeError(
                        "download_error", "Download was rejected", status_code=response.status_code
                    )
                length = response.headers.get("content-length")
                if length and length.isdecimal() and int(length) > max_bytes:
                    raise NativeError("download_too_large", "Download exceeds the configured byte limit")
                chunks, count = [], 0
                async for chunk in response.aiter_bytes():
                    count += len(chunk)
                    if count > max_bytes:
                        raise NativeError("download_too_large", "Download exceeds the configured byte limit")
                    chunks.append(chunk)
                if count == 0:
                    raise NativeError("download_error", "Download returned an empty body")
                return b"".join(chunks)
            except NativeError:
                raise
            except Exception:
                if index + 1 >= attempts:
                    raise NativeError("download_error", "Download connection failed") from None
            finally:
                if response is not None:
                    await response.aclose()
        raise NativeError("download_error", "Download did not complete within the request limit")

    async def close(self) -> None:
        clients, self._clients = self._clients, {}
        for client in clients.values():
            await client.aclose()
