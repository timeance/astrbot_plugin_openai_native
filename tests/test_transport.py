# SPDX-License-Identifier: Apache-2.0
"""Transport tests use in-memory HTTP streams; no live credentials or services."""

import base64
import json
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from urllib.parse import quote

import httpx
import pytest

from native.config import Settings
from native.models import Connection, NativeError
from native.transport import HTTPTransport


def connection(**kwargs):
    return replace(
        Connection("test", "https://api.example/prefix/v1", "secret-key", model="image-model"), **kwargs
    )


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks, fail=False):
        self.chunks, self.fail = chunks, fail

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.fail:
            raise httpx.ReadError("Authorization secret-key https://cdn.example/?signature=secret")


async def public_dns(host, port):
    return ["93.184.216.34"]


@pytest.mark.parametrize("failure", ["timeout", "disconnect", "server"])
async def test_paid_post_is_sent_only_once(failure):
    calls = []

    async def handler(request):
        calls.append(request)
        if failure == "server":
            return httpx.Response(503, json={"error": {"message": "secret-key"}})
        if failure == "timeout":
            raise httpx.ReadTimeout("secret-key")
        raise httpx.ReadError("secret-key")

    transport = HTTPTransport(Settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(NativeError) as caught:
            await transport.post(connection(), "generations", {"prompt": "a tree"})
        assert caught.value.ambiguous
        assert len(calls) == 1
        assert "secret-key" not in str(caught.value.to_dict())
    finally:
        await transport.close()


@pytest.mark.parametrize(
    ("status", "code", "category", "ambiguous"),
    [
        (400, None, "invalid_request", False),
        (400, "unsupported_parameter", "unsupported_parameter", False),
        (401, "invalid_api_key", "authentication", False),
        (403, None, "permission_denied", False),
        (404, "model_not_found", "model_not_found", False),
        (429, "insufficient_quota", "quota_exceeded", False),
        (429, None, "rate_limited", False),
        (408, None, "upstream_error", True),
        (500, None, "upstream_error", True),
    ],
)
async def test_precise_errors_without_provider_text(status, code, category, ambiguous):
    def handler(request):
        return httpx.Response(
            status,
            json={
                "error": {
                    "code": code,
                    "type": "invalid_request_error",
                    "message": "Authorization: Bearer secret-key https://cdn.example/?signature=signed-secret",
                }
            },
        )

    transport = HTTPTransport(Settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(NativeError) as caught:
            await transport.post(connection(), "generations", {})
        error = caught.value
        assert (error.category, error.ambiguous, error.status_code) == (category, ambiguous, status)
        assert all(
            secret not in str(error.to_dict())
            for secret in ["secret-key", "Authorization", "signed-secret", "https://"]
        )
    finally:
        await transport.close()


async def test_routes_keep_prefix_and_overrides_and_edit_multipart():
    observed = []

    async def handler(request):
        observed.append((request, await request.aread()))
        return httpx.Response(200, json={"data": [{"url": "https://cdn.example/image"}]})

    transport = HTTPTransport(Settings(), transport=httpx.MockTransport(handler))
    try:
        await transport.post(connection(), "generations", {"model": "images"})
        await transport.post(connection(), "alpha_search", {"commands": {}})
        await transport.post(
            connection(
                paths={"edits": "/v1/custom/edits"}, headers={"Content-Type": "incorrect", "X-Test": "value"}
            ),
            "edits",
            {"n": 1, "frame_count": 7},
            files=[
                ("image[]", ("one.png", b"one-image", "image/png")),
                ("image[]", ("two.webp", b"two-image", "image/webp")),
            ],
        )
        assert observed[0][0].url.path == "/prefix/v1/images/generations"
        assert observed[1][0].url.path == "/prefix/v1/alpha/search"
        request, body = observed[2]
        assert request.url.path == "/prefix/v1/custom/edits"
        assert request.headers["authorization"] == "Bearer secret-key"
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + body
        )
        parts = list(message.iter_parts())
        images = [p for p in parts if p.get_param("name", header="content-disposition") == "image[]"]
        assert [p.get_payload(decode=True) for p in images] == [b"one-image", b"two-image"]
        assert [p.get_content_type() for p in images] == ["image/png", "image/webp"]
        fields = {
            p.get_param("name", header="content-disposition"): p.get_payload(decode=True)
            for p in parts
            if not p.get_filename()
        }
        assert fields == {"n": b"1", "frame_count": b"7"}
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "payload",
    [
        b'{"status":"in_progress"}',
        b'{"status":"completed"',
        b"[]",
        b'{"status":"completed","error":{"message":"secret-key"}}',
    ],
)
async def test_json_requires_complete_responses(payload):
    transport = HTTPTransport(
        Settings(), transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
    )
    try:
        with pytest.raises(NativeError) as caught:
            await transport.post(connection(), "responses", {})
        assert caught.value.ambiguous
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "payload",
    [
        b'data: {"type":"response.output_text.delta","delta":"hello"}\n\n',
        b"data: [DONE]\n\n",
        b'data: {"type":"response.completed","response":{"status":"completed"}}',
        b'data: {"type":"response.completed","response":{"status":"in_progress"}}\n\n',
        b'data: {"type":"response.failed"}\n\n',
    ],
)
async def test_sse_partial_done_or_incomplete_is_ambiguous(payload):
    transport = HTTPTransport(
        Settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Chunks(payload)
            )
        ),
    )
    try:
        with pytest.raises(NativeError) as caught:
            await transport.post(connection(), "responses", {})
        expected = "upstream_error" if b"response.failed" in payload else "protocol_error"
        assert caught.value.category == expected
        assert caught.value.ambiguous
    finally:
        await transport.close()


@pytest.mark.parametrize(
    ("route", "event", "expected"),
    [
        (
            "responses",
            {"type": "response.completed", "response": {"status": "completed", "output": [], "id": "resp_1"}},
            {"status": "completed", "output": [], "id": "resp_1"},
        ),
        (
            "generations",
            {"type": "image_generation.completed", "b64_json": "aW1hZ2U="},
            {"data": [{"b64_json": "aW1hZ2U="}], "usage": {}},
        ),
        (
            "edits",
            {"type": "image_edit.completed", "b64_json": "aW1hZ2U="},
            {"data": [{"b64_json": "aW1hZ2U="}], "usage": {}},
        ),
    ],
)
async def test_sse_accepts_only_complete_delimited_terminal_event(route, event, expected):
    raw = ("event: " + event["type"] + "\r\ndata: " + json.dumps(event) + "\r\n\r\n").encode()
    transport = HTTPTransport(
        Settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=Chunks(raw[:11], raw[11:49], raw[49:]),
            )
        ),
    )
    try:
        assert await transport.post(connection(), route, {}) == expected
    finally:
        await transport.close()


async def test_sse_stream_failure_does_not_repeat_paid_post():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=Chunks(b'data: {"type":"response.created"}\n\n', fail=True),
        )

    transport = HTTPTransport(Settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(NativeError) as caught:
            await transport.post(connection(), "responses", {})
        assert caught.value.ambiguous and len(calls) == 1
        assert "secret-key" not in str(caught.value)
    finally:
        await transport.close()


@pytest.mark.parametrize("sse", [False, True])
async def test_post_response_limit_covers_json_and_sse(sse):
    payload = b"x" * 65542
    transport = HTTPTransport(
        Settings({"storage": {"max_output_bytes": 1}}),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream" if sse else "application/json"},
                stream=Chunks(payload),
            )
        ),
    )
    try:
        with pytest.raises(NativeError) as caught:
            await transport.post(connection(), "generations", {})
        assert caught.value.ambiguous
        assert "limit" in str(caught.value)
    finally:
        await transport.close()


async def test_download_uses_no_api_auth_or_cookies_and_pins_dns(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"data": []}, headers={"Set-Cookie": "session=private; Path=/"})
        return httpx.Response(200, content=b"image")

    transport = HTTPTransport(Settings(), transport=httpx.MockTransport(handler))
    monkeypatch.setattr(transport, "_resolve_host", public_dns)
    try:
        await transport.post(
            connection(headers={"X-API-Key": "another-secret", "Cookie": "x=private"}), "generations", {}
        )
        assert await transport.download("https://api.example/image?signature=signed-secret", 10) == b"image"
        request = calls[-1]
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "api.example"
        assert request.extensions["sni_hostname"] == "api.example"
        assert not {"authorization", "cookie", "x-api-key", "proxy-authorization"} & set(request.headers)
    finally:
        await transport.close()


async def test_get_retries_at_most_three_times(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    transport = HTTPTransport(
        Settings({"network": {"download_attempts": 20}}), transport=httpx.MockTransport(handler)
    )
    monkeypatch.setattr(transport, "_resolve_host", public_dns)
    try:
        with pytest.raises(NativeError) as caught:
            await transport.download("https://cdn.example/image?signature=secret", 100)
        assert len(calls) == 3 and not caught.value.ambiguous
        assert "signature" not in str(caught.value)
    finally:
        await transport.close()


async def test_get_retries_transient_failure_and_succeeds(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, content=b"image")

    transport = HTTPTransport(Settings(), transport=httpx.MockTransport(handler))
    monkeypatch.setattr(transport, "_resolve_host", public_dns)
    try:
        assert await transport.download("https://cdn.example/image", 10) == b"image"
        assert len(calls) == 2
    finally:
        await transport.close()


@pytest.mark.parametrize("target", ["http://127.0.0.1/secret", "https://internal.example/secret"])
async def test_redirect_revalidates_private_destination(monkeypatch, target):
    calls = []

    async def dns(host, port):
        return ["93.184.216.34"] if host == "cdn.example" else ["127.0.0.1"]

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": target})

    transport = HTTPTransport(Settings(), transport=httpx.MockTransport(handler))
    monkeypatch.setattr(transport, "_resolve_host", dns)
    try:
        with pytest.raises(NativeError) as caught:
            await transport.download("https://cdn.example/image", 10)
        assert caught.value.category == "download_blocked" and len(calls) == 1
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "host, allowed",
    [
        ("internal.example", True),
        ("sub.internal.example", False),
    ],
)
async def test_private_host_allowlist_is_exact(monkeypatch, host, allowed):
    transport = HTTPTransport(
        Settings({"network": {"allow_private_download_hosts": ["internal.example"]}}),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"ok")),
    )

    async def dns(host, port):
        return ["10.0.0.1"]

    monkeypatch.setattr(transport, "_resolve_host", dns)
    try:
        if allowed:
            assert await transport.download("https://" + host + "/image", 10) == b"ok"
        else:
            with pytest.raises(NativeError, match="non-public"):
                await transport.download("https://" + host + "/image", 10)
    finally:
        await transport.close()


async def test_mixed_dns_answer_blocks_entire_target(monkeypatch):
    def handler(request):
        pytest.fail("Blocked download must never connect")

    transport = HTTPTransport(Settings(), transport=httpx.MockTransport(handler))

    async def dns(host, port):
        return ["93.184.216.34", "127.0.0.1"]

    monkeypatch.setattr(transport, "_resolve_host", dns)
    try:
        with pytest.raises(NativeError):
            await transport.download("https://cdn.example/image", 10)
    finally:
        await transport.close()


@pytest.mark.parametrize("url", ["file:///secret", "ftp://cdn.example/a", "https://user:pass@cdn.example/a"])
async def test_download_rejects_invalid_scheme_and_userinfo(url):
    transport = HTTPTransport(
        Settings(),
        transport=httpx.MockTransport(lambda request: pytest.fail("Invalid URL must never connect")),
    )
    try:
        with pytest.raises(NativeError) as caught:
            await transport.download(url, 10)
        assert caught.value.category == "download_blocked"
    finally:
        await transport.close()


@pytest.mark.parametrize("declared", [True, False])
async def test_download_enforces_declared_and_streamed_byte_limit(monkeypatch, declared):
    transport = HTTPTransport(
        Settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-length": "6"} if declared else {}, stream=Chunks(b"123", b"456")
            )
        ),
    )
    monkeypatch.setattr(transport, "_resolve_host", public_dns)
    try:
        with pytest.raises(NativeError) as caught:
            await transport.download("https://cdn.example/a", 5)
        assert caught.value.category == "download_too_large"
    finally:
        await transport.close()


async def test_useful_error_message_is_sanitized_and_request_id_is_returned():
    conn = connection(headers={"X-Custom-Secret": "header-secret"})

    def handler(request):
        return httpx.Response(
            400,
            headers={"x-request-id": "req_test-123"},
            json={
                "error": {
                    "type": "invalid_request_error",
                    "code": "unsupported_value",
                    "message": "Invalid quality 'ultra'. secret-key header-secret "
                    "Authorization: Bearer other-token https://cdn.example/a?signature=secret "
                    "key=another-secret password=pass",
                }
            },
        )

    transport = HTTPTransport(Settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(NativeError) as caught:
            await transport.post(conn, "generations", {})
        error = caught.value
        assert "Invalid quality 'ultra'." in error.message
        assert error.request_id == "req_test-123"
        assert error.upstream_code == "unsupported_value" and not error.ambiguous
        assert all(
            secret not in str(error.to_dict())
            for secret in [
                "secret-key",
                "header-secret",
                "other-token",
                "https://",
                "signature=secret",
                "another-secret",
                "password=pass",
            ]
        )
    finally:
        await transport.close()


async def test_sse_error_message_is_sanitized_with_request_id():
    event = {
        "type": "error",
        "error": {
            "code": "billing_limit",
            "message": "Billing limit reached secret-key https://cdn.example/?sig=secret",
        },
    }
    transport = HTTPTransport(
        Settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream", "x-request-id": "req_stream"},
                stream=Chunks(("data: " + json.dumps(event) + "\n\n").encode()),
            )
        ),
    )
    try:
        with pytest.raises(NativeError) as caught:
            await transport.post(connection(), "responses", {})
        error = caught.value
        assert error.ambiguous and error.request_id == "req_stream"
        assert "Billing limit reached" in str(error)
        assert "secret-key" not in str(error) and "https://" not in str(error)
    finally:
        await transport.close()


async def test_proxy_credentials_and_masked_keys_are_scrubbed_from_http_errors():
    conn = connection(
        key="native-prefix-MIDDLE-suffix",
        headers={"X-Custom-Secret": "header/token"},
        proxy="http://proxy-user:p%40ssword@proxy.example:8080",
    )
    basic = base64.b64encode(b"proxy-user:p@ssword").decode()
    details = (
        "Invalid model selected. p@ssword p%40ssword proxy-user "
        + basic
        + " header/token "
        + quote("header/token", safe="")
        + " nati...ffix sk-proj-abc...xyz sk-abc****xyz "
        "'token': 'unknown-sensitive' socks5://user:pass@proxy.example "
        "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoxfQ.c2lnbmF0dXJl"
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            400,
            headers={"request-id": "req_proxy"},
            json={
                "error": {
                    "type": "invalid_request_error",
                    "code": "unsupported_parameter",
                    "message": details,
                }
            },
        )

    transport = HTTPTransport(Settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(NativeError) as caught:
            await transport.post(conn, "responses", {})
        error = caught.value
        assert "Invalid model selected." in error.message and len(error.message) < 600
        assert error.request_id == "req_proxy" and error.upstream_type == "invalid_request_error"
        assert error.category == "unsupported_parameter" and error.upstream_code == "unsupported_parameter"
        assert not error.ambiguous and len(calls) == 1
        assert all(
            secret not in str(error.to_dict())
            for secret in [
                "p@ssword",
                "p%40ssword",
                "proxy-user",
                basic,
                "header/token",
                "header%2Ftoken",
                "nati...ffix",
                "sk-proj-abc",
                "sk-abc",
                "xyz",
                "unknown-sensitive",
                "socks5://",
                "eyJhbGciOiJIUzI1NiJ9",
            ]
        )
    finally:
        await transport.close()


async def test_generic_403_does_not_invent_a_specific_permission_failure():
    transport = HTTPTransport(
        Settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                403, headers={"x-request-id": "req_denied"}, json={"error": {"message": "Forbidden."}}
            )
        ),
    )
    try:
        with pytest.raises(NativeError) as caught:
            await transport.post(connection(), "alpha_search", {})
        error = caught.value
        assert error.category == "permission_denied" and not error.ambiguous
        assert error.upstream_type is None and error.upstream_code is None
        assert error.message == "Upstream request rejected (HTTP 403): Forbidden."
    finally:
        await transport.close()


async def test_sse_failed_event_retains_nested_error_details_and_request_id():
    event = {
        "type": "response.failed",
        "response": {
            "status": "failed",
            "error": {
                "type": "server_error",
                "code": "execution_failed",
                "message": "Execution stopped secret-key https://private.example/?token=sensitive",
            },
        },
    }
    transport = HTTPTransport(
        Settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream", "x-request-id": "req_failed"},
                stream=Chunks(("data: " + json.dumps(event) + "\n\n").encode()),
            )
        ),
    )
    try:
        with pytest.raises(NativeError) as caught:
            await transport.post(connection(), "responses", {})
        error = caught.value
        assert error.category == "upstream_error" and error.ambiguous
        assert error.upstream_type == "server_error" and error.upstream_code == "execution_failed"
        assert error.request_id == "req_failed" and "Execution stopped" in error.message
        assert "secret-key" not in error.message and "private.example" not in error.message
    finally:
        await transport.close()


async def test_sse_image_completion_event_with_in_progress_status_is_rejected():
    event = {"type": "image_generation.completed", "status": "in_progress", "b64_json": "aW1hZ2U="}
    transport = HTTPTransport(
        Settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=Chunks(("data: " + json.dumps(event) + "\n\n").encode()),
            )
        ),
    )
    try:
        with pytest.raises(NativeError) as caught:
            await transport.post(connection(), "generations", {})
        assert caught.value.ambiguous and caught.value.category == "protocol_error"
    finally:
        await transport.close()


async def test_transport_byte_limit_allows_base64_expansion():
    maximum = 262144
    encoded = base64.b64encode(b"x" * maximum).decode()
    transport = HTTPTransport(
        Settings({"storage": {"max_output_bytes": maximum}}),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [{"b64_json": encoded}]})
        ),
    )
    try:
        payload = await transport.post(connection(), "generations", {})
        assert payload["data"][0]["b64_json"] == encoded
    finally:
        await transport.close()


async def test_download_redirect_loop_uses_at_most_three_gets(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://cdn.example/again"})

    transport = HTTPTransport(Settings(), transport=httpx.MockTransport(handler))
    monkeypatch.setattr(transport, "_resolve_host", public_dns)
    try:
        with pytest.raises(NativeError, match="request limit"):
            await transport.download("https://cdn.example/a", 10)
        assert len(calls) == 3 and all(request.method == "GET" for request in calls)
    finally:
        await transport.close()


@pytest.mark.parametrize("status", [400, 403])
@pytest.mark.parametrize(
    "code",
    [
        "unsupported_endpoint",
        "unsupported_channel",
        "unsupported_model",
        "unsupported_format",
        "unsupported_capability",
        "model_not_supported",
        "endpoint_not_supported",
        "account_permission",
        "auth_failed",
    ],
)
async def test_explicit_capability_and_account_codes_reach_service_categories(status, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={"x-request-id": "req_explicit"},
            json={
                "error": {
                    "type": "invalid_request_error",
                    "code": code,
                    "message": "Requested operation is unavailable. secret-key",
                }
            },
        )

    transport = HTTPTransport(Settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(NativeError) as caught:
            await transport.post(connection(), "alpha_search", {})
        error = caught.value
        assert error.category == code and error.upstream_code == code
        assert error.status_code == status and not error.ambiguous
        assert error.upstream_type == "invalid_request_error" and error.request_id == "req_explicit"
        assert "Requested operation is unavailable." in error.message
        assert "secret-key" not in error.message and len(calls) == 1
    finally:
        await transport.close()


@pytest.mark.parametrize("status", [400, 403])
@pytest.mark.parametrize("code", [None, "unsupported_parameter", "unsupported_value", "unsupported_unknown"])
async def test_generic_and_parameter_errors_never_claim_capability_or_account_rejection(status, code):
    transport = HTTPTransport(
        Settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                json={
                    "error": {
                        "type": "unsupported_endpoint",
                        "code": code,
                        "message": "unsupported_endpoint unsupported_model account_permission",
                    }
                },
            )
        ),
    )
    try:
        with pytest.raises(NativeError) as caught:
            await transport.post(connection(), "responses", {})
        expected = (
            code
            if code in {"unsupported_parameter", "unsupported_value"}
            else {
                400: "invalid_request",
                403: "permission_denied",
            }[status]
        )
        assert caught.value.category == expected and not caught.value.ambiguous
    finally:
        await transport.close()


@pytest.mark.parametrize("status", [408, 499, 503])
@pytest.mark.parametrize("code", ["unsupported_endpoint", "account_permission"])
async def test_ambiguous_http_status_takes_priority_over_explicit_blocking_code(status, code):
    transport = HTTPTransport(
        Settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, json={"error": {"code": code, "message": "Execution uncertain"}}
            )
        ),
    )
    try:
        with pytest.raises(NativeError) as caught:
            await transport.post(connection(), "responses", {})
        assert caught.value.category == "upstream_error" and caught.value.ambiguous
        assert caught.value.upstream_code == code
    finally:
        await transport.close()
