# 配置指南

搜索与生图可以分别配置。插件控制台的诊断日志设置保存后立即生效，其余设置在重载插件后生效。完整字段和默认值见 [`_conf_schema.json`](../_conf_schema.json)。

## 设置入口与生效方式

- **原生配置页**：仅保留日常常用项——搜索/生图启用开关、API 来源、绑定供应商与模型。
- **插件控制台（专页）**：包含全部高级参数，以及手动模式的 API 地址、密钥、代理、请求头、额度与诊断日志。
- **生效与草稿机制**：诊断日志设置保存后**立即生效**；其他服务配置保存后需在插件管理中**手动重载插件**，页面会高亮展示已保存值与当前运行值的差异。刷新诊断不会清空未保存的草稿；遇到版本冲突时会保留草稿，重新加载最新设置核对后再保存即可。
- **校验与敏感信息**：尚未填写供应商或图片模型时仍可保存（概览会提示尚未就绪）；JSON 格式错误、数值越界或类型不符会阻止保存。密钥、请求头等敏感字段读取时不会明文回传，未操作则保持原值，如需修改请明确选择替换或清除。环境变量引用必须在 AstrBot 运行进程中可见。

在控制台保存后，如需转到 AstrBot 原生配置页继续编辑，请重新打开表单，以便读取刚保存的配置。

## 常见接入示例

### 复用 AstrBot 已有供应商

```json
{
  "search": {"mode": "provider", "provider_id": "已有搜索Provider的ID", "model": "实际支持搜索的模型", "protocol": "responses"},
  "image": {"mode": "provider", "provider_id": "已有生图Provider的ID", "model": "实际图片模型", "protocol": "images"}
}
```

下拉菜单选择的是 AstrBot 中已配置的模型供应商 ID。若选择「当前会话供应商」（`mode=current`），任务提交时会锁定当前会话所选供应商的连接信息，后续聊天切换备用供应商不会影响已提交的任务。

### OpenAI 官方 API

```json
{
  "search": {"mode": "manual", "base_url": "https://api.openai.com/v1", "keys": ["${OPENAI_API_KEY}"], "model": "已授权的搜索宿主模型", "service_type": "openai", "protocol": "responses"},
  "image": {"mode": "manual", "base_url": "https://api.openai.com/v1", "keys": ["${OPENAI_API_KEY}"], "model": "已授权的图片模型", "service_type": "openai", "protocol": "images"},
  "gif": {"enabled": false, "frame_count": 9, "frame_duration_ms": 200}
}
```

建议先验证静态图片生成，确认账号具备原生 GIF 权限后再启用 GIF。示例中的中文说明请替换为账号实际可用的模型 ID。

### NewAPI

```json
{
  "search": {"mode": "manual", "base_url": "https://newapi.example.invalid/v1", "keys": ["${NEWAPI_KEY}"], "model": "已映射搜索模型", "service_type": "newapi", "protocol": "alpha"},
  "image": {"mode": "manual", "base_url": "https://newapi.example.invalid/v1", "keys": ["${NEWAPI_KEY}"], "model": "已映射图片模型", "service_type": "newapi", "protocol": "images"}
}
```

Alpha Search 需要对应的 Alpha 渠道支持，生图需要可用的 Images 渠道；仅支持 Responses 的 Codex 直连渠道不代表支持 Images 或 GIF。

### Sub2API

```json
{
  "search": {"mode": "manual", "base_url": "https://sub2api.example.invalid/v1", "keys": ["${SUB2API_KEY}"], "model": "实际搜索模型", "service_type": "sub2api", "protocol": "alpha"},
  "image": {"mode": "manual", "base_url": "https://sub2api.example.invalid/v1", "keys": ["${SUB2API_KEY}"], "model": "实际图片模型", "service_type": "sub2api", "protocol": "images"}
}
```

不同账号类型与权限对应的接口路径可能不同。如需将搜索改走 `responses` 可单独切换协议；为避免重复计费，请求失败后不会自动切换其他协议重试。

### Responses 生图

将 `image.protocol` 设为 `responses` 时，`image.model` 填图片模型。`image.host_model` 指定执行 Responses 的宿主模型，留空时沿用所选供应商的模型。手动 API 模式须明确填写两者。原生 GIF 使用 `images` 协议。

## 连接与高级参数

- **JSON 对象字段**：`headers`、`extra_body`、`paths` 均以 JSON 对象格式编辑（如 `{"X-Custom": "value"}`），切勿将整个对象包成字符串；清空请求头请提交 `{}`。
- **密钥与代理**：`keys` 为列表，支持明文或 `$NAME`、`${NAME}`、`env:NAME` 环境变量引用（修改环境变量后需重载插件/供应商，切勿在聊天中发送密钥）。`proxy` 填完整代理地址；`timeout` 填 `0` 时复用供应商超时，手动模式使用默认超时。
- **接口路径覆盖（`paths`）**：默认分别为 `alpha_search`（`/v1/alpha/search`）、`responses`（`/v1/responses`）、`generations`（`/v1/images/generations`）、`edits`（`/v1/images/edits`），相对 Base URL 自动拼接且不重复追加 `/v1`，通常无需修改。外部结果图片下载不会携带 API 请求头。
- **扩展请求体（`extra_body`）**：仅透传目标接口允许的扩展字段，不会覆盖认证、模型绑定等受保护字段。Alpha Search 请求体固定为 `id`、`model`、`commands`，不会携带 `extra_body`，因此同一供应商可同时用于带 `reasoning_effort` 的聊天与 Alpha Search。

## 群聊 @ 触发限制

`image.require_at_in_group` 默认关闭，可在控制台「生图设置 → 图片连接」开启。开启后，群聊中的静态图、改图与 GIF 请求（含命令、自然语言工具及公共 API）均须在当前消息中真正 @ 本机器人：

- 仅 @ 其他用户、@ 全体、直接叫机器人昵称或引用机器人消息均不满足条件。
- 需要参考他人头像时，可在同一条消息中同时 @ 本机器人与目标用户。
- 私聊、网页搜索及已有任务查询/取消不受此开关影响。保存后请重载插件。

## 默认限额与运行限制

- **网页搜索**：默认单次最多 1 个查询、返回 3 条结果，单条摘要上限 240 字符，返回给模型的总预算 2000 字符；每条消息最多触发 2 次搜索调用（首次 + 1 次补查）。
- **搜索触发策略**：默认 `explicit`（仅响应明确搜索请求）。私聊或已唤醒的群聊中，发送「帮我搜索 xxx」「搜索 xxx」会先搜索再回答；若仅说「帮我搜索」且无上下文主题，模型会追问搜索内容。改为 `on_demand` 后模型可按需自主搜索。「不要搜索」「在聊天记录里搜索」等不会触发网页搜索。关闭 AstrBot 内置网页搜索不影响本插件。
- **引用与链接**：默认不向模型返回来源链接（`show_links`）和引用标记（`show_citations`），`include_raw` 默认关闭。这两项仅控制工具返回给模型的数据，无法强制模型最终回复的排版格式。
- **生图与并发**：单次默认生成 1 张、上限 4 张，最多携带 16 张参考图（同时受上游限制）。默认搜索并发 4、生图并发 2、等待队列 20、单用户同时 1 个生图任务、最小调用间隔 2 秒。
- **文件与存储**：单张输入上限 20 MiB，单张输出上限 100 MiB，结果总存储上限 2 GiB；另设单帧及 GIF 总帧像素上限。`network.allow_private_download_hosts` 默认留空，仅在确需访问可信内网图片地址时按主机名放行。
- **每日额度**：默认搜索为群聊每人 20 次 / 单群 200 次 / 私聊每人 20 次；生图为群聊每人 3 张 / 单群 30 张 / 私聊每人 3 张，按 `limits.timezone`（默认 `UTC`）每日重置。群聊个人额度跨群累计。`0` 表示不限额（兼容旧版 `-1`；若旧配置曾用 `0` 禁用功能，请改用对应启用开关）。
- **生图计次与提醒**：每张提交给上游的图片占用 1 单位额度，失败与审核拒绝同样计次；参数校验失败、排队中取消以及多图任务未提交部分不扣额度。待核对任务保留预占，人工核对后不重复扣除。当有限额度剩余 2 张及以下时，图片投递或失败通知会附带余量提醒（不限额及管理员豁免时不提醒）。`limits.admin_exempt` 默认关闭，开启后仅免除额度限制，仍受队列与并发控制。
- **绘图预设与回执**：`image.presets` 为预设名到提示词的映射，命令中用 `--预设 名称`，自然语言可在提示词开头写 `[预设:名称]`。`image.acceptance_template` 控制固定命令的受理回执，支持 `{kind}` 和 `{task_id}` 占位符。

## 免费检查与计费实测

- **免费检查**：控制台概览的配置检查与 `/onative status` 仅读取已加载配置和公开特征，不发起付费请求。点击「重置能力记录」仅清除本地缓存的判断记录，不会触发探测。
- **计费实测**：管理员在聊天中主动发送 `/onative probe search confirm`、`/onative probe generate:png confirm` 或 `/onative probe generate:gif confirm` 才会向真实上游发起测试（改图测试需附图或引用图片）。探测同样受额度、队列与超时规则约束。

## 诊断日志

控制台「概览」中的诊断日志默认关闭，**开启并保存后立即生效**，无需重载插件。`diagnostics.max_mb` 默认 100 MB（支持 1–10000 MB），统计范围包含当前文件与最多 4 个轮转备份，超限自动清理最旧日志。

- **存储位置**：`data/plugin_data/astrbot_plugin_openai_native/diagnostics`（JSONL 格式）。关闭记录或重载/重启插件不会删除已有磁盘日志。
- **记录内容**：包含 AstrBot 版本、插件版本与入口代码指纹、命令前缀、唤醒状态、附图数量、匹配的处理器、工具筛选原因、任务受理状态及脱敏错误。**不保存聊天正文、提示词、图片内容/地址、请求体或密钥**。
- **排查生图触发问题**。开启日志后复现一次，记下时间和消息跟踪 ID，从概览下载诊断日志 ZIP，对照工具筛选和任务受理记录。消息若在到达本插件前已被拦截，需要结合 AstrBot 的日志排查。
