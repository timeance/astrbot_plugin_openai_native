"""Page contracts with mocked transports and optional real host source bindings."""

import ast
import asyncio
import contextvars
import inspect
import json
import os
import re
import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import MethodType, ModuleType, SimpleNamespace
from typing import Generic, TypeVar, overload
from unittest.mock import AsyncMock

import pytest

import native.page as page
from native.capabilities import CapabilityRegistry
from native.config import DEFAULTS, Settings
from native.models import Connection, NativeError
from native.page import PageAPI, PageError

ROOT = Path(__file__).resolve().parents[1]
HOST = Path(
    os.environ.get(
        "OPENAI_NATIVE_ASTRBOT_SOURCE",
        str(ROOT.parent / ".framework-validation" / "AstrBot-61335414433db721b3f7ecc93f90deafe0a59d78"),
    )
)


class Config(dict):
    def __init__(self, values=None):
        super().__init__(deepcopy(values or {}))
        self.saved = []
        self.fail = False

    def save_config(self, candidate):
        self.saved.append(deepcopy(candidate))
        self.update(candidate)
        if self.fail:
            raise OSError("credential-that-must-not-escape")


@pytest.fixture
def setup_page():
    config = Config()
    active = Settings()
    context = SimpleNamespace(get_all_providers=lambda: [])
    service = SimpleNamespace(
        diagnostics_snapshot=AsyncMock(return_value={"features": {}, "recent": [], "notice": "cached"}),
        capabilities=SimpleNamespace(clear=AsyncMock()),
    )
    return PageAPI(context, config, active, service), config, active, service


def payload(api, patch=None, **kwargs):
    return {"revision": api.snapshot()["revision"], "patch": patch or {}, **kwargs}


def test_snapshot_defaults_and_detail_fields(setup_page):
    api, config, _, _ = setup_page
    result = api.snapshot()
    assert set(result) == {
        "values",
        "active_values",
        "revision",
        "needs_reload",
        "changed_paths",
        "secrets",
        "fields",
    }
    assert result["values"]["limits"] == DEFAULTS["limits"]
    assert result["values"]["search"]["mode"] == "current"
    assert result["values"] == result["active_values"]
    assert result["needs_reload"] is False
    assert result["changed_paths"] == []
    assert result["secrets"]["search.keys"] == {"configured": False, "count": 0}
    assert result["fields"]["search.mode"]["options"] == ["current", "provider", "manual"]
    assert "invisible" not in result["fields"]["search.base_url"]
    assert config == {} and not config.saved


@pytest.mark.parametrize(
    "url",
    [
        "https://name:TOPSECRET@example.com/v1",
        "https://example.com/v1?token=TOPSECRET",
        "https://example.com/v1#TOPSECRET",
        "env:TOPSECRET",
        "https://name:TOPSECRET@[invalid",
    ],
)
def test_snapshot_omits_entire_credential_urls(url, setup_page):
    api, config, _, _ = setup_page
    config.update(
        {
            "search": {
                "keys": ["TOPSECRET"],
                "headers": {"Authorization": "TOPSECRET"},
                "base_url": url,
                "proxy": url,
            }
        }
    )
    result = api.snapshot()
    for key in ("keys", "headers", "base_url", "proxy"):
        assert key not in result["values"]["search"]
        assert result["secrets"][f"search.{key}"] == {"configured": True, "count": 1}
    assert "TOPSECRET" not in json.dumps(result)


def test_active_settings_and_legacy_unknowns_are_also_redacted():
    raw = {
        "search": {
            "keys": "TOPSECRET",
            "headers": '{"Authorization":"TOPSECRET"}',
            "base_url": "https://u:TOPSECRET@host/v1",
            "old_secret": "TOPSECRET",
        },
        "unknown": {"api_key": "TOPSECRET"},
    }
    result = PageAPI(SimpleNamespace(), Config(raw), Settings(raw), SimpleNamespace()).snapshot()
    assert "TOPSECRET" not in json.dumps(result)
    assert result["secrets"]["search.keys"]["count"] == 1
    assert not result["needs_reload"]


async def test_partial_save_retains_old_values_without_inserting_defaults():
    raw = {
        "search": {"keys": ["env:KEY"], "old_field": {"preserve": 1}},
        "unknown": {"keep": True},
        "image": {"formats": ["png", "gif"]},
    }
    config, active = Config(raw), Settings(raw)
    api = PageAPI(SimpleNamespace(), config, active, SimpleNamespace())
    result = await api.apply_patch(payload(api, {"search": {"model": "new-model"}}))
    expected = deepcopy(raw)
    expected["search"]["model"] = "new-model"
    assert config.saved == [expected]
    assert dict(config) == expected
    assert active.feature("search")["model"] == ""
    assert result["active_values"]["search"]["model"] == ""
    assert result["values"]["search"]["model"] == "new-model"
    assert result["changed_paths"] == ["search.model"]
    assert result["needs_reload"] is True


async def test_noop_does_not_save_or_fill_absent_defaults(setup_page):
    api, config, _, _ = setup_page
    revision = api.snapshot()["revision"]
    result = await api.apply_patch(payload(api, {"search": {"enabled": True, "mode": "current"}}))
    assert config == {} and not config.saved
    assert result["revision"] == revision


async def test_nested_mapping_patch_preserves_siblings(setup_page):
    api, config, _, _ = setup_page
    config["search"] = {"extra_body": {"reasoning": {"effort": "high", "summary": "auto"}}}
    result = await api.apply_patch(payload(api, {"search": {"extra_body": {"reasoning": {"effort": "low"}}}}))
    assert config["search"]["extra_body"] == {"reasoning": {"effort": "low", "summary": "auto"}}
    assert result["values"]["search"]["extra_body"] == config["search"]["extra_body"]


async def test_secret_replacement_absence_preserves_and_empty_clears(setup_page):
    api, config, _, _ = setup_page
    config["search"] = {"keys": ["old"], "headers": {"X-Token": "old"}}
    await api.apply_patch(payload(api, {"search": {"model": "draft"}}))
    assert config["search"]["keys"] == ["old"]
    result = await api.apply_patch(
        payload(
            api,
            secret_updates={
                "search.keys": ["env:KEY", "literal"],
                "search.headers": {"X-Token": "env:TOKEN"},
                "search.proxy": "http://user:TOPSECRET@proxy.example:8080",
            },
        )
    )
    assert result["secrets"]["search.keys"]["count"] == 2
    assert "TOPSECRET" not in json.dumps(result)
    assert config["search"]["keys"] == ["env:KEY", "literal"]
    result = await api.apply_patch(
        payload(
            api,
            secret_updates={
                "search.keys": [],
                "search.headers": {},
                "search.proxy": "",
            },
        )
    )
    assert result["secrets"]["search.keys"] == {"configured": False, "count": 0}
    assert config["search"]["headers"] == {} and config["search"]["proxy"] == ""


async def test_clean_urls_patch_and_secret_urls_replacement(setup_page):
    api, config, _, _ = setup_page
    result = await api.apply_patch(payload(api, {"search": {"base_url": "https://example.com/v1"}}))
    assert result["values"]["search"]["base_url"] == "https://example.com/v1"
    config["search"]["base_url"] = "https://user:secret@example.com/v1"
    with pytest.raises(PageError) as error:
        await api.apply_patch(payload(api, {"search": {"base_url": "https://new.example/v1"}}))
    assert error.value.code == "secret_patch"
    result = await api.apply_patch(payload(api, secret_updates={"search.base_url": "https://new.example/v1"}))
    assert result["values"]["search"]["base_url"] == "https://new.example/v1"


async def test_sensitive_custom_objects_are_hidden_and_replaced(setup_page):
    api, config, _, _ = setup_page
    config["search"] = {"extra_body": {"metadata": {"api_key": "TOPSECRET"}}}
    result = api.snapshot()
    assert "extra_body" not in result["values"]["search"]
    assert "TOPSECRET" not in json.dumps(result)
    await api.apply_patch(payload(api, secret_updates={"search.extra_body": {}}))
    assert config["search"]["extra_body"] == {}


@pytest.mark.parametrize(
    "patch",
    [
        {"unknown": {}},
        {"search": {"unknown": 1}},
        {"search": []},
        {"search": {"keys": []}},
        {"image": {"headers": {}}},
        {"search": {"proxy": "http://user:secret@proxy"}},
        {"search": {"extra_body": {"api_key": "secret"}}},
    ],
)
async def test_unknown_and_secret_patch_rejected_without_save(patch, setup_page):
    api, config, _, _ = setup_page
    with pytest.raises(PageError):
        await api.apply_patch(payload(api, patch))
    assert config == {} and not config.saved


@pytest.mark.parametrize(
    "patch",
    [
        {"search": {"enabled": 1}},
        {"image": {"model": 12}},
        {"limits": {"admin_exempt": "false"}},
        {"limits": {"user_image": -2}},
        {"limits": {"search_concurrency": 0}},
        {"search": {"max_queries": 5}},
        {"search": {"max_results": 3}},
        {"search": {"timeout": False}},
        {"search": {"timeout": -1}},
        {"network": {"connect_timeout": float("inf")}},
        {"image": {"formats": ["bmp"]}},
        {"image": {"formats": [1]}},
        {"search": {"mode": "other"}},
        {"image": {"protocol": "alpha"}},
        {"search": {"service_type": "other"}},
        {"image": {"default_format": "bmp"}},
        {"gif": {"frame_count": 1}},
        {"gif": {"frame_duration_ms": 2001}},
        {"storage": {"task_days": 1}},
        {"limits": {"timezone": "Invalid/Timezone"}},
        {"search": {"base_url": "file:///private"}},
        {"search": {"paths": {"search": "../bad"}}},
        {"search": {"extra_body": "{}"}},
        {"search": {"paths": {"search": 1}}},
    ],
)
async def test_type_and_range_validation(patch, setup_page):
    api, config, _, _ = setup_page
    with pytest.raises(PageError) as error:
        await api.apply_patch(payload(api, patch))
    assert error.value.status == 400
    assert config == {} and not config.saved


@pytest.mark.parametrize(
    "updates",
    [
        {"search.keys": "env:KEY"},
        {"search.keys": [1]},
        {"search.headers": []},
        {"search.headers": {"X-Test": "bad\nvalue"}},
        {"search.headers": {"X-Test": 12}},
        {"limits.user_image": 1},
        {"search.proxy": {}},
    ],
)
async def test_invalid_secret_updates(updates, setup_page):
    api, config, _, _ = setup_page
    with pytest.raises(PageError):
        await api.apply_patch(payload(api, secret_updates=updates))
    assert config == {} and not config.saved


async def test_incomplete_provider_model_and_env_can_be_saved(setup_page):
    api, config, _, service = setup_page
    await api.apply_patch(payload(api, {"image": {"mode": "provider", "provider_id": "", "model": ""}}))
    await api.apply_patch(
        payload(
            api,
            {"image": {"mode": "manual"}},
            secret_updates={
                "image.keys": ["env:DOES_NOT_NEED_TO_EXIST"],
                "image.base_url": "env:UNSET_URL",
            },
        )
    )
    assert config["image"]["mode"] == "manual"
    service.diagnostics_snapshot.assert_not_awaited()
    service.capabilities.clear.assert_not_awaited()


async def test_legacy_object_strings_and_single_key_survive_unrelated_save(setup_page):
    api, config, _, _ = setup_page
    config["search"] = {
        "headers": '{"X-Custom":"old"}',
        "keys": "$KEY",
        "paths": '{"responses":"/v1/responses"}',
        "extra_body": '{"temperature":0.2}',
    }
    before = deepcopy(config["search"])
    await api.apply_patch(payload(api, {"search": {"model": "new"}}))
    assert {key: config["search"][key] for key in before} == before


async def test_stale_and_external_revisions_conflict(setup_page):
    api, config, _, _ = setup_page
    request = payload(api, {"search": {"model": "new"}})
    config["external"] = {"do_not_erase": True}
    with pytest.raises(PageError) as error:
        await api.apply_patch(request)
    assert error.value.status == 409
    assert config == {"external": {"do_not_erase": True}} and not config.saved
    request["revision"] = "中文"
    with pytest.raises(PageError) as error:
        await api.apply_patch(request)
    assert error.value.status == 409


async def test_concurrent_saves_allow_one_revision_winner(setup_page):
    api, config, _, _ = setup_page
    first, second = (
        payload(api, {"search": {"model": "first"}}),
        payload(api, {"search": {"model": "second"}}),
    )
    results = await asyncio.gather(api.apply_patch(first), api.apply_patch(second), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert next(result for result in results if isinstance(result, PageError)).status == 409
    assert len(config.saved) == 1


async def test_failed_save_restores_memory_and_revision(setup_page):
    api, config, active, _ = setup_page
    config["search"] = {"keys": ["old"], "headers": {"X-Old": "keep"}}
    before = deepcopy(dict(config))
    request = payload(api, {"search": {"model": "candidate"}}, secret_updates={"search.keys": []})
    config.fail = True
    with pytest.raises(PageError) as error:
        await api.apply_patch(request)
    assert error.value.status == 500
    assert "credential-that-must-not-escape" not in str(error.value)
    assert dict(config) == before
    assert api.snapshot()["revision"] == request["revision"]
    assert active.data == DEFAULTS


async def test_providers_and_diagnostics_do_not_resolve_or_probe(setup_page):
    api, _, _, service = setup_page
    provider = SimpleNamespace(
        meta=lambda: SimpleNamespace(id="provider-A"),
        get_model=lambda: "chat-model",
        provider_config={"api_base": "TOPSECRET", "key": "TOPSECRET"},
        get_keys=lambda: pytest.fail("must not read provider credentials"),
    )
    api.context.get_all_providers = lambda: [provider]
    assert await api.providers() == {
        "providers": [{"id": "provider-A", "label": "provider-A", "model": "chat-model"}]
    }
    assert await api.diagnostics() == service.diagnostics_snapshot.return_value
    service.diagnostics_snapshot.assert_awaited_once_with()
    assert await api.reset_capabilities({}) == {"reset": True}
    service.capabilities.clear.assert_awaited_once_with()
    with pytest.raises(PageError):
        await api.reset_capabilities({"is_admin": True})


def response(data, *, status_code=200, headers=None):
    return SimpleNamespace(data=data, status_code=status_code, headers=headers or {})


async def test_identity_errors_are_controlled_401(monkeypatch, setup_page):
    api, _, _, _ = setup_page
    monkeypatch.setattr(page, "_http_response", response)

    def missing_identity():
        raise PageError("unauthorized", "需要登录", 401)

    monkeypatch.setattr(page, "_request_context", missing_identity)
    result = await api._protected("save")()
    assert result.status_code == 401 and result.data["code"] == "unauthorized"


def host_definitions(relative, names, namespace=None):
    """Execute owning host definitions in memory, without vendoring AGPL source."""
    path = HOST / relative
    if not path.is_file():
        pytest.skip("optional AstrBot 4.29.4 validation checkout is unavailable")
    tree = ast.parse(path.read_text("utf-8"))
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in names:
                selected.append(node)
            if node.name == "Context" and "register_web_api" in names:
                selected.extend(
                    child
                    for child in node.body
                    if isinstance(child, ast.FunctionDef) and child.name == "register_web_api"
                )
    assert len(selected) == len(names)
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected],
        type_ignores=[],
    )
    namespace = {} if namespace is None else namespace
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace


@pytest.fixture
def actual_host(monkeypatch, setup_page):
    api, _, _, _ = setup_page
    ctx = SimpleNamespace(registered_web_apis=[], get_all_providers=lambda: [])
    method = host_definitions("astrbot/core/star/context.py", {"register_web_api"})["register_web_api"]
    ctx.register_web_api = MethodType(method, ctx)
    api.context = ctx
    assert api.register() is None
    api.register()  # The real registration contract must replace, never duplicate.
    assert len(ctx.registered_web_apis) == 10
    namespace = {
        "contextvars": contextvars,
        "contextmanager": contextmanager,
        "Generic": Generic,
        "ValueT": TypeVar("ValueT"),
        "overload": overload,
        "_request_var": contextvars.ContextVar("test_actual_plugin_request"),
    }
    namespace = host_definitions(
        "astrbot/api/web.py",
        {
            "PluginMultiDict",
            "PluginRequest",
            "PluginRequestProxy",
            "bind_request_context",
        },
        namespace,
    )
    web = ModuleType("astrbot.api.web")
    web.PluginRequest = namespace["PluginRequest"]
    web.request = namespace["PluginRequestProxy"]()
    web.json_response = response
    monkeypatch.setitem(sys.modules, "astrbot", ModuleType("astrbot"))
    monkeypatch.setitem(sys.modules, "astrbot.api", ModuleType("astrbot.api"))
    monkeypatch.setitem(sys.modules, "astrbot.api.web", web)

    async def run_maybe_async(call):
        result = call()
        return await result if inspect.isawaitable(result) else result

    dispatcher = host_definitions(
        "astrbot/dashboard/api/plugins.py",
        {
            "_normalize_plugin_api_route",
            "_plugin_api_route_pattern",
            "_match_registered_web_api",
            "_call_plugin_extension",
        },
        {
            "re": re,
            "PluginRequest": web.PluginRequest,
            "bind_request_context": namespace["bind_request_context"],
            "run_maybe_async": run_maybe_async,
        },
    )["_call_plugin_extension"]

    async def call(path, *, method="GET", username="admin", body=None):
        request = SimpleNamespace(
            method=method,
            url=SimpleNamespace(path="/api/v1/plugins/extensions/" + path),
            headers={},
            cookies={},
            client=None,
            query_params=SimpleNamespace(multi_items=lambda: []),
            json=AsyncMock(return_value=body),
            app=SimpleNamespace(state=SimpleNamespace(core_lifecycle=SimpleNamespace(star_context=ctx))),
        )
        result = await dispatcher(path, request, username)
        return result, request

    return api, ctx, call, web


async def test_actual_host_registration_dispatches_get_post_and_bridge_alias(actual_host):
    api, ctx, call, _ = actual_host
    for prefix in ("openai-native", "astrbot_plugin_openai_native/openai-native"):
        result, _ = await call(prefix + "/settings")
        assert result.status_code == 200
        result, _ = await call(
            prefix + "/settings",
            method="POST",
            body={
                "revision": result.data["revision"],
                "patch": {"search": {"model": prefix}},
            },
        )
        assert result.status_code == 200 and result.data["values"]["search"]["model"] == prefix
    assert not inspect.iscoroutinefunction(api.register)
    assert len(ctx.registered_web_apis) == 10


@pytest.mark.parametrize(
    "endpoint,method",
    [
        ("settings", "GET"),
        ("settings", "POST"),
        ("providers", "GET"),
        ("diagnostics", "GET"),
        ("capabilities/reset", "POST"),
    ],
)
async def test_actual_bound_request_rejects_anonymous_even_with_admin_body(endpoint, method, actual_host):
    _, _, call, _ = actual_host
    result, request = await call(
        "astrbot_plugin_openai_native/openai-native/" + endpoint,
        method=method,
        username="",
        body={"is_admin": True, "username": "admin"},
    )
    assert result.status_code == 401
    request.json.assert_not_awaited()


async def test_actual_host_identity_ignores_client_admin_false_and_rejects_spoof(actual_host):
    api, _, call, _ = actual_host
    result, _ = await call(
        "openai-native/settings",
        method="POST",
        body={
            **payload(api),
            "is_admin": False,
        },
    )
    assert result.status_code == 400 and result.data["code"] == "validation"
    result, _ = await call("openai-native/settings")
    assert result.status_code == 200 and result.headers["Cache-Control"] == "no-store"
    result, _ = await call("openai-native/capabilities/reset", method="POST", body={})
    assert result.status_code == 200 and result.data == {"reset": True}


async def test_unbound_or_fake_plugin_request_cannot_authenticate(monkeypatch, actual_host):
    api, _, _, web = actual_host
    fake = SimpleNamespace(username="admin", json=AsyncMock(return_value={"is_admin": True}))
    web.request = SimpleNamespace(_get_current=lambda: fake)
    result = await api._protected("save")()
    assert result.status_code == 401
    fake.json.assert_not_awaited()


async def test_quart_host_identity_fallback_is_lazy(monkeypatch, setup_page):
    api, _, _, _ = setup_page
    monkeypatch.setitem(sys.modules, "astrbot.api.web", None)
    quart = ModuleType("quart")
    quart.g = SimpleNamespace(username="admin")
    quart.request = SimpleNamespace(get_json=AsyncMock(return_value=payload(api)))
    quart.jsonify = lambda data: response(data)
    monkeypatch.setitem(sys.modules, "quart", quart)
    result = await api._protected("save")()
    assert result.status_code == 200
    quart.g.username = None
    result = await api._protected("save")()
    assert result.status_code == 401


async def test_capability_clear_persists_and_keeps_other_registry_semantics(tmp_path):
    path = tmp_path / "capabilities.json"
    registry = CapabilityRegistry(path)
    connection = Connection("p", "https://example.com/v1", "secret", fingerprint="fingerprint")
    await registry.record(connection, "search", "auth_blocked", "auth")
    await registry.record(connection, "generate:gif", "unsupported", "format")
    await registry.clear()
    assert json.loads(path.read_text("utf-8"))["records"] == {}
    assert (await registry.get(connection, "search"))["status"] == "unverified"
    restored = CapabilityRegistry(path)
    assert (await restored.get(connection, "generate:gif"))["status"] == "unverified"


async def test_capability_clear_failure_retains_previous_observations(tmp_path, monkeypatch):
    registry = CapabilityRegistry(tmp_path / "capabilities.json")
    connection = Connection("p", "https://example.com/v1", "secret", fingerprint="fingerprint")
    await registry.record(connection, "search", "auth_blocked", "auth")

    def fail(records):
        raise OSError("secret must not escape")

    monkeypatch.setattr(registry, "_write", fail)
    with pytest.raises(NativeError) as error:
        await registry.clear()
    assert "secret must not escape" not in str(error.value)
    assert (await registry.get(connection, "search"))["status"] == "auth_blocked"


@pytest.mark.parametrize(
    "patch",
    [
        {"image": {"default_size": ""}},
        {"image": {"default_size": "1024X1024"}},
        {"image": {"default_size": "-1x1024"}},
        {"image": {"default_size": "0x1024"}},
        {"image": {"default_size": "1024x0"}},
        {"image": {"default_size": "1024x1.5"}},
        {"image": {"default_size": " 1024x1024"}},
        {"image": {"default_size": "1024x1024\n"}},
        {"image": {"default_size": "9000x9000"}},
        {"image": {"sizes": ["auto", "invalid"]}},
        {"image": {"sizes": ["0x1"]}},
        {"image": {"max_pixels": 1024, "default_size": "33x32"}},
    ],
)
async def test_size_syntax_and_default_pixel_limit(patch, setup_page):
    api, config, _, _ = setup_page
    with pytest.raises(PageError) as error:
        await api.apply_patch(payload(api, patch))
    assert error.value.status == 400
    assert config == {} and not config.saved


async def test_valid_sizes_and_exact_pixel_limit_can_be_saved(setup_page):
    api, config, _, _ = setup_page
    await api.apply_patch(
        payload(
            api,
            {
                "image": {
                    "max_pixels": 1024,
                    "default_size": "32x32",
                    "sizes": ["auto", "32x32", "64x64"],
                }
            },
        )
    )
    assert config["image"]["default_size"] == "32x32"
    await api.apply_patch(payload(api, {"image": {"default_size": "auto"}}))
    assert config["image"]["default_size"] == "auto"


@pytest.mark.parametrize(
    "url",
    [
        "https://bad host.example/v1",
        "https://example.com/with space",
        "https://example.com/\npath",
        "https://example.com/\x7fpath",
        "https://example.com/\\path",
        "https://-bad.example/v1",
        "https://bad-.example/v1",
        "https://example..com/v1",
        "https://example.com:0/v1",
        "https://example.com:65536/v1",
        "https://example.com:/v1",
        "https://256.1.2.3/v1",
        "https://[invalid/v1",
        "https://example.com/%invalid",
        "https://example.com/<path>",
    ],
)
@pytest.mark.parametrize("field", ["base_url", "proxy"])
async def test_url_syntax_and_characters_rejected(field, url, setup_page):
    api, config, _, _ = setup_page
    with pytest.raises(PageError) as error:
        await api.apply_patch(payload(api, secret_updates={f"search.{field}": url}))
    assert error.value.status == 400
    assert config == {} and not config.saved


@pytest.mark.parametrize(
    "field,url",
    [
        ("base_url", "https://example.com:443/v1/%E4%B8%AD"),
        ("base_url", "https://例子.测试/v1"),
        ("base_url", "https://[::1]:443/v1"),
        ("proxy", "socks5h://user:pass%40word@proxy.example:1080"),
        ("proxy", "http://localhost:8080"),
        ("proxy", "http://127.0.0.1:8080"),
    ],
)
async def test_valid_endpoint_and_proxy_syntax(field, url, setup_page):
    api, config, _, _ = setup_page
    await api.apply_patch(payload(api, secret_updates={f"search.{field}": url}))
    assert config["search"][field] == url


async def test_bridge_conflict_message_survives_host_string_only_errors(actual_host):
    api, _, call, _ = actual_host
    first = payload(api, {"search": {"model": "first"}})
    result, _ = await call("astrbot_plugin_openai_native/openai-native/settings", method="POST", body=first)
    assert result.status_code == 200
    result, _ = await call("astrbot_plugin_openai_native/openai-native/settings", method="POST", body=first)
    assert result.status_code == 409
    assert result.data["code"] == "conflict"
    assert "版本冲突" in result.data["message"]
