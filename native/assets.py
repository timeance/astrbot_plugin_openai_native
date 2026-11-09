from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import time
import uuid
import warnings
from pathlib import Path

from PIL import Image

from .models import Artifact, InputAsset, NativeError

MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}
EXT = {"image/png": "png", "image/jpeg": "jpeg", "image/webp": "webp", "image/gif": "gif"}


def atomic_write(path: Path, data: bytes):
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class AssetStore:
    def __init__(self, root, settings, transport, store):
        self.root = Path(root).resolve()
        self.settings, self.transport, self.store = settings, transport, store
        self.lock = asyncio.Lock()
        for name in ("references", "artifacts", "receipts"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def inspect(self, data: bytes, *, reference=False, expected=None):
        limit = self.settings.storage["max_input_bytes" if reference else "max_output_bytes"]
        if not data or len(data) > limit:
            raise NativeError("file_too_large", "Image is empty or exceeds the configured byte limit")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    mime = MIME.get(image.format)
                    width, height = image.size
                    frames = getattr(image, "n_frames", 1)
                    if not mime or width * height > self.settings.feature("image")["max_pixels"]:
                        raise NativeError(
                            "invalid_image", "Unsupported image format or excessive pixel count"
                        )
                    if frames * width * height > self.settings.feature("image")["max_frames_pixels"]:
                        raise NativeError("file_too_large", "Animation exceeds the configured pixel budget")
                    if reference and frames > 1:
                        raise NativeError(
                            "animated_input_unsupported",
                            "Animated image editing is outside this plugin's scope",
                        )
                    duration = image.info.get("duration") if frames > 1 else None
                    # Decode all frames before accepting; never re-encode the original bytes.
                    for frame in range(frames):
                        image.seek(frame)
                        image.load()
                if expected and EXT[mime] != expected:
                    raise NativeError("invalid_result", "Upstream returned a different image format")
                if expected == "gif" and frames < 2:
                    raise NativeError("invalid_result", "Upstream GIF result is not animated")
            return mime, width, height, frames, duration
        except NativeError:
            raise
        except Exception as exc:
            raise NativeError("invalid_image", "Image data is invalid or cannot be decoded") from exc

    async def _write(self, path, data):
        async with self.lock:

            def write():
                total = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
                if total + len(data) > self.settings.storage["max_total_bytes"]:
                    raise NativeError("storage_full", "Plugin storage limit reached")
                atomic_write(path, data)

            await asyncio.to_thread(write)

    async def import_data(self, actor, data):
        mime, width, height, _, _ = await asyncio.to_thread(self.inspect, data, reference=True)
        identity = uuid.uuid4().hex
        path = self.root / "references" / (identity + "." + EXT[mime])
        await self._write(path, data)
        asset = InputAsset(identity, str(path), mime, hashlib.sha256(data).hexdigest(), width, height)
        await self.store.asset_put(actor, asset, self.settings.storage["reference_ttl_seconds"])
        return asset

    async def _read_local(self, value):
        path = Path(value)
        if not path.is_file() or path.stat().st_size > self.settings.storage["max_input_bytes"]:
            raise NativeError("invalid_reference", "Reference file is missing or too large")
        return await asyncio.to_thread(path.read_bytes)

    async def explicit(self, actor, reference, *, allow_local_paths):
        if isinstance(reference, bytes):
            return await self.import_data(actor, reference)
        if isinstance(reference, InputAsset):
            saved = await self.store.asset_get(actor, reference.asset_id)
            if saved:
                return InputAsset(**saved)
            if allow_local_paths:
                return await self.import_data(actor, await self._read_local(reference.path))
            raise NativeError("invalid_reference", "Unknown or expired reference asset")
        if isinstance(reference, Path):
            if not allow_local_paths:
                raise NativeError("permission_denied", "Local paths are not accepted by LLM tools")
            return await self.import_data(actor, await self._read_local(reference))
        if not isinstance(reference, str):
            raise NativeError("invalid_reference", "Expected asset ID, URL, bytes or trusted Path")
        if reference.startswith(("https://", "http://")):
            data = await self.transport.download(reference, self.settings.storage["max_input_bytes"])
            return await self.import_data(actor, data)
        saved = await self.store.asset_get(actor, reference)
        if saved:
            return InputAsset(**saved)
        if allow_local_paths and Path(reference).is_absolute():
            return await self.import_data(actor, await self._read_local(reference))
        raise NativeError(
            "invalid_reference", "Reference asset is unknown, expired or owned by another conversation"
        )

    async def _quoted_images(self, event, reply):
        from astrbot.core.utils.quoted_message import extract_quoted_message_images

        return await extract_quoted_message_images(event, reply_component=reply)

    async def from_event(self, event, actor, *, current_only=False):
        result, quoted_refs = [], []
        components = event.get_messages() if hasattr(event, "get_messages") else []
        # Only inspect the actual event and its explicit reply chain, never group history.
        for component in components:
            kind = type(component).__name__
            if kind == "Reply" and not current_only:
                embedded = [
                    q for q in (getattr(component, "chain", None) or []) if type(q).__name__ == "Image"
                ]
                if embedded:
                    result.extend(embedded)
                else:
                    quoted_refs.extend(await self._quoted_images(event, component))
            elif kind == "Image":
                result.append(component)
        if len(result) + len(quoted_refs) > self.settings.feature("image")["max_references"]:
            raise NativeError("invalid_reference", "Too many reference images")
        assets = []
        for component in result:
            # Framework adapters resolve trusted local attachment URLs and platform downloads.
            if hasattr(component, "convert_to_file_path"):
                value = component.convert_to_file_path()
                if hasattr(value, "__await__"):
                    value = await value
                assets.append(await self.import_data(actor, await self._read_local(value)))
            else:
                value = getattr(component, "url", None) or getattr(component, "file", None)
                if not value:
                    raise NativeError("invalid_reference", "Unable to resolve attachment")
                if str(value).startswith(("http://", "https://")):
                    assets.append(await self.explicit(actor, value, allow_local_paths=False))
                else:
                    # Paths here originate from framework message components, not LLM parameters.
                    assets.append(
                        await self.import_data(
                            actor, await self._read_local(str(value).removeprefix("file:///"))
                        )
                    )
        for value in quoted_refs:
            if value.startswith(("http://", "https://")):
                assets.append(await self.explicit(actor, value, allow_local_paths=False))
            else:
                assets.append(await self.import_data(actor, await self._read_local(value)))
        return assets

    async def prepare(self, event, actor, request, *, allow_local_paths=False):
        if request.references:
            assets = [
                await self.explicit(actor, ref, allow_local_paths=allow_local_paths)
                for ref in request.references
            ]
        elif request.action == "generate":
            assets = []
        else:
            assets = await self.from_event(event, actor)
            if not assets:
                saved = await self.store.asset_get(actor)
                assets = [InputAsset(**saved)] if saved else []
        # Repeated images do not consume separate reference positions.
        unique = list({a.sha256: a for a in assets}.values())
        if len(unique) > self.settings.feature("image")["max_references"]:
            raise NativeError("invalid_request", "Too many reference images")
        if request.action == "edit" and not unique:
            raise NativeError("invalid_reference", "Image editing requires a reference image")
        return unique

    async def receipt(self, operation_id, index, batch):
        outputs = []
        for number, output in enumerate(batch.outputs):
            item = {"output_format": output.output_format}
            if output.data is not None:
                path = self.root / "receipts" / f"{operation_id}-{index}-{number}.bin"
                await self._write(path, output.data)
                item["spool_path"] = str(path)
            elif output.url:
                item["url"] = output.url
            else:
                raise NativeError("invalid_result", "Image output has no data or URL", ambiguous=True)
            outputs.append(item)
        receipt = {"index": index, "response_id": batch.response_id, "outputs": outputs, "usage": batch.usage}
        path = self.root / "receipts" / f"{operation_id}-{index}.json"
        await self._write(path, json.dumps(receipt, ensure_ascii=False).encode())
        return receipt

    async def discover_receipts(self, operation_id):
        receipts = []
        for path in sorted((self.root / "receipts").glob(operation_id + "-*.json")):
            try:
                receipts.append(json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return receipts

    async def materialize(self, operation_id, receipt_index, output_index, item, expected):
        identity = f"{operation_id}-{receipt_index}-{output_index}"
        if "spool_path" in item:
            path = Path(item["spool_path"]).resolve()
            if not path.is_relative_to(self.root / "receipts"):
                raise NativeError("invalid_result", "Invalid saved response location")
            data = await asyncio.to_thread(path.read_bytes)
        else:
            data = await self.transport.download(item["url"], self.settings.storage["max_output_bytes"])
        mime, width, height, frames, duration = await asyncio.to_thread(self.inspect, data, expected=expected)
        path = self.root / "artifacts" / (identity + "." + EXT[mime])
        if not path.exists():
            await self._write(path, data)
        return Artifact(
            identity,
            str(path),
            mime,
            len(data),
            hashlib.sha256(data).hexdigest(),
            width,
            height,
            frames,
            duration,
        )

    async def remember_result(self, actor, artifact):
        if artifact.frame_count > 1:
            return
        asset = InputAsset(
            artifact.artifact_id,
            artifact.path,
            artifact.mime,
            artifact.sha256,
            artifact.width,
            artifact.height,
        )
        await self.store.asset_put(actor, asset, self.settings.storage["reference_ttl_seconds"])

    async def cleanup(self):
        for op in await self.store.expired():
            for item in op["artifacts"]:
                path = Path(item["path"]).resolve()
                if path.is_relative_to(self.root / "artifacts"):
                    path.unlink(missing_ok=True)
            for path in (self.root / "receipts").glob(op["id"] + "-*"):
                if path.is_file():
                    path.unlink(missing_ok=True)
            if op["updated"] < time.time() - self.settings.storage["task_days"] * 86400:
                await self.store.purge(op["id"])
        # Active task references survive TTL; cache expiry is separate from file retention.
        protected = set()
        for op in await self.store.list_recovery():
            protected.update(r["path"] for r in op["request"].get("assets", []))
        cutoff = time.time() - self.settings.storage["reference_ttl_seconds"]
        for path in (self.root / "references").iterdir():
            if path.is_file() and str(path) not in protected and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
