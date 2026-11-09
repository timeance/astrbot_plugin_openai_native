# 验收与开发

## 本地自动化检查

在独立虚拟环境安装运行依赖及 `pytest pytest-asyncio ruff PyYAML`，执行：

```console
python -m pytest -q
python -m ruff check .
```

默认测试不使用真实 API Key、不执行付费请求。CI 在 Windows/Linux、Python 3.12/3.13 运行测试、Ruff 及元数据/配置一致性检查。

`tests/test_bridge.py` 使用明确的 AstrBot 4.29.4 契约替身，只能证明桥接逻辑；不能称为真实 AstrBot 加载、完整 Agent 或 QQ 集成通过。`tests/test_schema.py` 检查原生配置字段与运行默认值一致。

可选真实注册检查：在已经具备 AstrBot 4.29.4 依赖的独立测试环境，将 AstrBot 源码放入 PYTHONPATH，设置 `OPENAI_NATIVE_REAL_ASTRBOT=1`，再运行 `python -m pytest tests/test_bridge.py -q`。此检查仅导入真实注册 API，不会提交计费请求。已在独立 Python 3.12 环境对固定 v4.29.4 源码完成检查，三个工具和图片缓存消息钩子均已注册；CI 分别对最低版本和研究时的 master 固定提交运行真实框架注册检查。完整 Agent 和聊天平台仍须实测。

## 发布前真实验收

以下均尚未在本次开发中使用真实账号或聊天平台验证。每项记录：日期、插件/框架/网关版本、模型、渠道和账号类型、请求数、费用上限、脱敏错误及结果证据。

| 环境 | 需要验证 |
| --- | --- |
| OpenAI 官方 API | Responses 搜索及引用、Images 静态生成与编辑、Responses 生图、获准账号原生 GIF |
| Sub2API | 实际使用的 API Key/OAuth/setup token/PAT 账号路径与权限差异 |
| NewAPI | Codex、Sub2API、兼容 Images 渠道分别记录支持/不支持，不相互推导 |
| 完整部署链路 | Sub2API → NewAPI → AstrBot 的参数、结果、错误透传 |
| 默认 Agent / Angel Heart | 自主调用、人格禁用、真实参考图、背景任务完成 |
| QQ / NapCat | 原始 GIF 动画、文件大小限制、延迟发送、失败后不重复生成 |

## 必测行为

1. A/B/C/D Provider 下拉与“当前会话”；聊天从 A fallback B 后插件仍访问绑定的 A。排队期间修改会话不改变已提交任务。
2. 两个用户、两个群、同用户跨群与私聊：引用图和任务所有权不串，群聊用户额度跨群累计，私聊单独。
3. 搜索只接受实际搜索执行证据；URL 引用可用，缺摘要留空，上游综合内容标记为不可信资料。
4. 当前图、引用图、两参考图、两输出；缺参考图不从群历史猜测，输入动画不静默取首帧。
5. PNG/JPEG/WebP/GIF 文件魔数与实际格式一致；GIF `n_frames > 1`、帧参数符合实际输出，发送前后原始文件未重新编码。
6. 同结果 URL 下载失败重试仍只有一次生成 POST；响应截断或超时标记待核对，不换 Key、模型或协议重发。
7. 排队取消、执行中取消、重启、部分成功、结果暂不可取和投递失败：额度结算幂等，不重复扣除或退款。
8. 公共 API 不自动发消息；等待超时不取消；管理员探测只有明确确认才提交。
9. 日志和聊天错误不出现 API Key、认证头、签名下载链接等信息。

## 故障核对

优先查看原任务状态。已生成但未下载：执行管理员 `retry-download`。执行情况未知：在上游账单或记录确认后使用 `reconcile` 填实际文件数。不要仅因客户端超时填 0，也不要用新任务“试一下”。任务完成和消息投递是不同状态。

## 交付状态

项目以独立 Apache-2.0 源码交付到 timeance/astrbot_plugin_openai_native。完整第一版在本地验收后提交 Draft PR；发布与部署仍需单独授权。上表中没有实际账号证据的能力保持实验标记。
