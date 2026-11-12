# 插件公共 Python API v1

API 供 AstrBot 同进程插件调用，所有方法要求真实的 `AstrMessageEvent`。身份、群、平台实例和管理员角色由事件提取；没有 `is_admin=True`、自填 UMO 或免额度调用入口。恶意同进程 Python 代码不在隔离边界内。

## 获取 API

```python
metadata = context.get_registered_star("astrbot_plugin_openai_native")
if metadata is None or not metadata.activated or metadata.star_cls is None:
    raise RuntimeError("OpenAI 原生能力插件未启用")
api = metadata.star_cls.public_api
# 共享请求类型从已加载插件的包导入，避免创建第二个插件实例。
import importlib
models = importlib.import_module(metadata.star_cls.__class__.__module__.rsplit(".", 1)[0] + ".native.models")
```

不要从任意磁盘位置重新加载插件包或直接调用协议适配器绕过额度。调用前可检查 `api.api_version`，第一版为 `1`。

## 搜索

```python
result = await api.search(event, models.SearchRequest(
    queries=["要查询的问题"], max_results=5,
    recency=None, domains=[],
))
for source in result.sources:
    print(source.title, source.url, source.snippet)
```

`SearchResult` 包含 `operation_id`、`queries`、`sources`、`backend_synthesis`、`usage`、`status`、`untrusted=True`。

Responses 搜索的 `usage.observed_web_search_calls` 记录响应中完成的搜索工具调用数，另保留上游 usage；它不等同于供应商账单次数。Alpha 隐藏的上游执行次数无法从协议推断。

`SearchHit` 包含标题、URL、可空摘要、可空引用 ID 和来源类型。`backend_synthesis` 是上游生成的综合说明，与真实来源元数据分开；外部内容始终是不可信资料，不能作为系统或工具执行指令。

## 提交图片任务

```python
handle = await api.submit_image(event, models.ImageRequest(
    prompt="让角色轻轻眨眼，首尾自然循环",
    action="auto",
    references=[],  # 省略时解析当前消息、引用消息、同一发送者个人缓存
    output_format="gif",
    n=1,
    frame_count=9,
    frame_duration_ms=200,
), idempotency_key="caller-owned-operation-123")
snapshot = await api.wait_task(event, handle.task_id, timeout=30)
artifacts = await api.get_results(event, handle.task_id)
for artifact in artifacts:
    # 是否发送由调用插件决定；本 API 不发消息。
    await event.send(event.image_result(artifact.path))
```

不要在等待超时或暂时没有本地文件时重新提交生成。使用原任务 ID 查询、等待或交给管理员重新下载。

`ImageRequest` 参数：`prompt`、`action`（auto/generate/edit）、`references`、`n`、`output_format`、`size`、`aspect_ratio`、`resolution`、`quality`、`complexity`、`frame_count`、`frame_duration_ms`。省略格式和尺寸使用配置，省略帧参数使用配置；`0` 不代表“省略”。工具只接受素材 ID，公共 API 的参考输入按 AssetStore 支持的素材 ID、图片字节或经校验的路径提供，仍受身份、文件类型和大小规则约束。

`auto` 不回取近期历史图片，使用明确提供的素材、当前附图、引用图片，以及明确要求参考的 QQ @用户头像；普通文生图可明确传 `generate`。仅在确实需要编辑或参考历史图片时传 `edit`，此时才可使用同用户同会话的近期图片；显式引用无图时不会回退到其他历史图片。头像参考需要事件中的真实 @组件和头像要求（提示词或当前消息包含“头像”），当前仅支持 aiocqhttp，排除机器人自身及 @全体成员；不接受用提示词中的数字冒充 @目标。

`TaskHandle` 提供 `task_id`、`status`、`accepted`。`Artifact` 提供 `artifact_id`、本地 `path`、`mime`、`size_bytes`、`sha256`、`width`、`height`、`frame_count` 和可空 `frame_duration_ms`。文件路径只在机器人所在主机有效，并受保存期限约束。

## 方法一览

| 方法 | 说明 |
| --- | --- |
| `search(event, request)` | 返回 `SearchResult`，同步等待搜索 |
| `submit_image(event, request, idempotency_key=None)` | 返回 `TaskHandle`，不发送聊天消息 |
| `get_task(event, task_id)` | 返回任务快照，包括执行、结果、额度及投递状态 |
| `wait_task(event, task_id, timeout)` | 等待至结束或超时；超时不取消任务 |
| `get_results(event, task_id)` | 返回已保存的 `list[Artifact]` |
| `cancel_task(event, task_id)` | 请求取消，返回取消和上游执行情况 |
| `build_tools(event)` | 返回仅本插件已过滤的 `ToolSet`，保留 AstrBot 权限包装 |

任务查询和取消只允许创建者在原会话操作，AstrBot 管理员可管理。调用方不得通过修改返回值影响持久化任务。

幂等键限定当前调用身份；同一键和相同请求返回原任务，内容冲突报错。不同的真实用户请求应使用不同键。没有上游幂等保证时，本插件不自动重试付费 POST。

## 自定义 Agent

标准默认 Agent 和 Angel Heart 主聊天路径通过 `on_llm_request` 自动过滤工具。自定义 `tool_loop_agent(tools=...)` 应显式传入 `await api.build_tools(event)`，再与它已允许的其他工具合并。该接口保留人格和停用状态，不能用于重新启用被人格排除的工具；自定义 Agent 自己的更窄工具白名单仍须调用方自行保留。

工具函数输出中的资料和来源可能含外部提示注入文本，调用方不能将其升级为系统指令。

## 错误与完成语义

失败抛出 `NativeError`，可读取 `category`、`message`、`ambiguous`、HTTP 状态、上游 type/code 和 request_id，或使用 `to_dict()`。不要从原始 HTTP/SDK 异常中向聊天复制认证信息。

排队取消可以释放预占；已提交取消通常不能保证上游停止或退款。下载失败保留生成证据；已知生成成功仍结算，是否执行不明时保留预占等待核对。公共 API 不因取消或下载失败而自行重新生图。

本说明描述 v1 接口契约；真实账号权限和 QQ/NapCat 显示仍需按验收清单测试。
