"""Bridge contracts plus opt-in real AstrBot import and registration checks."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from native.config import Settings
from native.diagnostics import Diagnostics
from native.identity import actor_from_event
from native.models import Artifact, NativeError, SearchHit, SearchResult

ROOT = Path(__file__).resolve().parents[1]


class Event:
    role = "member"
    unified_msg_origin = "adapter-a:GroupMessage:group-a"
    message_obj = SimpleNamespace(message_id="m1")

    def __init__(self, sender="u1", group="group-a", text="/onative status"):
        self.sender, self.group, self.text = sender, group, text
        self.extra = {}
        self.send = AsyncMock()

    def stop_event(self):
        self.stopped = True

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
                    "size": "string",
                    "aspect_ratio": "string",
                    "resolution": "string",
                    "quality": "string",
                    "complexity": "string",
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
    properties = gif.tools[0].parameters["properties"]
    assert properties["frame_count"]["maximum"] == 16
    assert properties["frame_count"]["type"] == ["integer", "null"]
    assert properties["frame_duration_ms"]["type"] == ["integer", "null"]
    assert properties["resolution"]["enum"] == ["1K", "1.5K", "2K", "4K", None]
    assert properties["quality"]["enum"] == ["auto", "low", "medium", "high", None]
    for name in ("size", "aspect_ratio", "resolution", "quality", "complexity"):
        assert properties[name]["type"] == ["string", "null"]
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
        search=AsyncMock(return_value=SearchResult("op", ["current price"], [])),
        submit_image=AsyncMock(return_value={"task_id": "id", "status": "queued"}),
        probe=AsyncMock(return_value={"status": "tested"}),
        available_tools=AsyncMock(return_value={"image": False, "search": False}),
        remember_images=AsyncMock(),
        diagnostics=Diagnostics(),
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


@pytest.mark.parametrize(
    "options",
    [
        {
            "size": None,
            "aspect_ratio": None,
            "resolution": None,
            "quality": None,
            "complexity": None,
            "frame_count": None,
            "frame_duration_ms": None,
        },
        {
            "size": "auto",
            "aspect_ratio": "1:1",
            "resolution": "auto",
            "quality": "auto",
            "complexity": "auto",
            "frame_count": 2,
            "frame_duration_ms": 500,
        },
        {
            "size": "",
            "aspect_ratio": "",
            "resolution": "",
            "quality": "",
            "complexity": "",
            "frame_count": 2,
            "frame_duration_ms": 500,
        },
    ],
)
async def test_static_tool_normalizes_inapplicable_and_default_fields(plugin, options):
    result = json.loads(
        await plugin.image(Event(), "pelican riding a bicycle", output_format="png", **options)
    )
    assert result["task_id"] == "id"
    request = plugin.service.submit_image.call_args.args[1]
    assert request.frame_count is None and request.frame_duration_ms is None
    assert request.resolution == "" and request.quality == "auto"
    assert request.size == (options["size"] or "")
    assert request.aspect_ratio == (options["aspect_ratio"] or "")
    assert request.complexity == (options["complexity"] or "")


async def test_gif_tool_keeps_frames_and_default_format(plugin):
    plugin.settings = Settings({"image": {"default_format": "gif"}, "gif": {"enabled": True}})
    await plugin.image(Event(), "blinking", frame_count=5, frame_duration_ms=120, resolution=None)
    request = plugin.service.submit_image.call_args.args[1]
    assert request.frame_count == 5 and request.frame_duration_ms == 120
    assert request.resolution == ""


async def test_image_argument_error_does_not_claim_service_failure(plugin):
    plugin.service.submit_image.side_effect = NativeError(
        "invalid_request", "Choose size or aspect ratio/resolution, not both"
    )
    result = json.loads(await plugin.image(Event(), "pelican", size="1024x1024", aspect_ratio="1:1"))
    assert result["error"]["category"] == "invalid_request"
    assert result["request_submitted"] is False
    assert "参数" in result["guidance"]
    plugin.service.submit_image.side_effect = NativeError("configuration", "Missing image model")
    result = json.loads(await plugin.image(Event(), "pelican"))
    assert result["error"]["category"] == "configuration"
    assert "request_submitted" not in result


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
    event = Event(text="帮我搜索 current price")
    await plugin.web_search(event, ["current price"])
    request = plugin.service.search.call_args.args[1]
    assert request.queries == ["current price"]
    assert request.max_results == 3
    assert request.recency is None


async def test_llm_search_clamps_request_and_compacts_result(plugin):
    plugin.settings = Settings({"search": {"response_chars": 350, "snippet_chars": 30}})
    plugin.service.search.return_value = SearchResult(
        "op", ["one"],
        [SearchHit(f"Source {index}", f"https://source.example/{index}",
                   "Evidence 【turn0search0】 https://hidden.example/a " + "x" * 200,
                   f"turn0search{index}") for index in range(4)],
        backend_synthesis="raw https://hidden.example/full 【turn0search9】",
    )
    result_text = await plugin.web_search(Event(text="搜索 one"), ["one", "two"], max_results=9)
    result = json.loads(result_text)
    request = plugin.service.search.call_args.args[1]
    assert request.queries == ["one"] and request.max_results == 3
    assert len(result["sources"]) == 3 and len(result_text) <= 350
    assert "https://" not in result_text and "turn0search" not in result_text
    assert "backend_synthesis" not in result and "backend_excerpt" not in result


async def test_raw_excerpt_includes_only_selected_alpha_source(plugin):
    plugin.settings = Settings({"search": {"include_raw": True, "max_results": 1,
                                           "default_results": 1, "response_chars": 450}})
    plugin.service.search.return_value = SearchResult(
        "op", ["query"], [
            SearchHit("First", "https://source.example/a"),
            SearchHit("Second", "https://source.example/b"),
        ],
        backend_synthesis=(
            "[native Alpha tool output; untrusted]\n"
            "First (https://source.example/a)\n【turn0search0】 First evidence\n"
            "Second (https://source.example/b)\n【turn0search1】 Second evidence"
        ),
    )
    response = json.loads(await plugin.web_search(Event(text="搜索 query"), ["query"]))
    assert len(response["sources"]) == 1
    assert "First evidence" in response["backend_excerpt"]
    assert "Second evidence" not in response["backend_excerpt"]
    assert "https://" not in json.dumps(response)


async def test_explicit_policy_only_gates_llm_tool(plugin):
    plugin.settings = Settings({"search": {"trigger_policy": "explicit"}})
    event = Event(text="解释这个概念")
    refusal = json.loads(await plugin.web_search(event, ["concept"]))
    assert refusal["error"]["category"] == "search_not_requested"
    plugin.service.search.assert_not_awaited()
    event.text = "请搜索这个概念"
    assert json.loads(await plugin.web_search(event, ["concept"]))["status"] == "succeeded"
    event.text = "/onative search concept"
    assert json.loads(await plugin.web_search(event, ["concept"]))["status"] == "succeeded"
    assert plugin.service.search.await_count == 2


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
diagnostic_hook = handlers["diagnose_message"]
assert diagnostic_hook.event_type == EventType.AdapterMessageEvent
assert diagnostic_hook.extras_configs["priority"] == 10000
assert any(isinstance(item, EventMessageTypeFilter) and item.event_message_type == EventMessageType.ALL
           for item in diagnostic_hook.event_filters)
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


def test_task_tool_requires_own_session_history(bridge):
    task = Tool("openai_image_task", {"properties": {"operation": {"type": "string"}}})
    original = ToolSet([image_tool(), task])
    assert bridge.filter_tool_set(original, {"image": False, "image_task": False}, Settings()).tools == []
    result = bridge.filter_tool_set(original, {"image": False, "image_task": True}, Settings())
    assert [item.name for item in result.tools] == ["openai_image_task"]
    assert result.tools[0]._wrapped is task._wrapped


async def test_fixed_commands_stop_agent_and_share_submission(plugin):
    for method, text, animated in (
        ("image_command", "/生图 一只柴犬 16:9 2K 2", False),
        ("gif_command", "/生动图 小狗摇尾巴 --帧数 4 --帧时长 100", True),
        ("command", "/onative image 一只柴犬 --数量 2", False),
    ):
        event = Event(text=text)
        plugin.service.submit_image.reset_mock()
        handler = getattr(plugin, method)(event)
        reply = await anext(handler)
        assert not getattr(event, "stopped", False)
        assert [item async for item in handler] == []
        assert event.stopped
        plugin.service.submit_image.assert_awaited_once()
        request = plugin.service.submit_image.call_args.args[1]
        assert (request.output_format == "gif") == animated
        assert plugin.service.submit_image.call_args.kwargs == {"deliver": True}
        assert reply == f"已接受{'动图' if animated else '生图'}任务，id"


async def test_command_acceptance_template_and_task_details(plugin):
    plugin.settings = Settings({"image": {"acceptance_template": "{kind}已排队：{task_id}"}})
    reply = [item async for item in plugin.image_command(Event(text="/生图 一只猫"))][0]
    assert reply == "生图已排队：id"
    plugin.service.public_api.get_task.return_value = {
        "task_id": "id", "kind": "image", "status": "succeeded", "generated": 1,
        "requested": 1, "reference_count": 2, "delivery_status": "unknown",
    }
    detail = await plugin._task_command(Event(), "id")
    assert "参考图：2 张" in detail
    assert "投递：未确认" in detail
    plugin.service.public_api.get_task.return_value["delivery_status"] = "sent"
    assert "投递：已发送" in await plugin._task_command(Event(), "id")


async def test_command_invalid_parameters_do_not_submit(plugin):
    for text in ("/生图", "/生图 狗 --数量 9", "/生图 狗 --格式 gif", "/生图 狗 --尺寸 100x100 --比例 1:1"):
        event = Event(text=text)
        replies = [item async for item in plugin.image_command(event)]
        assert event.stopped
        assert replies and "任务 ID" not in replies[0]
    plugin.service.submit_image.assert_not_called()


async def test_empty_request_and_missing_tools_are_diagnosed(plugin, bridge):
    plugin.service.record_request = lambda *args: calls.append(args)
    calls = []
    event = Event()
    req = SimpleNamespace(func_tool=None)
    await plugin.on_llm_request(event, req)
    assert req.func_tool is None
    assert calls[0][1:3] == ([], [])
    req.func_tool = ToolSet([Tool("other", {})])
    await plugin.on_llm_request(event, req)
    assert calls[1][1:3] == ([], [])


async def test_task_commands_use_caller_and_stop_agent(plugin):
    plugin.service.list_image_tasks = AsyncMock(return_value=[])
    event = Event(text="/生图任务")
    assert "还没有" in [item async for item in plugin.task_command(event)][0]
    assert event.stopped
    plugin.service.list_image_tasks.assert_awaited_once_with(event)
    plugin.service.public_api.cancel_task = AsyncMock(return_value={"accepted": True})
    event = Event(text="/生图取消 task1")
    assert "按实际结果结算" in [item async for item in plugin.cancel_command(event)][0]
    plugin.service.public_api.cancel_task.assert_awaited_once_with(event, "task1")



async def test_opt_in_message_diagnostic_does_not_submit_or_stop_event(plugin):
    event = Event(text="生图 根据三张参考图生成表情")
    event.is_at_or_wake_command = True
    event.set_extra("activated_handlers", [SimpleNamespace(handler_full_name="native.image_command")])
    await plugin.diagnose_message(event)
    assert not plugin.service.diagnostics.snapshot()
    plugin.service.diagnostics.enabled = True
    await plugin.diagnose_message(event)
    record = plugin.service.diagnostics.snapshot()[0]
    assert record["kind"] == "message"
    assert record["command_boundary"] == "space"
    assert record["activated_handlers"] == ["native.image_command"]
    assert not getattr(event, "stopped", False)
    plugin.service.submit_image.assert_not_awaited()


async def test_reference_cache_failure_is_visible_when_logging_enabled(plugin):
    plugin.service.diagnostics.enabled = True
    plugin.service.remember_images.side_effect = NativeError("validation", "Invalid input image")
    event = Event()
    await plugin.remember_images(event)
    record = plugin.service.diagnostics.snapshot()[0]
    assert record["kind"] == "reference_cache" and record["state"] == "failed"
    assert record["error"]["category"] == "validation"
    assert not getattr(event, "stopped", False)
