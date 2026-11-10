# SPDX-License-Identifier: Apache-2.0
"""Command parsing contracts; no model, shell, or network calls."""

import pytest

from native.commands import parse_image_command
from native.config import Settings
from native.models import ImageRequest, NativeError
from native.service import NativeService


def invalid(text, *, animated=False, message=None):
    with pytest.raises(NativeError) as caught:
        parse_image_command(text, animated=animated)
    error = caught.value
    assert error.category == "invalid_request"
    assert not error.ambiguous
    assert any("\u4e00" <= char <= "\u9fff" for char in error.message)
    if message:
        assert message in error.message


def test_defaults_keep_formats_separate_and_leave_other_options_to_service():
    assert parse_image_command("  柴犬  ") == ImageRequest(prompt="柴犬", output_format="png")
    assert parse_image_command("柴犬", animated=True) == ImageRequest(prompt="柴犬", output_format="gif")


@pytest.mark.parametrize(
    "prompt",
    [
        "3 只柴犬在 2 棵树下",
        "第 2026 年的第 3 场雪",
        "16:9 的显示器播放 2K 电影",
        "draw a dog's portrait",
        "It's a dog's life",
        "'It's a dog --数量 99'",
        '海报写着 "hello world"',
        "'a quoted prompt'",
        'a dog says "hello',
        '标签 "3"',
        "标签 '2K'",
        '"--数量 9 --不存在 1"',
        "画面  中有\t两只\n小狗",
        r"a $(echo data); `whoami` C:\pictures\dog",
    ],
)
def test_prompt_remains_plain_text(prompt):
    assert parse_image_command(prompt).prompt == prompt


def test_explicit_options_support_both_assignment_forms():
    result = parse_image_command("柴犬 --数量 3 --比例=16:9 --分辨率 1.5k --格式=WEBP")
    assert result == ImageRequest(
        prompt="柴犬", n=3, aspect_ratio="16:9", resolution="1.5K", output_format="webp"
    )


def test_explicit_options_can_precede_or_split_prompt():
    result = parse_image_command("--数量 2 柴犬 --格式 webp 在草地上")
    assert result.n == 2 and result.output_format == "webp"
    assert result.prompt.split() == ["柴犬", "在草地上"]


def test_natural_chinese_ratio_and_resolution_suffix():
    assert parse_image_command("柴犬立绘 比例 1:1 分辨率 1k") == ImageRequest(
        prompt="柴犬立绘", aspect_ratio="1:1", resolution="1K", output_format="png"
    )
    result = parse_image_command("柴犬 比例 16:9 --格式 webp")
    assert result.prompt == "柴犬" and result.aspect_ratio == "16:9"
    assert result.output_format == "webp"


@pytest.mark.parametrize(
    "prompt",
    [
        "画出比例 1:1 的显示器",
        "分辨率 2K 的电影海报",
        "画面 比例 1:1 后面有文字",
        "标签 '比例 1:1'",
    ],
)
def test_natural_labels_in_prose_are_not_consumed(prompt):
    assert parse_image_command(prompt).prompt == prompt


def test_natural_values_reuse_explicit_validation_and_conflict_checks():
    invalid("柴犬 比例 8:1")
    invalid("柴犬 --比例 1:1 比例 16:9")
    invalid("柴犬 --尺寸 512x512 分辨率 2K")


def test_preset_option_prepends_marker_without_changing_other_options():
    assert parse_image_command("柴犬 --预设 QQ表情 比例 1:1 --数量 2") == ImageRequest(
        prompt="[预设:QQ表情] 柴犬", aspect_ratio="1:1", n=2, output_format="png"
    )
    assert parse_image_command('海报 --预设 "黑白 线稿"').prompt == "[预设:黑白 线稿] 海报"
    assert parse_image_command("--预设 QQ表情").prompt == "[预设:QQ表情]"
    invalid("柴犬 --预设 QQ表情 --预设 另一种")
    invalid("柴犬 --预设 '坏]名称'")


def test_quote_text_is_used_only_when_command_has_no_prompt():
    assert parse_image_command("--分辨率 2K", quoted_text="  引用中的猫咪  ").prompt == "引用中的猫咪"
    assert parse_image_command("新的猫咪", quoted_text="旧提示词").prompt == "新的猫咪"
    assert parse_image_command("--预设 QQ表情", quoted_text="困惑的猫咪").prompt == (
        "[预设:QQ表情] 困惑的猫咪"
    )
    invalid("--分辨率 2K", message="提示词")


@pytest.mark.parametrize("suffix", ["16:9 2K 3", "3 16:9 2k", "2K 3 16:9"])
def test_contiguous_trailing_shorthand(suffix):
    assert parse_image_command(f"柴犬 {suffix}") == ImageRequest(
        prompt="柴犬", n=3, aspect_ratio="16:9", resolution="2K", output_format="png"
    )


def test_shorthand_does_not_consume_numbers_before_explicit_options():
    result = parse_image_command("画面上写着 3 --格式 png")
    assert result.prompt == "画面上写着 3"
    assert result.n == 1
    result = parse_image_command("柴犬 16:9 文字 2K 3")
    assert result.prompt == "柴犬 16:9 文字"
    assert result.resolution == "2K" and result.n == 3 and not result.aspect_ratio


def test_shorthand_can_follow_explicit_options_without_reinterpreting_option_values():
    result = parse_image_command("柴犬 --数量 2 16:9 2K")
    assert result == ImageRequest(
        prompt="柴犬", n=2, aspect_ratio="16:9", resolution="2K", output_format="png"
    )


@pytest.mark.parametrize(
    "ratio", ["1:1", "1:2", "2:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "9:21", "21:9"]
)
def test_common_aspect_ratios(ratio):
    assert parse_image_command(f"柴犬 --比例 {ratio}").aspect_ratio == ratio


@pytest.mark.parametrize("resolution", ["1K", "1.5K", "2K", "4K"])
def test_resolution_presets(resolution):
    assert parse_image_command(f"柴犬 {resolution.lower()}").resolution == resolution


@pytest.mark.parametrize(
    "value,expected",
    [
        ("auto", "auto"),
        ("AUTO", "auto"),
        ("1024x768", "1024x768"),
        ("1024X768", "1024x768"),
        ("512×256", "512x256"),
        ("'512x256'", "512x256"),
    ],
)
def test_size_syntax(value, expected):
    assert parse_image_command(f"柴犬 --尺寸={value}").size == expected


def test_auto_size_can_be_combined_with_ratio_and_resolution():
    result = parse_image_command("柴犬 --尺寸 auto --比例 16:9 --分辨率 2K")
    assert result.size == "auto" and result.aspect_ratio == "16:9" and result.resolution == "2K"


@pytest.mark.parametrize("output_format", ["png", "jpeg", "webp"])
def test_static_formats(output_format):
    assert parse_image_command(f"柴犬 --格式 {output_format}").output_format == output_format


def test_quoted_argument_values_and_numeric_prompt():
    assert parse_image_command("'3' --数量 '2'") == ImageRequest(prompt="'3'", n=2, output_format="png")
    assert parse_image_command('柴犬 --比例 "16:9"').aspect_ratio == "16:9"


@pytest.mark.parametrize("frames", [2, 16])
def test_animated_frame_count_boundaries(frames):
    result = parse_image_command(f"柴犬 --帧数 {frames} --格式 GIF", animated=True)
    assert result.output_format == "gif" and result.frame_count == frames
    assert result.frame_duration_ms is None


@pytest.mark.parametrize(
    "value,expected", [(20, 20), (24, 20), (25, 30), (26, 30), (1999, 2000), (2000, 2000)]
)
def test_animated_duration_matches_service_quantization(value, expected):
    result = parse_image_command(f"柴犬 --帧时长 {value}", animated=True)
    assert result.output_format == "gif" and result.frame_duration_ms == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        " \n\t ",
        "''",
        '"  "',
        "--数量 2",
        "16:9 2K 3",
        "柴犬 --未知 2",
        "柴犬 --数量",
        "柴犬 --数量 --比例 1:1",
        "柴犬 --数量=",
        "柴犬 --数量 ''",
        "柴犬 --数量 0",
        "柴犬 --数量 5",
        "柴犬 --数量 -1",
        "柴犬 --数量 1.5",
        "柴犬 --数量 二",
        "柴犬 0",
        "柴犬 5",
        "柴犬 -1",
        "柴犬 1.5",
        "柴犬 --比例 8:1",
        "柴犬 --比例 0:1",
        "柴犬 --比例 16/9",
        "柴犬 99:1",
        "柴犬 --分辨率 8K",
        "柴犬 8K",
        "柴犬 1.7K",
        "柴犬 --尺寸 0x512",
        "柴犬 --尺寸 512x0",
        "柴犬 --尺寸 -1x512",
        "柴犬 --尺寸 512",
        "柴犬 --尺寸 1.5x512",
        "柴犬 --格式 jpg",
        "柴犬 --格式 bmp",
        "柴犬 --数量 2 --数量 2",
        "柴犬 --数量 2 3",
        "柴犬 2 3",
        "柴犬 --比例 1:1 --比例 1:1",
        "柴犬 --比例 1:1 16:9",
        "柴犬 1:1 16:9",
        "柴犬 --分辨率 2K 2K",
        "柴犬 --尺寸 auto --尺寸 auto",
        "柴犬 --格式 png --格式 png",
        "柴犬 --尺寸 1024x1024 --比例 1:1",
        "柴犬 --尺寸 1024x1024 2K",
        "柴犬 --帧数 2",
        "柴犬 --帧时长 200",
    ],
)
def test_invalid_static_requests_are_rejected_in_chinese(text):
    invalid(text)


def test_static_gif_explains_animated_command():
    invalid("柴犬 --格式 gif", message="/生动图")


@pytest.mark.parametrize(
    "text",
    [
        "柴犬 --格式 png",
        "柴犬 --格式 jpeg",
        "柴犬 --格式 webp",
        "柴犬 --帧数 1",
        "柴犬 --帧数 17",
        "柴犬 --帧数 2.5",
        "柴犬 --帧时长 19",
        "柴犬 --帧时长 2001",
        "柴犬 --帧时长 20.5",
        "柴犬 --帧数 2 --帧数 2",
        "柴犬 --帧时长 20 --帧时长 30",
        "柴犬 --格式 gif --格式 gif",
    ],
)
def test_invalid_animated_requests(text):
    invalid(text, animated=True)


def test_non_text_oversized_prompts_and_huge_integer_values_are_rejected():
    invalid(None)
    invalid("犬" * 32001)
    assert parse_image_command("犬" * 32000).prompt == "犬" * 32000
    invalid("柴犬 --数量 " + "9" * 5000)
    invalid("柴犬 --尺寸 " + "9" * 5000 + "x1")


def test_quantity_boundaries():
    assert parse_image_command("柴犬 1").n == 1
    assert parse_image_command("柴犬 --数量 4").n == 4


@pytest.mark.parametrize("text", ["柴犬", "柴犬 1.5K"])
def test_static_commands_remain_static_with_global_gif_default(text):
    service = NativeService.__new__(NativeService)
    service.settings = Settings({"image": {"default_format": "gif"}, "gif": {"enabled": False}})
    request = service._image_request(parse_image_command(text))
    assert request.output_format == "png"
    assert request.frame_count is None and request.frame_duration_ms is None
    if text.endswith("1.5K"):
        assert request.size == "1536x1536"


def test_animated_request_is_compatible_with_service_and_adapter_validation():
    service = NativeService.__new__(NativeService)
    service.settings = Settings({"gif": {"enabled": True}})
    request = service._image_request(
        parse_image_command("柴犬 --尺寸 512x256 --帧数 2 --帧时长 25", animated=True)
    )
    assert request.output_format == "gif" and request.frame_duration_ms == 30
    from native.adapters import ImageAdapter

    assert ImageAdapter(None, service.settings)._validate(request, []) == "generate"
