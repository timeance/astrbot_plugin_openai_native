# SPDX-License-Identifier: Apache-2.0
"""Parse image command arguments as text, without shell interpretation."""

from __future__ import annotations

import re

from .models import ImageRequest, NativeError

# Quoted text is opaque; unmatched quotes and apostrophes remain ordinary text.
_TOKENS = re.compile(r"""(?:"[^"]*"|'.*?')(?=\s|$)|\S+""", re.DOTALL)
_OPTIONS = {
    "--数量": "n",
    "--比例": "aspect_ratio",
    "--分辨率": "resolution",
    "--预设": "preset",
    "--尺寸": "size",
    "--格式": "output_format",
    "--帧数": "frame_count",
    "--帧时长": "frame_duration_ms",
}
_NATURAL_OPTIONS = {"比例": "aspect_ratio", "分辨率": "resolution"}
_RATIOS = {
    "1:1",
    "1:2",
    "2:1",
    "2:3",
    "3:2",
    "3:4",
    "4:3",
    "4:5",
    "5:4",
    "9:16",
    "16:9",
    "9:21",
    "21:9",
}
_RESOLUTIONS = {"1K", "1.5K", "2K", "4K"}


def _invalid(message: str) -> NativeError:
    return NativeError("invalid_request", message)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[1:-1]
    return value


def _integer(value: str, low: int, high: int, label: str) -> int:
    try:
        if not re.fullmatch(r"[0-9]+", value):
            raise ValueError
        number = int(value)
        if not low <= number <= high:
            raise ValueError
    except ValueError:
        raise _invalid(f"{label}必须是 {low} 至 {high} 的整数。") from None
    return number


def _value(field: str, value: str, *, animated: bool) -> str | int:
    value = _unquote(value)
    if field == "preset":
        value = value.strip()
        if not value or len(value) > 40 or re.search(r"[\[\]\r\n]", value):
            raise _invalid("预设名称不能为空、超过 40 字符或包含方括号及换行。")
        return value
    if field == "n":
        return _integer(value, 1, 4, "图片数量")
    if field == "aspect_ratio":
        if value not in _RATIOS:
            raise _invalid("不支持的图片比例；请使用 1:1、2:3、3:2、16:9 等常用比例。")
        return value
    if field == "resolution":
        value = value.upper()
        if value not in _RESOLUTIONS:
            raise _invalid("分辨率仅支持 1K、1.5K、2K 或 4K。")
        return value
    if field == "size":
        if value.lower() == "auto":
            return "auto"
        match = re.fullmatch(r"([0-9]+)[xX×]([0-9]+)", value)
        if match:
            try:
                width, height = map(int, match.groups())
                if width > 0 and height > 0:
                    return f"{width}x{height}"
            except ValueError:
                pass
        raise _invalid("尺寸必须是 auto 或正整数宽x高，例如 1024x1024。")
    if field == "output_format":
        value = value.lower()
        if animated:
            if value != "gif":
                raise _invalid("/生动图 仅支持 GIF；请移除 --格式 或设置 --格式 gif。")
        elif value == "gif":
            raise _invalid("生成 GIF 请使用 /生动图。")
        elif value not in {"png", "jpeg", "webp"}:
            raise _invalid("静态图片格式仅支持 png、jpeg 或 webp。")
        return value
    if not animated:
        raise _invalid("--帧数 和 --帧时长 仅适用于 /生动图。")
    if field == "frame_count":
        return _integer(value, 2, 16, "动图帧数")
    duration = _integer(value, 20, 2000, "每帧时长（毫秒）")
    # Match the service's nearest-10ms rounding, with halves rounded up.
    return (duration + 5) // 10 * 10


def _shorthand(value: str) -> str | None:
    if re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", value):
        return "n"
    if re.fullmatch(r"[0-9]+:[0-9]+", value):
        return "aspect_ratio"
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?[kK]", value):
        return "resolution"
    return None


async def quoted_prompt_from_event(event) -> str | None:
    """Read text only from an explicitly quoted message, using AstrBot's resolver."""
    reply = next(
        (
            part
            for part in (event.get_messages() if hasattr(event, "get_messages") else [])
            if type(part).__name__ == "Reply"
        ),
        None,
    )
    if reply is None:
        return None
    from astrbot.core.utils.quoted_message import extract_quoted_message_text

    return await extract_quoted_message_text(event, reply_component=reply)


def parse_image_command(
    text: str, *, animated: bool = False, quoted_text: str | None = None
) -> ImageRequest:
    """Parse text after /生图 or /生动图; leave model limits to the service.

    Explicit options accept ``--参数 值`` or ``--参数=值`` anywhere outside
    quoted text. Shorthand values must form a contiguous suffix in the original
    input; an explicit option ends that suffix. Quotes in the prompt are kept.
    Natural 比例/分辨率 pairs are recognized only in the argument suffix. A
    supplied quote is used only when the command has no prompt of its own.
    Static output defaults to png; animated output is always gif.
    """
    if not isinstance(text, str):
        raise _invalid("请输入图片提示词。")
    tokens = list(_TOKENS.finditer(text))
    removed: set[int] = set()
    options: dict[str, str | int] = {}

    def add(field: str, value: str) -> None:
        if field in options:
            label = next(name for name, target in _OPTIONS.items() if target == field)
            raise _invalid(f"参数 {label} 重复或冲突，请只指定一次。")
        options[field] = _value(field, value, animated=animated)

    for index, token in enumerate(tokens):
        if index in removed:
            continue
        raw = token.group()
        if not raw.startswith("--"):
            continue
        name, separator, value = raw.partition("=")
        if name not in _OPTIONS:
            raise _invalid(f"未知参数：{name}。")
        removed.add(index)
        if not separator:
            if index + 1 >= len(tokens) or tokens[index + 1].group().startswith("--"):
                raise _invalid(f"参数 {name} 缺少取值。")
            value = tokens[index + 1].group()
            removed.add(index + 1)
        if not value or not _unquote(value):
            raise _invalid(f"参数 {name} 缺少取值。")
        add(_OPTIONS[name], value)

    bare_suffix = True
    for index in range(len(tokens) - 1, -1, -1):
        if index in removed:
            bare_suffix = False
            continue
        field = _shorthand(tokens[index].group())
        if (
            field is not None
            and index > 0
            and index - 1 not in removed
            and _NATURAL_OPTIONS.get(tokens[index - 1].group()) == field
        ):
            add(field, tokens[index].group())
            removed.update((index - 1, index))
            bare_suffix = True
        elif field is not None and bare_suffix:
            add(field, tokens[index].group())
            removed.add(index)
        else:
            break

    if options.get("size", "auto") != "auto" and ("aspect_ratio" in options or "resolution" in options):
        raise _invalid("--尺寸 的宽高不能与 --比例 或 --分辨率 同时使用；请选择一种方式。")

    # Delete only argument spans, retaining all whitespace inside prompt spans.
    parts, cursor = [], 0
    for index in sorted(removed):
        token = tokens[index]
        parts.append(text[cursor : token.start()])
        cursor = token.end()
    parts.append(text[cursor:])
    prompt = "".join(parts).strip()
    if not prompt and isinstance(quoted_text, str):
        prompt = quoted_text.strip()
    preset = options.pop("preset", None)
    if preset:
        prompt = f"[预设:{preset}]" + (f" {prompt}" if prompt else "")
    if not prompt or not _unquote(prompt).strip():
        raise _invalid("请输入图片提示词。")
    if len(prompt) > 32000:
        raise _invalid("图片提示词不能超过 32000 个字符。")
    options.setdefault("output_format", "gif" if animated else "png")
    return ImageRequest(prompt=prompt, **options)
