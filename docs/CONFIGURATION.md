# 配置指南

配置以 `_conf_schema.json` 和 `native/config.py` 的 `DEFAULTS` 为准。搜索和生图各有独立的一组连接字段，修改后保存并重新加载插件。

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

`headers`、`extra_body`、`paths` 是 AstrBot 原生 `dict` 字段，由 ObjectEditor 编辑和保存为对象。嵌套值使用编辑器的 JSON 类型；不要将整个字典填成 JSON 字符串。无需在插件中猜测字符串类型或重复转义。

`keys` 是列表，手动 Key 支持完整 `$NAME` 或 `${NAME}` 环境变量引用。修改环境变量后需要重载插件/供应商；不要在聊天中提供 Key。`proxy` 使用完整代理地址；`timeout=0` 复用已有供应商超时，手动模式使用服务默认。

`paths` 用于定制接口路径：`alpha_search` 默认 `/v1/alpha/search`，`responses` 默认 `/v1/responses`，`generations` 默认 `/v1/images/generations`，`edits` 默认 `/v1/images/edits`。路径相对配置的 Base URL 前缀拼接，末尾 `/v1` 不重复添加；一般无需覆盖。不要把不同来源的结果下载地址配置为 API Base URL。请求头不会携带到外部结果下载。

`extra_body` 只适用于对应接口允许的扩展字段；不能覆盖身份、模型绑定或受保护请求字段。不要把聊天的 temperature、messages 等直接复制给 Images。

## 默认限制

- 搜索最多 4 个查询，默认 5 条结果，上限 10；每条消息最多 3 次搜索调用。
- 生图默认 1 个文件，上限 4；最多 16 张参考图，同时受上游能力限制。
- 默认搜索并发 4、生图并发 2、等待队列 20、每用户同时 1 个生图任务；最小调用间隔 2 秒。
- 输入上限 20MiB、输出上限 100MiB、结果总存储上限 2GiB；像素和 GIF 总帧像素另有限制。
- `network.allow_private_download_hosts` 默认空。仅对确需访问的可信图片主机添加白名单，不应添加任意地址范围。
- `limits.admin_exempt` 默认关闭；管理员豁免额度也不能绕过队列和全局并发。

单次调用省略格式、尺寸、帧参数时，服务层统一使用配置；显式传入非法 `0` 不应变成默认值。
