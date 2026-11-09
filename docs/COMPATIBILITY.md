# 兼容范围与源码依据

研究日期：2026-11-09。以下是源码与协议依据，不代表真实账号成功记录。插件不依赖参考生图插件，不复制其 AGPL 代码。

| 项目 | 核验基线 | 结论 |
| --- | --- | --- |
| AstrBot | v4.29.4，`61335414433db721b3f7ecc93f90deafe0a59d78` | 公开 Provider 查询、标准 llm_tool、on_llm_request、ToolSet 和插件数据目录可用 |
| Angel Heart | `562c8904e0fba7289880e52bab8c3a765e0e5778` | 群聊决策后进入标准主 Agent；请求钩子不清空工具 |
| NewAPI | main `bd6bb53019fb7e21804b31ab7365c9b2097f4b6c`，rc.49 | Alpha 渠道受限；直接 Codex 渠道 Images 转换不支持 |
| Sub2API | main `ee58c0013c76ccd3f36439881bc848d342ec1c9d`，v0.2.20 | 账号类型影响 Alpha/Responses/Images；2.6 路径保留帧参数 |

额外兼容检查固定研究时的 AstrBot master 提交 `6b10af75e118714335aff3aab047bf7689fa1388`；它与最低支持版本分别运行，不以滚动分支名代替可复现基线。

## AstrBot 接入

- `astrbot/api/event/filter/__init__.py` 导出 `llm_tool`、`command`、`on_llm_request`。
- `astrbot/core/star/register/star_handler.py` 解析工具 docstring 的 `Args` 类型，包括 `array[string]`；不能只写 Python 注解。
- `astrbot/core/agent/tool.py` 的 ToolSet 保存工具列表。请求钩子只浅复制集合及目标工具，再深复制参数，保留权限包装的调用逻辑。
- `astrbot/core/platform/astr_message_event.py` 的 `is_admin()` 等价于 `role == "admin"`；`get_platform_id()` 是实例 ID，`get_sender_id()` 才是用户 ID。
- `dashboard/src/components/shared/ConfigItemRenderer.vue` 支持 `_special=select_provider` 和 `type=dict`（ObjectEditor）；分组使用 `type=object`、`items`。

永久源码链接：

- https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/star/register/star_handler.py
- https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/provider/func_tool_manager.py
- https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/dashboard/src/components/shared/ConfigItemRenderer.vue

## 引用消息

OneBot 适配器通常按回复 ID 获取消息，并构造含 `chain=abm_reply.message` 的 Reply；失败时可能保留空链。`astrbot.core.utils.quoted_message.extract_quoted_message_images(event, reply_component)` 提供按引用 ID 补取和图片解析入口，其返回的临时素材必须在事件结束前复制。

只读取用户明确引用的消息，不扫描群共享历史。平台无法返回原引用图片时，应报缺少参考图或请用户重新发送，不能拿其他成员最近图片代替。

- https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py
- https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/astrbot/core/utils/quoted_message/extractor.py

## OpenAI 与网关

- Responses hosted search 读取搜索调用和 URL 引用；Alpha 使用其独立 commands 请求结构，`response_length` 位于 `commands` 中。其结构依据 openai/codex 提交 `e54a5713caf331f42a6eef0166186df5f7563c29` 的 `codex-rs/codex-api/src/search.rs`。
- Responses 生图的宿主模型与图片工具模型分开配置，不能假定 Images 模型在 Responses 同样可用。
- GIF 采用官方指南较保守的 20–2000ms 范围；文档不同页面的时长上限存在差异。账号仍需 GIF preview 权限。
- 入口服务识别不等于模型、渠道和账号能力检测。泛化的 400/403/500 不足以确定故障层级。

官方文档：

- https://developers.openai.com/api/docs/guides/tools-web-search
- https://developers.openai.com/api/docs/guides/image-generation
- https://developers.openai.com/api/docs/guides/tools-image-generation

源码依据：

- https://github.com/QuantumNous/new-api/blob/bd6bb53019fb7e21804b31ab7365c9b2097f4b6c/relay/alpha_search_handler.go
- https://github.com/QuantumNous/new-api/blob/bd6bb53019fb7e21804b31ab7365c9b2097f4b6c/relay/channel/codex/adaptor.go
- https://github.com/Wei-Shaw/sub2api/blob/ee58c0013c76ccd3f36439881bc848d342ec1c9d/backend/internal/service/openai_alpha_search.go
- https://github.com/Wei-Shaw/sub2api/blob/ee58c0013c76ccd3f36439881bc848d342ec1c9d/backend/internal/service/openai_images_direct.go
