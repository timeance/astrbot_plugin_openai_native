# SPDX-License-Identifier: Apache-2.0
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from native.config import Settings
from native.models import NativeError
from native.providers import ProviderResolver


def loaded_provider(**config):
    return SimpleNamespace(
        provider_config={"api_base": "https://gateway.invalid/prefix/v1", **config},
        get_model=Mock(return_value="chat-model"),
        get_current_key=Mock(return_value="credential-A"),
        get_keys=Mock(return_value=["credential-A", "credential-B"]),
    )


def context_for(provider):
    return SimpleNamespace(
        get_current_chat_provider_id=AsyncMock(return_value="provider-A"),
        get_provider_by_id=Mock(return_value=provider),
    )


def event(umo="onebot:GroupMessage:123"):
    # Reading selected_provider is deliberately unavailable.
    return SimpleNamespace(unified_msg_origin=umo)


def settings(feature="search", **values):
    return Settings({feature: {"service_type": "unknown", **values}})


@pytest.mark.asyncio
async def test_current_routes_each_exact_umo_and_ignores_manual_fields():
    providers = {"A": loaded_provider(), "B": loaded_provider(api_base="https://b.invalid/v1")}
    context = context_for(providers["A"])
    context.get_current_chat_provider_id.side_effect = lambda umo: "A" if umo.endswith(":1") else "B"
    context.get_provider_by_id.side_effect = providers.get
    resolver = ProviderResolver(
        context,
        settings(base_url="https://wrong.invalid", keys=["wrong-key"], provider_id="B"),
        b"installation-secret",
    )
    first = await resolver.resolve(event("onebot:GroupMessage:1"), "search")
    second = await resolver.resolve(event("onebot:GroupMessage:2"), "search")
    assert (first.provider_id, second.provider_id) == ("A", "B")
    assert first.base_url == "https://gateway.invalid/prefix/v1"
    assert second.base_url == "https://b.invalid/v1"
    assert first.key == "credential-A"
    assert context.get_current_chat_provider_id.await_args_list[0].args == ("onebot:GroupMessage:1",)


@pytest.mark.asyncio
async def test_fixed_provider_does_not_fall_back_to_current():
    context = context_for(loaded_provider())
    resolver = ProviderResolver(context, settings(mode="provider", provider_id="fixed-A"), b"secret")
    conn = await resolver.resolve(event(), "search")
    assert conn.provider_id == "fixed-A"
    context.get_current_chat_provider_id.assert_not_called()
    context.get_provider_by_id.assert_called_once_with("fixed-A")
    context.get_provider_by_id.return_value = None
    with pytest.raises(NativeError, match="not loaded"):
        await resolver.resolve(event(), "search")
    context.get_current_chat_provider_id.assert_not_called()


@pytest.mark.asyncio
async def test_manual_whole_value_env_and_multiple_keys(monkeypatch):
    monkeypatch.setenv("NATIVE_TEST_BASE", "https://manual.invalid/deployment/v1/")
    monkeypatch.setenv("NATIVE_TEST_KEY", "configured-key")
    monkeypatch.setenv("NATIVE_TEST_HEADER", "project-value")
    context = SimpleNamespace()
    resolver = ProviderResolver(
        context,
        settings(
            mode="manual",
            base_url="${NATIVE_TEST_BASE}",
            keys=["", "$NATIVE_TEST_KEY", "unused"],
            model="search-model",
            headers={"OpenAI-Project": "env:NATIVE_TEST_HEADER"},
            paths={"responses": "/custom/responses"},
        ),
        b"secret",
    )
    conn = await resolver.resolve(event(), "search")
    assert conn.base_url == "https://manual.invalid/deployment/v1"
    assert conn.key == "configured-key"
    assert conn.headers == {"OpenAI-Project": "project-value"}
    assert conn.paths == {"responses": "/custom/responses"}
    assert conn.provider_id == ""
    assert conn.protocol == "responses"
    assert "configured-key" not in repr(conn)
    assert "project-value" not in repr(conn)


@pytest.mark.asyncio
async def test_env_is_not_interpolated_inside_values(monkeypatch):
    monkeypatch.setenv("NATIVE_TEST_KEY", "expanded")
    conn = await ProviderResolver(
        SimpleNamespace(),
        settings(
            mode="manual",
            base_url="https://manual.invalid/v1",
            model="search",
            keys=["literal-$NATIVE_TEST_KEY"],
        ),
        b"secret",
    ).resolve(event(), "search")
    assert conn.key == "literal-$NATIVE_TEST_KEY"


@pytest.mark.asyncio
async def test_missing_env_is_a_configuration_error(monkeypatch):
    monkeypatch.delenv("NATIVE_TEST_MISSING", raising=False)
    provider = loaded_provider()
    provider.get_current_key.return_value = "$NATIVE_TEST_MISSING"
    with pytest.raises(NativeError) as caught:
        await ProviderResolver(context_for(provider), settings(), b"secret").resolve(event(), "search")
    assert caught.value.category == "configuration"


@pytest.mark.asyncio
async def test_provider_current_key_wins_and_fallback_uses_get_keys(monkeypatch):
    monkeypatch.setenv("NATIVE_TEST_SECOND_KEY", "resolved-second")
    provider = loaded_provider(key=["wrong-configured-key"])
    context = context_for(provider)
    resolver = ProviderResolver(context, settings(), b"secret")
    assert (await resolver.resolve(event(), "search")).key == "credential-A"
    provider.get_keys.assert_not_called()
    provider.get_current_key.return_value = None
    provider.get_keys.return_value = ["", "${NATIVE_TEST_SECOND_KEY}", "not-selected"]
    conn = await resolver.resolve(event(), "search")
    assert conn.key == "resolved-second"
    assert provider.provider_config["key"] == ["wrong-configured-key"]


@pytest.mark.asyncio
async def test_search_override_and_explicit_image_model():
    provider = loaded_provider()
    context = context_for(provider)
    conn = await ProviderResolver(context, settings(model="native-search"), b"secret").resolve(
        event(), "search"
    )
    assert conn.model == "native-search" and conn.host_model == "chat-model"
    with pytest.raises(NativeError, match="explicitly"):
        await ProviderResolver(context, settings("image"), b"secret").resolve(event(), "image")
    conn = await ProviderResolver(context, settings("image", model="image-model"), b"secret").resolve(
        event(), "image"
    )
    assert conn.model == "image-model"
    assert conn.host_model == "chat-model"
    assert conn.protocol == "images"


@pytest.mark.asyncio
async def test_safe_http_inheritance_is_nonmutating():
    provider = loaded_provider(
        timeout="77",
        proxy="http://proxy.invalid:8080",
        custom_headers={
            "OpenAI-Project": "source",
            "Authorization": "should-not-forward",
            "Cookie": "do-not-forward",
            "Host": "attacker.invalid",
            "X-Route": "source-route",
        },
        custom_extra_body={
            "temperature": 0.4,
            "model": "wrong",
            "tools": ["wrong"],
            "input": "wrong",
            "stream": True,
            "reasoning": {"effort": "low"},
        },
    )
    before = deepcopy(provider.provider_config)
    conn = await ProviderResolver(
        context_for(provider),
        settings(
            headers={"openai-project": "override", "X-Route": "override-route"},
            extra_body={"temperature": 0.7, "messages": [], "model": "also-wrong"},
        ),
        b"secret",
    ).resolve(event(), "search")
    assert {key.lower(): val for key, val in conn.headers.items()} == {
        "openai-project": "override",
        "x-route": "override-route",
    }
    assert conn.extra_body == {"temperature": 0.7, "reasoning": {"effort": "low"}}
    assert conn.proxy == "http://proxy.invalid:8080" and conn.timeout == 77
    conn.extra_body["reasoning"]["effort"] = "high"
    assert provider.provider_config == before


@pytest.mark.asyncio
async def test_endpoint_specific_extra_body():
    provider = loaded_provider(
        custom_extra_body={
            "temperature": 0.3,
            "background": "transparent",
            "n": 100,
            "output_format": "gif",
        }
    )
    conn = await ProviderResolver(context_for(provider), settings("image", model="image"), b"secret").resolve(
        event(), "image"
    )
    assert conn.extra_body == {"background": "transparent"}
    conn = await ProviderResolver(
        context_for(provider), settings("image", model="image", protocol="responses"), b"secret"
    ).resolve(event(), "image")
    assert conn.extra_body == {"temperature": 0.3}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["https://other.invalid/route", "//other.invalid/route", "../route"])
async def test_paths_cannot_reroute_credentials(path):
    with pytest.raises(NativeError, match="Endpoint paths"):
        await ProviderResolver(
            context_for(loaded_provider()), settings(paths={"responses": path}), b"secret"
        ).resolve(event(), "search")


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [-1, "nan", "invalid", "inf"])
async def test_invalid_timeout_is_typed(timeout):
    with pytest.raises(NativeError) as caught:
        await ProviderResolver(context_for(loaded_provider()), settings(timeout=timeout), b"secret").resolve(
            event(), "search"
        )
    assert caught.value.category == "configuration"


@pytest.mark.asyncio
async def test_official_domain_detection_needs_no_probe():
    def handler(request):
        pytest.fail("Official OpenAI detection must not send a probe")

    resolver = ProviderResolver(
        context_for(loaded_provider(api_base="https://api.openai.com/v1")),
        Settings(),
        b"secret",
        transport=httpx.MockTransport(handler),
    )
    conn = await resolver.resolve(event(), "search")
    assert conn.service_type == "openai" and conn.protocol == "responses"


@pytest.mark.asyncio
async def test_newapi_header_detection_is_public_cached_and_preserves_prefix():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url == "https://gateway.invalid/prefix/api/status"
        assert not any(name in request.headers for name in ["authorization", "cookie", "x-secret"])
        return httpx.Response(200, headers={"New-Api-Version": "v0.test"}, json={"success": True})

    provider = loaded_provider(custom_headers={"X-Secret": "header-secret"})
    resolver = ProviderResolver(
        context_for(provider), Settings(), b"secret", transport=httpx.MockTransport(handler)
    )
    first = await resolver.resolve(event(), "search")
    provider.get_current_key.return_value = "credential-B"
    second = await resolver.resolve(event(), "search")
    assert first.service_type == "newapi"
    assert first.protocol == "alpha"
    assert first.base_url == "https://gateway.invalid/prefix/v1"
    assert resolver.identification(first) == {
        "type": "newapi",
        "confidence": "high",
        "evidence": ["header:new-api-version"],
    }
    assert resolver.identification(second) == resolver.identification(first)
    assert len(calls) == 1
    assert first.fingerprint != second.fingerprint


@pytest.mark.asyncio
async def test_sub2api_requires_structural_public_settings_evidence():
    calls = []
    signature = {
        "registration_enabled": True,
        "email_verify_enabled": False,
        "linuxdo_oauth_enabled": False,
        "purchase_subscription_enabled": True,
        "backend_mode_enabled": False,
        "api_base_url": "",
        "version": "v.test",
    }

    def handler(request):
        calls.append(str(request.url))
        if request.url.path.endswith("/api/status"):
            return httpx.Response(404)
        return httpx.Response(200, json={"code": 0, "data": signature})

    resolver = ProviderResolver(
        context_for(loaded_provider()),
        Settings(),
        b"secret",
        transport=httpx.MockTransport(handler),
    )
    conn = await resolver.resolve(event(), "search")
    assert conn.service_type == "sub2api"
    assert conn.protocol == "alpha"
    assert calls[-1] == "https://gateway.invalid/prefix/api/v1/settings/public"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"site_name": "Sub2API", "version": "v.test"}),
        httpx.Response(401, json={"code": 0, "data": {"site_name": "New API"}}),
        httpx.Response(302, headers={"location": "https://redirect.invalid"}),
        httpx.Response(200, content=b"x" * 65537),
    ],
)
async def test_branding_errors_and_redirects_remain_unknown(response):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    resolver = ProviderResolver(
        context_for(loaded_provider(api_base="https://api.openai.com.attacker.invalid/v1")),
        Settings(),
        b"secret",
        transport=httpx.MockTransport(handler),
    )
    conn = await resolver.resolve(event(), "search")
    assert conn.service_type == "unknown"
    assert conn.protocol == "responses"
    assert len(requests) == 2
    assert all(request.url.host == "api.openai.com.attacker.invalid" for request in requests)


@pytest.mark.asyncio
async def test_identification_network_failure_does_not_fail_resolve():
    def handler(request):
        raise httpx.ConnectError("network unavailable", request=request)

    resolver = ProviderResolver(
        context_for(loaded_provider()), Settings(), b"secret", transport=httpx.MockTransport(handler)
    )
    conn = await resolver.resolve(event(), "search")
    assert conn.service_type == "unknown"


@pytest.mark.asyncio
async def test_fingerprint_stable_and_sensitive_to_effective_configuration():
    provider = loaded_provider()
    context = context_for(provider)
    one = await ProviderResolver(context, settings(), b"secret").resolve(event(), "search")
    same = await ProviderResolver(context, settings(), b"secret").resolve(event(), "search")
    changed_model = await ProviderResolver(context, settings(model="other-model"), b"secret").resolve(
        event(), "search"
    )
    changed_installation = await ProviderResolver(context, settings(), b"other-installation").resolve(
        event(), "search"
    )
    changed_protocol = await ProviderResolver(context, settings(protocol="alpha"), b"secret").resolve(
        event(), "search"
    )
    assert one.fingerprint == same.fingerprint
    assert (
        len(
            {
                one.fingerprint,
                changed_model.fingerprint,
                changed_installation.fingerprint,
                changed_protocol.fingerprint,
            }
        )
        == 4
    )
    assert len(one.fingerprint) == 64


@pytest.mark.asyncio
async def test_feature_timeout_and_proxy_override_source():
    provider = loaded_provider(timeout=50, proxy="http://source.invalid:8080")
    conn = await ProviderResolver(
        context_for(provider), settings(timeout=32, proxy="http://override.invalid:8080"), b"secret"
    ).resolve(event(), "search")
    assert conn.timeout == 32
    assert conn.proxy == "http://override.invalid:8080"


@pytest.mark.asyncio
async def test_alpha_preserves_configured_base_for_transport_endpoint_joining():
    conn = await ProviderResolver(
        context_for(loaded_provider()), settings(protocol="alpha"), b"secret"
    ).resolve(event(), "search")
    assert conn.base_url == "https://gateway.invalid/prefix/v1"


@pytest.mark.asyncio
async def test_context_lookup_failure_is_typed_without_raw_config():
    context = context_for(loaded_provider())
    context.get_current_chat_provider_id.side_effect = RuntimeError("credential-secret")
    with pytest.raises(NativeError) as caught:
        await ProviderResolver(context, settings(), b"secret").resolve(event(), "search")
    assert caught.value.category == "configuration"
    assert "credential-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_injected_transport_remains_isolated_even_with_source_proxy():
    def handler(request):
        return httpx.Response(200, headers={"New-Api-Version": "v.test"})

    conn = await ProviderResolver(
        context_for(loaded_provider(proxy="http://unreachable.invalid:8080")),
        Settings(),
        b"secret",
        transport=httpx.MockTransport(handler),
    ).resolve(event(), "search")
    assert conn.service_type == "newapi"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_type", "protocol"),
    [
        ("newapi", "alpha"),
        ("sub2api", "alpha"),
        ("unknown", "responses"),
        ("openai", "responses"),
    ],
)
async def test_manual_service_selection_controls_default_protocol_and_has_evidence(service_type, protocol):
    def handler(request):
        pytest.fail("Manual service selection must not probe")

    resolver = ProviderResolver(
        context_for(loaded_provider()),
        settings(service_type=service_type),
        b"secret",
        transport=httpx.MockTransport(handler),
    )
    conn = await resolver.resolve(event(), "search")
    assert conn.protocol == protocol
    assert conn.base_url == "https://gateway.invalid/prefix/v1"
    assert resolver.identification(conn) == {
        "type": service_type,
        "confidence": "manual",
        "evidence": ["manual:service_type"],
    }
    info = resolver.identification(conn)
    info["evidence"].append("modified")
    assert resolver.identification(conn)["evidence"] == ["manual:service_type"]


@pytest.mark.asyncio
async def test_auto_identification_domain_and_unknown_evidence():
    def handler(request):
        return httpx.Response(404)

    resolver = ProviderResolver(
        context_for(loaded_provider(api_base="https://api.openai.com/v1")),
        Settings(),
        b"secret",
        transport=httpx.MockTransport(handler),
    )
    conn = await resolver.resolve(event(), "search")
    assert resolver.identification(conn) == {
        "type": "openai",
        "confidence": "high",
        "evidence": ["domain:api.openai.com"],
    }
    resolver.context = context_for(loaded_provider())
    unknown = await resolver.resolve(event(), "search")
    assert resolver.identification(unknown) == {
        "type": "unknown",
        "confidence": "low",
        "evidence": ["no_matching_public_evidence"],
    }


@pytest.mark.asyncio
async def test_structural_evidence_does_not_store_public_response_values():
    signature = {
        "registration_enabled": True,
        "email_verify_enabled": False,
        "linuxdo_oauth_enabled": False,
        "purchase_subscription_enabled": True,
        "backend_mode_enabled": False,
        "api_base_url": "https://private.invalid/secret",
        "version": "credential-A",
        "site_name": "custom branding",
    }
    resolver = ProviderResolver(
        context_for(loaded_provider()),
        Settings(),
        b"secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"code": 0, "data": signature})
        ),
    )
    conn = await resolver.resolve(event(), "search")
    assert resolver.identification(conn) == {
        "type": "sub2api",
        "confidence": "high",
        "evidence": ["structure:/api/v1/settings/public:PublicSettings"],
    }


@pytest.mark.asyncio
async def test_explicit_protocol_overrides_identified_gateway_default():
    resolver = ProviderResolver(
        context_for(loaded_provider()),
        Settings({"search": {"protocol": "responses"}}),
        b"secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"X-New-Api-Version": "v.test"})
        ),
    )
    conn = await resolver.resolve(event(), "search")
    assert conn.protocol == "responses"
    assert resolver.identification(conn)["evidence"] == ["header:x-new-api-version"]


@pytest.mark.asyncio
async def test_current_missing_umo_configuration_never_uses_runtime_fallback():
    context = context_for(loaded_provider())
    context.get_current_chat_provider_id.return_value = None
    context.get_using_provider = Mock(side_effect=AssertionError("Fallback must not be consulted"))
    with pytest.raises(NativeError, match="No provider"):
        await ProviderResolver(context, settings(), b"secret").resolve(event(), "search")
    context.get_provider_by_id.assert_not_called()
    context.get_using_provider.assert_not_called()


@pytest.mark.asyncio
async def test_fingerprint_normalizes_header_case_body_order_and_trailing_slash():
    context = context_for(loaded_provider())
    first = await ProviderResolver(
        context,
        settings(
            headers={"X-Route": "value"},
            extra_body={"temperature": 0.3, "top_p": 0.8},
        ),
        b"secret",
    ).resolve(event(), "search")
    context.get_provider_by_id.return_value = loaded_provider(api_base="https://gateway.invalid/prefix/v1/")
    second = await ProviderResolver(
        context,
        settings(
            headers={"x-route": "value"},
            extra_body={"top_p": 0.8, "temperature": 0.3},
        ),
        b"secret",
    ).resolve(event(), "search")
    assert first.fingerprint == second.fingerprint
