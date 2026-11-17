"""Recognize direct web requests in the current message, not in chat history."""

import re

_VERB = (
    r"(?:请(?:你)?|麻烦(?:你)?|能不能|能否|能|可以)?\s*(?:帮我|帮忙)?\s*"
    r"(?:(?:联网|上网|网上)\s*(?:搜索|检索|搜一搜|搜一下|搜下|查一查|查一下|查找|查询|搜|查)|"
    r"搜索|检索|搜一搜|搜一下|搜下|搜搜)"
    r"(?:一下)?|(?:please\s+)?(?:search(?:\s+for)?|look\s+up|browse\s+the\s+web|find\s+sources)\b"
)
_REQUEST = re.compile(r"^(?:" + _VERB + r")", re.I)
_NO_WEB = re.compile(
    r"(?:不需要|不要|不用|无需|别|禁止)(?:再|去|帮我)?(?:联网|上网|搜索|搜|查)|"
    r"(?:do not|don't|without)\s+(?:search|brows|look(?:ing)? up)|"
    r"(?:在|从)(?:本地|群聊?|聊天记录|历史消息|文件夹|数据库)(?:中|里|内)?(?:搜索|查找|检索)|"
    r"^(?:请(?:你)?|帮我|请帮我)?(?:搜一下|搜一搜|查一下|搜索|查找|查询|检索)(?:一下)?\s*(?:本地文件|群聊记录|聊天记录|我的文件|我的订单)|"
    r"^(?:网页)?搜索(?:(?:功能|按钮|命令|工具|结果|引擎|设置|开关|框|算法)(?:怎么|如何|是|有|为|在|的|呢|[？?]|$)|是什么意思|怎么用)|"
    r"^(?:please\s+)?(?:search|look\s+up)\s+(?:the\s+|my\s+)?(?:chat history|local files|database)\b",
    re.I,
)


def web_forbidden(text: str) -> bool:
    return bool(_NO_WEB.search(text.strip()))


def direct_query(text: str) -> str | None:
    """None = not a direct request; empty = explicit request without a subject."""
    text = text.strip()
    match = _REQUEST.match(text)
    if not match or web_forbidden(text):
        return None
    return text[match.end():].strip(" \t\r\n:：，,。！？!?")
