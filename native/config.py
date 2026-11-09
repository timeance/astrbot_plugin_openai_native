from __future__ import annotations

import math
from copy import deepcopy
from typing import Any
from zoneinfo import ZoneInfo

FEATURE = {
    "enabled": True,
    "mode": "current",
    "provider_id": "",
    "base_url": "",
    "keys": [],
    "model": "",
    "host_model": "",
    "protocol": "auto",
    "service_type": "auto",
    "proxy": "",
    "timeout": 0,
    "headers": {},
    "extra_body": {},
    "paths": {},
}
DEFAULTS = {
    "search": {**FEATURE, "max_queries": 4, "max_results": 10, "default_results": 5, "max_tool_calls": 4},
    "image": {
        **FEATURE,
        "max_outputs": 4,
        "max_references": 16,
        "formats": ["png", "jpeg", "webp", "gif"],
        "sizes": [],
        "max_pixels": 16777216,
        "max_frames_pixels": 67108864,
        "default_format": "png",
        "default_size": "auto",
    },
    "gif": {"enabled": False, "frame_count": 9, "frame_duration_ms": 200},
    "limits": {
        "timezone": "UTC",
        "user_search": 20,
        "group_search": 200,
        "private_search": 20,
        "user_image": 3,
        "group_image": 30,
        "private_image": 3,
        "admin_exempt": False,
        "search_concurrency": 4,
        "image_concurrency": 2,
        "queue_size": 20,
        "user_image_concurrency": 1,
        "min_interval_seconds": 2,
        "search_calls_per_message": 3,
    },
    "storage": {
        "reference_ttl_seconds": 600,
        "artifact_days": 7,
        "task_days": 30,
        "max_input_bytes": 20971520,
        "max_output_bytes": 104857600,
        "max_total_bytes": 2147483648,
    },
    "network": {
        "connect_timeout": 10,
        "download_timeout": 60,
        "allow_private_download_hosts": [],
        "download_attempts": 3,
    },
}


def _merge(base: dict, values: dict) -> dict:
    result = deepcopy(base)
    for key, value in values.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


class Settings:
    def __init__(self, values: dict[str, Any] | None = None):
        self.data = _merge(DEFAULTS, dict(values or {}))
        try:
            if any(not isinstance(self.data[name], dict) for name in DEFAULTS):
                raise ValueError("section")
            ZoneInfo(self.limits["timezone"])
            positive = {
                "limits": (
                    "search_concurrency",
                    "image_concurrency",
                    "queue_size",
                    "user_image_concurrency",
                    "search_calls_per_message",
                ),
                "search": ("max_queries", "max_results", "default_results", "max_tool_calls"),
                "image": ("max_outputs", "max_references", "max_pixels", "max_frames_pixels"),
                "storage": (
                    "reference_ttl_seconds",
                    "artifact_days",
                    "task_days",
                    "max_input_bytes",
                    "max_output_bytes",
                    "max_total_bytes",
                ),
                "network": ("download_attempts",),
            }
            for section, names in positive.items():
                for name in names:
                    value = self.data[section][name]
                    if type(value) is not int or value < 1:
                        raise ValueError(name)
            for name in (
                "user_search",
                "group_search",
                "private_search",
                "user_image",
                "group_image",
                "private_image",
            ):
                if type(self.limits[name]) is not int or self.limits[name] < -1:
                    raise ValueError(name)
            for value in (
                self.limits["min_interval_seconds"],
                self.network["connect_timeout"],
                self.network["download_timeout"],
            ):
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ValueError("timing")
            if (
                self.data["search"]["default_results"] > self.data["search"]["max_results"]
                or self.data["search"]["max_queries"] > 4
                or self.data["search"]["max_results"] > 10
                or self.data["image"]["max_outputs"] > 4
            ):
                raise ValueError("request bounds")
            if self.storage["task_days"] < self.storage["artifact_days"]:
                raise ValueError("retention")
            formats = self.data["image"]["formats"]
            if not isinstance(formats, list) or any(
                fmt not in ("png", "jpeg", "webp", "gif") for fmt in formats
            ):
                raise ValueError("formats")
        except (ValueError, TypeError, KeyError) as exc:
            from .models import NativeError

            raise NativeError("configuration", "Invalid plugin configuration or timezone") from exc

    def feature(self, name: str) -> dict[str, Any]:
        return self.data[name]

    @property
    def limits(self) -> dict[str, Any]:
        return self.data["limits"]

    @property
    def storage(self) -> dict[str, Any]:
        return self.data["storage"]

    @property
    def network(self) -> dict[str, Any]:
        return self.data["network"]

    @property
    def gif(self) -> dict[str, Any]:
        return self.data["gif"]
