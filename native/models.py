from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


class NativeError(Exception):
    def __init__(
        self,
        category: str,
        message: str,
        *,
        ambiguous: bool = False,
        status_code: int | None = None,
        upstream_type: str | None = None,
        upstream_code: str | None = None,
        request_id: str | None = None,
    ):
        super().__init__(message)
        self.category, self.message, self.ambiguous = category, message, ambiguous
        self.status_code, self.upstream_type = status_code, upstream_type
        self.upstream_code, self.request_id = upstream_code, request_id

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in vars(self).items() if v is not None}


@dataclass(frozen=True)
class Actor:
    platform_id: str
    bot_id: str
    sender_id: str
    umo: str
    group_id: str = ""
    is_admin: bool = False
    message_id: str = ""

    @property
    def owner_key(self) -> str:
        return json.dumps([self.platform_id, self.bot_id, self.sender_id, self.umo], ensure_ascii=False)

    @property
    def user_key(self) -> str:
        return json.dumps(
            [self.platform_id, self.bot_id, self.sender_id, "group" if self.group_id else "private"],
            ensure_ascii=False,
        )

    @property
    def group_key(self) -> str:
        return json.dumps([self.platform_id, self.bot_id, self.group_id], ensure_ascii=False)


@dataclass(frozen=True)
class Connection:
    provider_id: str
    base_url: str
    key: str = field(repr=False)
    model: str = ""
    host_model: str = ""
    protocol: str = "auto"
    service_type: str = "unknown"
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    extra_body: dict[str, Any] = field(default_factory=dict, repr=False)
    proxy: str | None = field(default=None, repr=False)
    timeout: float = 120
    paths: dict[str, str] = field(default_factory=dict)
    fingerprint: str = ""


@dataclass(frozen=True)
class SearchRequest:
    queries: list[str]
    max_results: int = 5
    recency: int | None = None
    domains: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str | None = None
    citation_id: str | None = None
    provenance: str = "source"


@dataclass(frozen=True)
class SearchResult:
    operation_id: str
    queries: list[str]
    sources: list[SearchHit]
    backend_synthesis: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    status: str = "succeeded"
    untrusted: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InputAsset:
    asset_id: str
    path: str
    mime: str
    sha256: str
    width: int
    height: int


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    action: str = "auto"
    references: list[Any] = field(default_factory=list)
    n: int = 1
    output_format: str = ""
    size: str = ""
    aspect_ratio: str = ""
    resolution: str = ""
    quality: str = "auto"
    complexity: str = ""
    frame_count: int | None = None
    frame_duration_ms: int | None = None


@dataclass(frozen=True)
class RemoteImage:
    data: bytes | None = field(default=None, repr=False)
    url: str | None = field(default=None, repr=False)
    output_format: str = "png"
    revised_prompt: str | None = None


@dataclass(frozen=True)
class ImageBatch:
    outputs: list[RemoteImage]
    response_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    path: str
    mime: str
    size_bytes: int
    sha256: str
    width: int
    height: int
    frame_count: int = 1
    frame_duration_ms: int | None = None


@dataclass(frozen=True)
class TaskHandle:
    task_id: str
    status: str
    accepted: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
