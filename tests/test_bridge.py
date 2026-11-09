"""Bridge contracts plus opt-in real AstrBot import and registration checks."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from native.config import Settings
from native.identity import actor_from_event
from native.models import Artifact, NativeError

ROOT = Path(__file__).resolve().parents[1]


class Event:
    role = "member"
    unified_msg_origin = "adapter-a:GroupMessage:group-a"
    message_obj = SimpleNamespace(message_id="m1")

    def __init__(self, sender="u1", group="group-a", text="/onative status"):
        self.sender, self.group, self.text = sender, group, text
        self.extra = {}
        self.send = AsyncMock()

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return self.group

    def get_platform_id(self):
        return "adapter-a"

    def get_self_id(self):
        return "bot-a"

    def get_platform_name(self):
        return "aiocqhttp"

    def get_message_str(self):
        return self.text

    def get_extra(self, name):
        return self.extra.get(name)

    def set_extra(self, name, value):
        self.extra[name] = value

    def plain_result(self, value):
        return value

    def image_result(self, path):
        return ("original-file", path)


@dataclass
class Tool:
    name: str
    parameters: dict
    active: bool = True
    _wrapped: object = None


class ToolSet:
    def __init__(self, tools=None):
        self.tools = list(tools or [])


@pytest.fixture
def bridge(monkeypatch):
    """Import just the bridge against explicit framework and service contracts."""

    def module(name, **attrs):
        obj = ModuleType(name)
        obj.__dict__.update(attrs)
        obj.__path__ = []
        monkeypatch.setitem(sys.modules, name, obj)
        return obj

    def decorate(*args, **kwargs):
        def apply(obj):
            return obj

        return apply

    module("astrbot")
    module("astrbot.api", AstrBotConfig=dict, logger=SimpleNamespace(error=lambda message: None))
    filters = SimpleNamespace(
        llm_tool=decorate,
        on_llm_request=decorate,
        command=decorate,
        event_message_type=decorate,
        EventMessageType=SimpleNamespace(ALL="all"),
    )
    module("astrbot.api.event", AstrMessageEvent=Event, filter=filters)
    module("astrbot.api.star", Context=object, Star=object, StarTools=object, register=decorate)
    module("astrbot.core")
    module("astrbot.core.agent")
    module("astrbot.core.agent.tool", ToolSet=ToolSet)
    package = module("_bridge_contract")
    package.__path__ = [str(ROOT)]
    native = module("_bridge_contract.native")
    native.__path__ = [str(ROOT / "native")]
    # Reuse the shared classes rather than loading duplicate dataclasses/errors.
    import native.config
    import native.identity
    import native.models

    for name in ("config", "identity", "models"):
        monkeypatch.setitem(sys.modules, "_bridge_contract.native." + name, sys.modules["native." + name])
    module("_bridge_contract.native.service", NativeService=object)
    spec = importlib.util.spec_from_file_location("_bridge_contract.main", ROOT / "main.py")
    target = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, target)
    spec.loader.exec_module(target)
    return target


def image_tool():
    return Tool(
        "openai_image",
        {
            "type": "object",
            "properties": {
                name: {"type": kind}
                for name, kind in {
                    "prompt": "string",
                    "output_format": "string",
                    "action": "string",
                    "n": "integer",
                    "references": "array",
                    "frame_count": "integer",
                    "frame_duration_ms": "integer",
                }.items()
            },
        },
        _wrapped=object(),
    )


def test_identity_uses_sender_not_umo_and_private_is_separate():
    one, two = actor_from_event(Event("u1")), actor_from_event(Event("u2"))
    assert one.umo == two.umo
    assert one.user_key != two.user_key
    other = Event("u1", "group-b")
    other.unified_msg_origin = "adapter-a:GroupMessage:group-b"
    assert actor_from_event(other).user_key == one.user_key
    assert actor_from_event(other).owner_key != one.owner_key
    private = Event("u1", "")
    private.unified_msg_origin = "adapter-a:FriendMessage:u1"
    assert actor_from_event(private).user_key != one.user_key
    assert one.message_id == "m1"


def test_identity_rejects_missing_sender_and_caller_admin_boolean():
    with pytest.raises(NativeError, match="sender_id"):
        actor_from_event(Event(""))
    event = Event()
    event.is_admin = True
    assert not actor_from_event(event).is_admin
    event.role = "admin"
    assert actor_from_event(event).is_admin


def test_filter_copies_schema_preserves_guards_and_other_tools(bridge):
    target, unrelated = image_tool(), Tool("other", {"type": "object"})
    original = ToolSet([target, unrelated])
    result = bridge.filter_tool_set(
        original, {"image": True, "search": False, "formats": ["png"], "actions": ["generate"]}, Settings()
    )
    changed = result.tools[0]
    assert result is not original
    assert changed is not target
    assert changed._wrapped is target._wrapped
    assert result.tools[1] is unrelated
    assert changed.parameters["properties"]["output_format"]["enum"] == ["png"]
    assert changed.parameters["properties"]["action"]["enum"] == ["generate"]
    assert "frame_count" not in changed.parameters["properties"]
    assert "frame_count" in target.parameters["properties"]
    gif = bridge.filter_tool_set(
        original, {"image": True, "formats": ["gif"], "actions": ["generate", "edit"]}, Settings()
    )
    assert gif.tools[0].parameters["properties"]["frame_count"]["maximum"] == 16
    assert "enum" not in target.parameters["properties"]["output_format"]


def test_filter_never_adds_persona_disabled_tools(bridge):
    original = ToolSet([Tool("other", {})])
    result = bridge.filter_tool_set(
        original, {"image": True, "search": True, "formats": ["png"], "actions": ["generate"]}, Settings()
    )
    assert [tool.name for tool in result.tools] == ["other"]
    blocked = bridge.filter_tool_set(ToolSet([image_tool()]), {"image": False}, Settings())
    assert blocked.tools == []


@pytest.fixture
def plugin(bridge):
    instance = bridge.OpenAINativePlugin.__new__(bridge.OpenAINativePlugin)
    instance.settings = Settings()
    instance.service = SimpleNamespace(
        search=AsyncMock(return_value={"status": "succeeded"}),
        submit_image=AsyncMock(return_value={"task_id": "id", "status": "queued"}),
        probe=AsyncMock(return_value={"status": "tested"}),
        available_tools=AsyncMock(return_value={"image": False, "search": False}),
        remember_images=AsyncMock(),
        public_api=SimpleNamespace(get_task=AsyncMock(return_value={"status": "queued"})),
    )
    return instance


async def test_image_submits_background_and_accepts_asset_ids_only(plugin):
    event = Event()
    result = await plugin.image(event, "a dog", references=["asset_123"], output_format="gif")
    assert '"task_id"' in result
    request = plugin.service.submit_image.call_args.args[1]
    assert request.references == ["asset_123"]
    assert request.output_format == "gif"
    assert request.frame_count is None
    assert plugin.service.submit_image.call_args.kwargs == {"deliver": True}
    plugin.service.submit_image.reset_mock()
    for reference in ("C:/secret.png", "https://example.test/a", "../a", "C:\\secret"):
        result = await plugin.image(event, "a dog", references=[reference])
        assert '"error"' in result
    plugin.service.submit_image.assert_not_called()


async def test_probe_requires_admin_and_explicit_confirmation(plugin):
    event = Event(text="/onative probe gif")
    event.role = "admin"
    replies = [item async for item in plugin.command(event)]
    assert "confirm" in replies[0]
    plugin.service.probe.assert_not_called()
    event.text = "/onative probe gif confirm"
    assert [item async for item in plugin.command(event)]
    plugin.service.probe.assert_awaited_once_with(event, "gif", confirm_paid=True)
    plugin.service.probe.reset_mock()
    event.role = "member"
    replies = [item async for item in plugin.command(event)]
    assert '"permission"' in replies[0]
    plugin.service.probe.assert_not_called()


async def test_delivery_preserves_original_file_and_hook_does_not_resurrect(plugin, bridge):
    event = Event()
    artifact = Artifact("a", "saved/animation.gif", "image/gif", 123, "hash", 16, 16, 9, 200)
    await plugin._deliver(event, [artifact])
    event.send.assert_awaited_once_with(("original-file", artifact.path))
    req = SimpleNamespace(func_tool=ToolSet([Tool("other", {})]))
    await plugin.on_llm_request(event, req)
    assert [tool.name for tool in req.func_tool.tools] == ["other"]
    tools = await plugin.build_tools(event)
    assert tools.tools == []
    req.func_tool = None
    await plugin.on_llm_request(event, req)
    assert event.get_extra(bridge.TOOL_SNAPSHOT).tools == []


async def test_search_calls_service_and_returns_marked_structured_result(plugin):
    event = Event()
    await plugin.web_search(event, ["current price"])
    request = plugin.service.search.call_args.args[1]
    assert request.queries == ["current price"]
    assert request.max_results == 5
    assert request.recency is None


async def test_explicit_zero_frame_is_forwarded_for_service_validation(plugin):
    await plugin.image(Event(), "a dog", output_format="gif", frame_count=0, frame_duration_ms=0)
    request = plugin.service.submit_image.call_args.args[1]
    assert request.frame_count == 0
    assert request.frame_duration_ms == 0


@pytest.mark.skipif(
    os.environ.get("OPENAI_NATIVE_REAL_ASTRBOT") != "1",
    reason="Optional real AstrBot import; default tests use explicit contract doubles",
)
def test_real_astrbot_tool_registration():
    script = """
import importlib
import sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root.parent))
bridge = importlib.import_module(root.name + ".main")
from astrbot.api.star import Star
from astrbot.core.provider.register import llm_tools
from astrbot.core.star.filter.event_message_type import EventMessageType, EventMessageTypeFilter
from astrbot.core.star.star_handler import EventType, star_handlers_registry
assert issubclass(bridge.OpenAINativePlugin, Star)
handlers = {handler.handler_name: handler
            for handler in star_handlers_registry.get_handlers_by_module_name(bridge.__name__)}
cache_hook = handlers["remember_images"]
assert cache_hook.event_type == EventType.AdapterMessageEvent
assert cache_hook.extras_configs["priority"] == 0
assert any(isinstance(item, EventMessageTypeFilter) and item.event_message_type == EventMessageType.ALL
           for item in cache_hook.event_filters)
assert handlers["on_llm_request"].event_type == EventType.OnLLMRequestEvent
for name in bridge.TOOL_NAMES:
    tool = llm_tools.get_func(name)
    assert tool is not None, name
    assert tool.parameters["type"] == "object"
assert llm_tools.get_func("openai_image").parameters["properties"]["references"]["items"] == {"type": "string"}
assert llm_tools.get_func("openai_web_search").parameters["properties"]["queries"]["items"] == {"type": "string"}
"""
    # Core imports initialize configuration/database files. Isolate those side effects
    # even when this test is invoked from the plugin checkout.
    runtime = Path(
        os.environ.get("OPENAI_NATIVE_ASTRBOT_RUNTIME", ROOT.parent / ".framework-validation" / "runtime")
    ).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    temporary = runtime / "tmp"
    temporary.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "ASTRBOT_ROOT": str(runtime),
        "TEMP": str(temporary),
        "TMP": str(temporary),
        "TMPDIR": str(temporary),
        "XDG_CACHE_HOME": str(runtime / "cache"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    }
    completed = subprocess.run(
        [sys.executable, "-c", script, str(ROOT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        cwd=runtime,
        env=env,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


async def test_custom_tool_builder_respects_persona_before_request_hook(plugin):
    event = Event()
    guarded = image_tool()
    plugin.context = SimpleNamespace(
        conversation_manager=SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value=None),
            get_conversation=AsyncMock(),
        ),
        persona_manager=SimpleNamespace(
            resolve_selected_persona=AsyncMock(return_value=("p", {"tools": []}, None, False))
        ),
        get_llm_tool_manager=lambda: SimpleNamespace(get_full_tool_set=lambda: ToolSet([guarded])),
    )
    plugin.service.available_tools.return_value = {"image": True, "formats": ["png"], "actions": ["generate"]}
    assert (await plugin.build_tools(event)).tools == []
    plugin.context.conversation_manager.get_conversation.assert_not_called()


async def test_capability_failure_hides_only_own_tools(plugin):
    event = Event()
    plugin.service.available_tools.side_effect = NativeError("configuration", "not configured")
    unrelated = Tool("other", {})
    original = ToolSet([unrelated, image_tool()])
    req = SimpleNamespace(func_tool=original)
    await plugin.on_llm_request(event, req)
    assert req.func_tool.tools == [unrelated]
    assert len(original.tools) == 2


@pytest.mark.parametrize("outcome", ["cached", "invalid_image", "disabled"])
async def test_current_image_cache_hook_never_replies(plugin, outcome):
    event = Event()
    if outcome == "invalid_image":
        plugin.service.remember_images.side_effect = NativeError("invalid_image", "Invalid image")
    elif outcome == "disabled":
        plugin.settings = Settings({"image": {"enabled": False}})
    assert await plugin.remember_images(event) is None
    if outcome == "disabled":
        plugin.service.remember_images.assert_not_called()
    else:
        plugin.service.remember_images.assert_awaited_once_with(event)
    event.send.assert_not_called()
