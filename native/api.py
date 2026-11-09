from __future__ import annotations

from .models import Artifact, ImageRequest, NativeError, SearchRequest, SearchResult, TaskHandle

__all__ = [
    "Artifact",
    "ImageRequest",
    "NativeError",
    "SearchRequest",
    "SearchResult",
    "TaskHandle",
    "PublicAPI",
]


class PublicAPI:
    """Versioned plugin API. It never enables platform delivery."""

    api_version = 1

    def __init__(self, service):
        self._service = service

    async def search(self, event, request: SearchRequest) -> SearchResult:
        return await self._service.search(event, request)

    async def submit_image(self, event, request: ImageRequest, idempotency_key=None) -> TaskHandle:
        return await self._service.submit_image(
            event, request, idempotency_key, deliver=False, allow_local_paths=True
        )

    async def get_task(self, event, task_id):
        return await self._service.get_task(event, task_id)

    async def wait_task(self, event, task_id, timeout=30):
        return await self._service.wait_task(event, task_id, timeout)

    async def get_results(self, event, task_id) -> list[Artifact]:
        return await self._service.get_results(event, task_id)

    async def cancel_task(self, event, task_id):
        return await self._service.cancel_task(event, task_id)

    async def build_tools(self, event):
        self._service._ready()
        if self._service.tools_callback is None:
            raise NativeError("unavailable", "AstrBot tool builder is not attached")
        return await self._service.tools_callback(event)
