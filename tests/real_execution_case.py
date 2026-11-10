"""Subprocess-only cases: real AstrBot executor/guards, simulated HTTP upstream.

This helper deliberately does not match pytest's test collection patterns.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import sys
from copy import copy, deepcopy
from dataclasses import replace
from email.parser import BytesParser
from email.policy import default
from functools import partial
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jsonschema
from astrbot.core import db_helper, sp
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolSet
from astrbot.core.astr_agent_context import AstrAgentContext

try:
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
except ModuleNotFoundError as exc:
    if exc.name != "astrbot.core.astr_agent_tool_exec":
        raise
    # The pinned development branch moved the real executor into this package.
    from astrbot.core.astr_agent_runtime.tool_executor import FunctionToolExecutor
from astrbot.core.message.components import Image
from astrbot.core.persona_mgr import DEFAULT_PERSONALITY, PersonaManager
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.func_tool_manager import FunctionToolManager, _PermissionGuardedTool
from astrbot.core.provider.register import llm_tools
from astrbot.core.star.context import Context
from mcp.types import CallToolResult, TextContent
from PIL import Image as PillowImage

ROOT = Path(__file__).resolve().parents[1]
bridge = importlib.import_module(ROOT.name + ".main")
# Use the plugin package's own class identities, including its error classes.
Connection = importlib.import_module(ROOT.name + ".native.models").Connection
actor_from_event = importlib.import_module(ROOT.name + ".native.identity").actor_from_event


class FakeEvent(AstrMessageEvent):
    """Use real event identity/results; capture delivery at the adapter boundary."""

    def __init__(self, case, *, admin=False, components=(), text=None):
        message = AstrBotMessage()
        message.type = MessageType.GROUP_MESSAGE
        message.self_id = "execution-bot"
        message.session_id = message.group_id = "real-execution-group"
        message.message_id = "real-execution-" + case
        message.sender = MessageMember("execution-user", "Executor test")
        message.message = list(components)
        message.message_str = text if text is not None else "executor integration " + case
        message.raw_message = {}
        super().__init__(
            message.message_str,
            message,
            PlatformMetadata("aiocqhttp", "Test adapter", "execution-adapter"),
            message.session_id,
        )
        self.role = "admin" if admin else "member"
        self.sent = []
        self.delivered_files = []

    async def send(self, result):
        # Capture the real image_result and resolve its local file exactly as an
        # adapter would, so a URL/thumbnail or a missing file cannot pass.
        self.sent.append(result)
        assert len(result.chain) == 1 and isinstance(result.chain[0], Image)
        component = result.chain[0]
        assert component.file.startswith("file:")
        path = Path(await component.convert_to_file_path()).resolve()
        self.delivered_files.append((path, path.read_bytes()))


def image_bytes(fmt):
    stream = BytesIO()
    first = PillowImage.new("RGB", (32, 32), "blue")
    if fmt == "gif":
        first.save(
            stream,
            format="GIF",
            save_all=True,
            append_images=[PillowImage.new("RGB", (32, 32), "red")],
            duration=30,
            loop=0,
        )
    else:
        first.save(stream, format="PNG")
    return stream.getvalue()


class Upstream:
    def __init__(self, case):
        self.case, self.requests = case, []
        self.png, self.gif = image_bytes("png"), image_bytes("gif")

    async def __call__(self, request):
        self.requests.append(request)
        assert request.method == "POST", f"Unexpected HTTP request: {request.method} {request.url}"
        assert request.url.host == "upstream.example.test"
        assert request.headers["authorization"] == "Bearer execution-test-key"
        if request.url.path == "/v1/responses" and self.case in {"search", "persona_permissions"}:
            return httpx.Response(
                200,
                json={
                    "id": "response-search",
                    "status": "completed",
                    "output": [
                        {
                            "type": "web_search_call",
                            "id": "web-call",
                            "status": "completed",
                            "action": {
                                "type": "search",
                                "query": "AstrBot executor",
                                "sources": [
                                    {
                                        "title": "AstrBot",
                                        "url": "https://astrbot.app/",
                                        "snippet": "Official documentation",
                                    }
                                ],
                            },
                        },
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "A source-backed upstream summary.",
                                    "annotations": [],
                                }
                            ],
                        },
                    ],
                    "usage": {"input_tokens": 11, "output_tokens": 7},
                },
            )
        expected_path = "/v1/images/edits" if self.case == "edit" else "/v1/images/generations"
        assert (
            self.case in {"generate", "generate_nulls", "generate_legacy", "edit", "gif"}
            and request.url.path == expected_path
        )
        data = self.gif if self.case == "gif" else self.png
        return httpx.Response(
            200,
            json={
                "id": "response-" + self.case,
                "data": [{"b64_json": base64.b64encode(data).decode("ascii")}],
            },
        )


async def filtered_tools(plugin, event, original=None):
    original = original if original is not None else plugin.context.get_llm_tool_manager().get_full_tool_set()
    request = SimpleNamespace(func_tool=original)
    await plugin.on_llm_request(event, request)
    assert isinstance(request.func_tool, ToolSet)
    # Readiness/filtering must not probe a paid HTTP endpoint.
    return request.func_tool


async def execute_text(plugin, event, name, *, tools=None, **arguments):
    tools = await filtered_tools(plugin, event, tools)
    tool = tools.get_tool(name)
    assert isinstance(tool, _PermissionGuardedTool), (name, tools)
    assert tool.handler is None  # The executor must enter call(), preserving permission checks.
    assert tool._wrapped.handler.func is getattr(
        bridge.OpenAINativePlugin,
        {
            "openai_web_search": "web_search",
            "openai_image": "image",
            "openai_image_task": "image_task",
        }[name],
    )
    context = ContextWrapper(context=AstrAgentContext(context=plugin.context, event=event))
    results = [result async for result in FunctionToolExecutor.execute(tool, context, **arguments)]
    assert len(results) == 1 and isinstance(results[0], CallToolResult), results
    result = results[0]
    assert not result.model_dump(by_alias=True)["isError"] and len(result.content) == 1
    assert isinstance(result.content[0], TextContent)
    return result.content[0].text


async def check_search(plugin, event, upstream, *, tools=None):
    result = json.loads(
        await execute_text(
            plugin,
            event,
            "openai_web_search",
            tools=tools,
            queries=["AstrBot executor"],
            domains=["astrbot.app"],
            max_results=2,
        )
    )
    assert result["status"] == "succeeded" and result["untrusted"] is True, result
    assert set(result) == {"operation_id", "status", "sources", "untrusted"}
    assert result["sources"] == [{"title": "AstrBot", "snippet": "Official documentation"}]
    assert (await plugin.service.get_task(event, result["operation_id"]))["status"] == "succeeded"
    body = json.loads(upstream.requests[0].content)
    assert body["model"] == "search-model"
    assert body["tools"] == [{"type": "web_search", "filters": {"allowed_domains": ["astrbot.app"]}}]
    assert body["tool_choice"] == {"type": "web_search"}
    assert body["include"] == ["web_search_call.action.sources"]
    assert json.loads(body["input"].split("\n", 1)[1])["queries"] == ["AstrBot executor"]
    assert not event.sent


async def check_image(plugin, event, upstream, data_dir, case):
    arguments = {"prompt": "executor " + case, "action": "generate", "output_format": "png"}
    if case == "edit":
        reference = data_dir / "reference.png"
        reference.write_bytes(upstream.png)
        event.message_obj.message = [Image.fromFileSystem(str(reference))]
        await plugin.remember_images(event)
        asset = await plugin.service.store.asset_get(actor_from_event(event))
        assert asset and Path(asset["path"]).read_bytes() == upstream.png
        arguments.update(action="edit", references=[asset["asset_id"]])
    elif case in {"generate_nulls", "gif"}:
        arguments.update(
            references=[],
            n=1,
            size=None,
            aspect_ratio=None,
            resolution=None,
            quality=None,
            complexity=None,
            frame_count=None,
            frame_duration_ms=None,
        )
        if case == "gif":
            arguments.update(output_format="gif", frame_count=2, frame_duration_ms=30)
    elif case == "generate_legacy":
        # Exact shape from the failing real chat: auto resolution and GIF frame
        # fillers must not prevent an explicitly static PNG from being submitted.
        arguments.update(
            references=[],
            n=1,
            size="auto",
            aspect_ratio="1:1",
            resolution="auto",
            quality="auto",
            complexity="auto",
            frame_count=2,
            frame_duration_ms=500,
        )
    before = plugin.context.get_llm_tool_manager().get_full_tool_set()
    raw_schema = deepcopy(before.get_tool("openai_image").parameters)
    tools = await filtered_tools(plugin, event, before)
    assert not upstream.requests
    tool = tools.get_tool("openai_image")
    assert tool is not before.get_tool("openai_image")
    assert tool._wrapped is before.get_tool("openai_image")._wrapped
    assert before.get_tool("openai_image").parameters == raw_schema
    assert tool.parameters["properties"]["output_format"]["enum"] == ["png", "gif"]
    assert tool.parameters["properties"]["action"]["enum"] == ["auto", "generate", "edit"]
    if case in {"generate_nulls", "gif"}:
        # Use the image schema even when the set contains the search tool.
        schema = deepcopy(
            next(
                t["function"]["parameters"]
                for t in tools.openai_schema()
                if t["function"]["name"] == "openai_image"
            )
        )
        schema["required"] = list(schema["properties"])
        schema["additionalProperties"] = False
        jsonschema.validate(arguments, schema)
    submitted = json.loads(await execute_text(plugin, event, "openai_image", tools=tools, **arguments))
    assert "error" not in submitted and submitted["status"] in {"queued", "running"}, submitted
    task_id = submitted["task_id"]
    # Task settlement and delivery are separate steps. Join the worker before
    # asserting delivery, avoiding a race with wait_task's terminal-state check.
    await plugin.service.wait_task(event, task_id, 10)
    await asyncio.wait_for(plugin.service.queue.join(), 10)
    task = await plugin.service.get_task(event, task_id)
    assert task["status"] == "succeeded", task
    assert task["generated"] == 1 and task["held"] == 0
    assert task["delivery_status"] == "sent", task
    artifacts = await plugin.service.get_results(event, task_id)
    assert len(artifacts) == len(event.sent) == len(event.delivered_files) == 1
    artifact = artifacts[0]
    expected = upstream.gif if case == "gif" else upstream.png
    assert Path(artifact.path).is_relative_to(data_dir)
    assert event.delivered_files == [(Path(artifact.path).resolve(), expected)]
    assert Path(artifact.path).read_bytes() == expected
    assert artifact.mime == ("image/gif" if case == "gif" else "image/png")
    assert artifact.frame_count == (2 if case == "gif" else 1)
    request = upstream.requests[0]
    if case == "edit":
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        envelope = BytesParser(policy=default).parsebytes(
            b"Content-Type: "
            + request.headers["content-type"].encode("ascii")
            + b"\r\n\r\n"
            + request.content
        )
        assert envelope.is_multipart()
        parts = list(envelope.iter_parts())
        images = [part for part in parts if part.get_param("name", header="content-disposition") == "image[]"]
        assert len(images) == 1 and images[0].get_filename() == "reference_1.png"
        assert images[0].get_content_type() == "image/png"
        assert images[0].get_payload(decode=True) == upstream.png
        body = {
            part.get_param("name", header="content-disposition"): part.get_payload(decode=True).decode()
            for part in parts
            if part.get_filename() is None
        }
        assert body["n"] == "1"
    else:
        assert request.headers["content-type"].startswith("application/json")
        body = json.loads(request.content)
        assert body["n"] == 1
    assert body["model"] == "image-model" and body["prompt"] == arguments["prompt"]
    assert body["output_format"] == arguments["output_format"]
    if case == "gif":
        assert body["frame_count"] == 2 and body["frame_duration_ms"] == 30
        assert artifact.frame_duration_ms == 30
    else:
        assert "frame_count" not in body and "frame_duration_ms" not in body
        assert body["size"] == ("1024x1024" if case == "generate_legacy" else "auto")


async def check_persona_permissions(plugin, upstream):
    event = FakeEvent("persona")
    persona = plugin.context.persona_manager.personas_v3[0]
    persona["tools"] = []
    assert (await plugin.build_tools(event)).empty()  # Real PersonaManager resolver, custom-agent path.
    request_tools = await filtered_tools(plugin, event, ToolSet())
    assert request_tools.empty()
    assert (await plugin.build_tools(event)).empty()  # Snapshot must not resurrect excluded tools.
    partial_event = FakeEvent("persona-search-only", text="请搜索 AstrBot executor")
    persona["tools"] = ["openai_web_search"]
    partial_tools = await plugin.build_tools(partial_event)
    assert [tool.name for tool in partial_tools] == ["openai_web_search"]
    assert [tool.name for tool in (await filtered_tools(plugin, partial_event, partial_tools))] == [
        "openai_web_search"
    ]
    assert not upstream.requests
    await sp.global_put("tool_permissions", {"_default": {"openai_web_search": "admin"}})
    denied = await execute_text(
        plugin, partial_event, "openai_web_search", tools=partial_tools, queries=["AstrBot executor"]
    )
    assert "Permission denied" in denied and "requires admin" in denied, denied
    assert not upstream.requests and not partial_event.sent
    assert all(
        row["used"] == 0 and row["held"] == 0 for row in (await plugin.service.quota(partial_event))["search"]
    )
    # Reuse the very same filtered guard: permissions must consult the current
    # event at call time instead of being bypassed or frozen during filtering.
    partial_event.role = "admin"
    await check_search(plugin, partial_event, upstream, tools=partial_tools)


async def run(case, data_dir):
    upstream = Upstream(case)
    settings = bridge.Settings(
        {
            "limits": {"min_interval_seconds": 0},
            "image": {"model": "image-model", "formats": ["png", "gif"]},
            "gif": {"enabled": True},
        }
    )
    plugin = bridge.OpenAINativePlugin.__new__(bridge.OpenAINativePlugin)
    plugin.settings = settings
    manager = FunctionToolManager()
    context = Context.__new__(Context)
    context.provider_manager = SimpleNamespace(llm_tools=manager)
    # Each pinned source reads its own runner configuration schema.
    config = {
        "agent_runner": {"runner_type": "local", "config": {"persona": {"persona_id": "executor"}}},
        "agent": {"runtime": "astrbot", "config": {"persona": {"persona_id": "executor"}}},
    }
    context.persona_manager = PersonaManager(
        db_helper, SimpleNamespace(default_conf=config, get_conf=lambda umo: config)
    )
    context.persona_manager.personas_v3 = [
        {**deepcopy(DEFAULT_PERSONALITY), "name": "executor", "tools": None}
    ]
    context.conversation_manager = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value=None), get_conversation=AsyncMock()
    )
    plugin.context = context
    plugin.service = bridge.NativeService(
        context,
        settings,
        data_dir / "service",
        transport=httpx.MockTransport(upstream),
        delivery=plugin._deliver,
    )
    # Match StarManager's binding of registered decorator handlers, then obtain
    # guards from the real FunctionToolManager rather than constructing doubles.
    for name in sorted(bridge.TOOL_NAMES):
        registered = llm_tools.get_func(name)
        assert registered is not None and registered.handler is not None, name
        tool = copy(registered)
        tool.handler = partial(registered.handler, plugin)
        manager.func_list.append(tool)
    connection = Connection(
        "provider-A",
        "https://upstream.example.test/v1",
        "execution-test-key",
        model="image-model",
        protocol="images",
        service_type="openai",
        fingerprint="executor-A",
    )
    previous_permissions = None
    permissions_saved = False
    try:
        await db_helper.initialize()
        previous_permissions = await sp.global_get("tool_permissions")
        permissions_saved = True
        await sp.global_put("tool_permissions", {"_default": {name: "member" for name in bridge.TOOL_NAMES}})
        await plugin.service.initialize()
        plugin.service.resolver.resolve = AsyncMock(
            side_effect=lambda event, feature: (
                replace(connection, model="search-model", protocol="responses")
                if feature == "search"
                else connection
            )
        )
        if case == "persona_permissions":
            await check_persona_permissions(plugin, upstream)
        elif case == "search":
            await check_search(plugin, FakeEvent(case, text="请搜索 AstrBot executor"), upstream)
        else:
            await check_image(plugin, FakeEvent(case), upstream, data_dir, case)
        assert len(upstream.requests) == 1, [
            (request.method, request.url.path) for request in upstream.requests
        ]
        assert all(request.method == "POST" for request in upstream.requests)
        context.conversation_manager.get_conversation.assert_not_called()
    finally:
        await plugin.service.close()
        if permissions_saved:
            if previous_permissions is None:
                await sp.global_remove("tool_permissions")
            else:
                await sp.global_put("tool_permissions", previous_permissions)
        await sp.close()
        await db_helper.engine.dispose()
    print("REAL_EXECUTION_OK " + json.dumps({"case": case, "posts": len(upstream.requests)}))


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1], Path(sys.argv[2]).resolve()))
