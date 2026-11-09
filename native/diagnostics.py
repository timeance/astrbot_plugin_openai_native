# SPDX-License-Identifier: Apache-2.0
"""Bounded execution evidence. Never retain prompts, request bodies or credentials."""

from __future__ import annotations

import time
import uuid
from collections import deque
from copy import deepcopy

from .capabilities import _redact, _secrets

CORRELATION = "openai_native.diagnostic_id"


def connection_info(connection):
    return {
        name: getattr(connection, name)
        for name in ("provider_id", "model", "host_model", "protocol", "service_type")
    }


def safe_error(error, connection=None):
    details = error.to_dict() if hasattr(error, "to_dict") else dict(error)
    secrets = _secrets(connection) if connection else set()
    return {
        name: (
            _redact(value, secrets, 1000 if name == "message" else 200) if isinstance(value, str) else value
        )
        for name, value in details.items()
        if name
        in {
            "category",
            "message",
            "status_code",
            "upstream_type",
            "upstream_code",
            "request_id",
            "ambiguous",
            "operation_id",
        }
    }


class Diagnostics:
    def __init__(self, capacity=200):
        self.records = deque(maxlen=capacity)

    def correlation(self, event):
        getter, setter = getattr(event, "get_extra", None), getattr(event, "set_extra", None)
        value = getter(CORRELATION) if callable(getter) else None
        if not value:
            value = uuid.uuid4().hex[:16]
            if callable(setter):
                setter(CORRELATION, value)
        return value

    def add(self, event=None, **record):
        row = {"time": time.time(), **record}
        if event is not None:
            row["correlation_id"] = self.correlation(event)
        self.records.append(deepcopy(row))
        return row

    def snapshot(self):
        return deepcopy(list(reversed(self.records)))
