# 配置指南

配置以 `_conf_schema.json` 和 `native/config.py` 的 `DEFAULTS` 为准。搜索和生图各有独立的一组连接字段，修改后保存并重新加载插件。

## 设置入口与生效方式

原生配置只保留搜索/生图开关、API 来源、已有供应商选择和模型。详细参数放在插件页面；手动连接的 Base URL、Key、代理和请求头也在该页设置。

专页保存使用版本校验和局部更新。诊断日志设置保存后立即生效；其他服务配置保存后需手动重载插件，页面显示已保存与运行值的差异。刷新诊断不重置草稿；冲突时保留草稿，核对后重新加载设置再保存。原生配置页继续沿用 AstrBot 自身行为，打开过的旧原生表单需要重新打开再编辑。

未填写供应商/图片模型可保存，但概览显示尚未就绪；非法 JSON、范围和类型会阻止保存。Key、请求头等敏感字段不会在读取时回传，需要明确选择替换或清除；未操作则保持原值。环境变量必须在 AstrBot 进程可见。

## 三种接入

### 复用已有供应商

```json
{
  "search": {"mode": "provider", "provider_id": "已有搜索Provider的ID", "model": "实际支持搜索的模型", "protocol": "responses"},
  "image": {"mode": "provider", "provider_id": "已有生图Provider的ID", "model": "实际图片模型", "protocol": "images"}
}
```

Provider 下拉使用 AstrBot 原生 `_special: select_provider`，选择的是模型 Provider ID，不是 Provider Source ID。`mode=current` 会在提交时按完整 UMO 查询会话 Provider，固定其连接快照；聊天 fallback 不改变插件绑定。

### OpenAI 官方 API

```json
{
  "search": {"mode": "manual", "base_url": "https://api.openai.com/v1", "keys": ["${OPENAI_API_KEY}"], "model": "已授权的搜索宿主模型", "service_type": "openai", "protocol": "responses"},
  "image": {"mode": "manual", "base_url": "https://api.openai.com/v1", "keys": ["${OPENAI_API_KEY}"], "model": "已授权的图片模型", "service_type": "openai", "protocol": "images"},
  "gif": {"enabled": false, "frame_count": 9, "frame_duration_ms": 200}
}
```

先用静态图片验证，取得原生 GIF 权限后再启用 GIF。模型名称应使用账号实际可用值；示例中的中文说明不是可发送模型 ID。

### NewAPI

```json
{
  "search": {"mode": "manual", "base_url": "https://newapi.example.invalid/v1", "keys": ["${NEWAPI_KEY}"], "model": "已映射搜索模型", "service_type": "newapi", "protocol": "alpha"},
  "image": {"mode": "manual", "base_url": "https://newapi.example.invalid/v1", "keys": ["${NEWAPI_KEY}"], "model": "已映射图片模型", "service_type": "newapi", "protocol": "images"}
}
```

Alpha 需要相应渠道支持；Images 需要可用的 Images 渠道。直接 Codex 渠道不能因为“支持 Responses”就被视为支持 Images/GIF。

### Sub2API

```json
{
  "search": {"mode": "manual", "base_url": "https://sub2api.example.invalid/v1", "keys": ["${SUB2API_KEY}"], "model": "实际搜索模型", "service_type": "sub2api", "protocol": "alpha"},
  "image": {"mode": "manual", "base_url": "https://sub2api.example.invalid/v1", "keys": ["${SUB2API_KEY}"], "model": "实际图片模型", "service_type": "sub2api", "protocol": "images"}
}
```

不同账号类型和权限可能采用不同执行路径。可以单独将搜索改为 `responses`；不在付费请求失败后自动切换协议重试。

### Responses 生图

设置 `image.protocol=responses`，`image.host_model` 为执行 Responses 的宿主模型，`image.model` 为工具的图片模型。两者分别配置。此路径不声明 GIF；GIF 使用 Images。

## 连接与高级参数

`headers`、`extra_body`、`paths` 在详细页中以 JSON 对象编辑，例如 `{"X-Custom":"value"}`，不要额外包成 JSON 字符串。敏感对象通过明确替换提交；清空请求头使用 `{}`。已有配置键和旧值继续兼容。

`keys` 是列表，手动 Key 支持完整 `$NAME`、`${NAME}` 或 `env:NAME` 环境变量引用。修改环境变量后需要重载插件/供应商；不要在聊天中提供 Key。`proxy` 使用完整代理地址；`timeout=0` 复用已有供应商超时，手动模式使用服务默认。

`paths` 用于定制接口路径：`alpha_search` 默认 `/v1/alpha/search`，`responses` 默认 `/v1/responses`，`generations` 默认 `/v1/images/generations`，`edits` 默认 `/v1/images/edits`。路径相对配置的 Base URL 前缀拼接，末尾 `/v1` 不重复添加；一般无需覆盖。不要把不同来源的结果下载地址配置为 API Base URL。请求头不会携带到外部结果下载。

`extra_body` 只适用于对应接口允许的扩展字段；不能覆盖身份、模型绑定或受保护请求字段。不要把聊天的 temperature、messages 等直接复制给 Images。

Alpha 搜索的请求体固定为 `id`、`model` 和 `commands`。供应商或插件配置的 `extra_body` 仅在其他支持附加参数的协议中生效，因此同一供应商可以同时用于配置了 `reasoning_effort` 的聊天和 Alpha 搜索。

## 群聊 @触发

`image.require_at_in_group` 默认 `false`，在“生图设置 → 图片连接”编辑。开启后，静态图、编辑和 GIF 的命令、自然语言工具及公共 API 提交均须在当前群消息中真正 @本机器人。只 @其他人或其他 bot、@全体、文字昵称、引用 bot 消息均不满足；可以同时 @本机器人和需要参考头像的用户。私聊、搜索和已有任务管理不受影响。保存后重新加载插件。

## 默认限制

- 搜索默认每次 1 个查询、3 条结果；面向模型的单次搜索回应默认限制 2000 字符，每条摘要最多 240 字符。每条消息默认最多 2 次搜索调用（首次及一次补查）。这些限制均可在插件页面调整。
- 默认 `explicit` 仅响应用户的搜索请求。私聊或已唤醒的群聊中，“帮我搜索 AstrBot 更新日志”“搜索 AstrBot 更新日志”会在回答前搜索一次，两种策略都适用。只有“帮我搜索”时，有明确上下文可由模型提取主题，否则询问搜什么。`on_demand` 还允许模型为其他问题按需搜索。关闭 AstrBot 内置网页搜索不影响本插件工具。
- 直接搜索仍受当前会话的工具权限和额度限制，失败不会自动重试。“不要搜索”“在聊天记录里搜索”等不触发网页搜索。搜索设置默认显示连接、触发方式和每日额度；查询上限、返回内容和协议在「高级搜索设置」中。
- 默认不向模型返回来源链接或引用标记；`show_links`、`show_citations` 控制工具输出，不能保证模型最终聊天内容绝不出现链接。内部任务仍保留完整来源供诊断。`include_raw` 默认关闭。
- 生图默认 1 个文件，上限 4；最多 16 张参考图，同时受上游能力限制。
- 默认搜索并发 4、生图并发 2、等待队列 20、每用户同时 1 个生图任务；最小调用间隔 2 秒。
- 输入上限 20MiB、输出上限 100MiB、结果总存储上限 2GiB；像素和 GIF 总帧像素另有限制。
- `network.allow_private_download_hosts` 默认空。仅对确需访问的可信图片主机添加白名单，不应添加任意地址范围。
- `limits.admin_exempt` 默认关闭；管理员豁免额度也不能绕过队列和全局并发。
- 生图默认群聊每人每天 3 张、每群 30 张、私聊每人 3 张，按 `limits.timezone` 的日期重置（默认 UTC）。已向上游提交的每张图片占用 1 单位额度，失败（含审核拒绝）也计数；多图任务尚未提交的部分、参数校验失败和排队中取消的任务不扣除。结果不明的请求保留预占，核对后仍按已提交次数计入，不会重复扣除。实际生成数独立记录，失败不会显示成已生成。
- 图片投递后，个人或本群有限额度剩余 2 张及以下时发送提醒；失败通知也附带同一提醒。`0` 不限额及管理员豁免时不提醒。处理中预占计入剩余额度；提醒发送失败不改变图片的投递状态。
- 搜索和图片的群聊个人、群组、私聊日额度分别设置；群聊个人额度跨群累计。`0` 表示不限制；旧配置中的 `-1` 也按不限制读取。旧版用 `0` 禁用的配置，升级后请改用功能启用开关。

`image.presets` 是名称到提示词的映射。固定命令可用 `--预设 名称`，自然语言工具可在提示词开头写 `[预设:名称]`；预设内容与当次要求组合后提交。`image.acceptance_template` 控制固定命令任务受理回复，允许 `{kind}` 与 `{task_id}`，默认单行。自然语言回复仍由聊天模型生成。

单次调用省略格式、尺寸、帧参数时，服务层统一使用配置；显式传入非法 `0` 不应变成默认值。


## 免费检查与计费测试

概览中的配置检查读取已加载供应商及公开服务特征，不发送付费搜索或生图。清除旧能力记录仅重置证据，不代表已通过账号验证。管理员主动执行 `/onative probe search confirm` 或 `/onative probe generate:png confirm` 才测试上游；GIF 用 `generate:gif`，编辑测试需当前消息或引用中有参考图。所有探测遵循统一额度、队列和超时规则。

## 诊断日志

概览中的“诊断日志”默认关闭；开启并保存后立即生效，无需重载插件。`diagnostics.max_mb` 默认 100 MB，表示当前文件与轮转备份的总上限，可设置 1–10000 MB。其他配置仍按页面提示手动重载。

文件位于 AstrBot 的 `data/plugin_data/astrbot_plugin_openai_native/diagnostics`，使用 JSONL 格式，最多保留当前文件及 4 个轮转备份。达到上限后清理最旧日志；缩小上限会清理旧文件。关闭开关会停止追加，保留已有文件；重载或重启不会清空磁盘日志。

记录包含 AstrBot 版本、插件版本与入口代码指纹、命令前缀及分隔符类型、唤醒状态、当前图片数、已匹配处理器、工具进入或离开请求的原因、任务受理状态和脱敏错误。日志不保存聊天正文、提示词、图片内容、图片地址、请求体或密钥；消息跟踪 ID 与关联 ID 用于串联同一次处理。更早阶段已中止、尚未到达本插件的消息无法由插件记录；缺少后续记录也不能单独认定某插件抛出了异常。

页面显示当前日志开关、占用量、文件名和保存异常。写入失败不会阻断聊天或任务执行，修复权限或磁盘空间后可继续记录。最近调用诊断仍保留最多 200 条内存记录，插件重载后清空；文件日志独立按容量保留。

排查“没有触发生图”时，开启后重现一次，提供对应时间、消息跟踪 ID 和相关 JSONL 记录即可；页面中的运行版本和代码指纹可以核对实际加载的入口代码。
