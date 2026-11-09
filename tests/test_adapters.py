# SPDX-License-Identifier: Apache-2.0
"""Native adapter contracts with synthetic responses, never a paid API."""

import base64
import io
from dataclasses import replace
from pathlib import Path

import pytest

from native.adapters import ImageAdapter, SearchAdapter
from native.config import Settings
from native.models import Connection, ImageRequest, InputAsset, NativeError, SearchRequest


def connection(**kwargs):
    return replace(
        Connection("test", "https://api.example/v1", "test-key", model="image-model", protocol="images"),
        **kwargs,
    )


def image_request(**kwargs):
    return replace(ImageRequest("draw a fox", output_format="png", size="auto"), **kwargs)


class RecordingTransport:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    async def post(self, conn, route, body, files=None):
        self.calls.append((conn, route, body, files))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    async def download(self, *args, **kwargs):
        pytest.fail("Adapters must return receipts before any result download")


@pytest.fixture
def assets(monkeypatch):
    monkeypatch.setattr(
        Path, "open", lambda path, *a, **kw: io.BytesIO(b"first" if path.name == "one.png" else b"second")
    )
    return [
        InputAsset("one", "one.png", "image/png", "hash1", 32, 32),
        InputAsset("two", "two.webp", "image/webp", "hash2", 32, 32),
    ]


async def test_alpha_request_and_structured_sources_are_preserved():
    transport = RecordingTransport(
        {
            "output": "Backend synthesis with https://fabricated.example/ignored",
            "results": {
                "query-one": {
                    "results": [
                        {
                            "title": "Source",
                            "url": "https://source.example/a",
                            "snippet": "Evidence",
                            "ref_id": "ref1",
                        },
                        {"title": "duplicate", "url": "https://source.example/a"},
                    ]
                }
            },
            "usage": {"total_tokens": 12},
        }
    )
    conn = connection(
        protocol="alpha",
        model="search-model",
        extra_body={"commands": {"bad": True}, "model": "wrong", "response_length": "wrong"},
    )
    request = SearchRequest(["query one", "query two"], max_results=3, recency=7, domains=["example.org"])
    result = await SearchAdapter(transport, Settings()).search(conn, request, "operation-1")
    _, route, body, files = transport.calls[0]
    assert route == "alpha_search" and files is None and len(transport.calls) == 1
    assert body == {
        "id": "operation-1",
        "model": "search-model",
        "commands": {
            "search_query": [
                {"q": "query one", "recency": 7, "domains": ["example.org"]},
                {"q": "query two", "recency": 7, "domains": ["example.org"]},
            ],
            "response_length": "short",
        },
    }
    assert result.operation_id == "operation-1" and result.queries == request.queries
    assert result.untrusted and result.status == "succeeded"
    assert len(result.sources) == 1
    hit = result.sources[0]
    assert (hit.url, hit.title, hit.snippet, hit.citation_id, hit.provenance) == (
        "https://source.example/a",
        "Source",
        "Evidence",
        "ref1",
        "source",
    )
    assert result.usage == {"total_tokens": 12}


@pytest.mark.parametrize("results", [None, []])
async def test_alpha_native_output_extracts_reference_headers_without_structured_results(results):
    payload = {"output": "Native title (https://source.example/a)\n【turn0search0】 native result"}
    if results is not None:
        payload["results"] = results
    transport = RecordingTransport(payload)
    result = await SearchAdapter(transport, Settings()).search(
        connection(protocol="alpha"), SearchRequest(["query"]), "op"
    )
    assert result.status == "succeeded" and result.untrusted
    assert len(result.sources) == 1
    hit = result.sources[0]
    assert (hit.title, hit.url, hit.citation_id, hit.provenance) == (
        "Native title",
        "https://source.example/a",
        "turn0search0",
        "native_tool_output",
    )
    assert result.backend_synthesis == "[native Alpha tool output; untrusted]\n" + payload["output"]
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"output": [{"text": "https://invented.example/a"}], "results": []},
        {"results": []},
        {"output": "native output", "results": "invalid"},
    ],
)
async def test_alpha_rejects_invalid_native_response_shapes(payload):
    transport = RecordingTransport(payload)
    with pytest.raises(NativeError) as caught:
        await SearchAdapter(transport, Settings()).search(
            connection(protocol="alpha"), SearchRequest(["query"]), "op"
        )
    assert caught.value.category == "protocol_error" and caught.value.ambiguous


@pytest.mark.parametrize("annotations", [False, True])
async def test_responses_search_forces_web_search_and_extracts_real_sources(annotations):
    source = {"url": "https://source.example/a", "title": "Source"}
    call = {"type": "web_search_call", "status": "completed", "action": {"type": "search"}}
    message = {"type": "message", "content": [{"type": "output_text", "text": "synthesis"}]}
    if annotations:
        message["content"][0]["annotations"] = [{"type": "url_citation", **source}]
    else:
        call["action"]["sources"] = [source]
    transport = RecordingTransport({"status": "completed", "output": [call, message]})
    conn = connection(
        protocol="responses",
        model="search-model",
        host_model="host-model",
        extra_body={"tool_choice": "none", "tools": [], "include": []},
    )
    result = await SearchAdapter(transport, Settings()).search(
        conn, SearchRequest(["query"], domains=["source.example"]), "op"
    )
    _, route, body, _ = transport.calls[0]
    assert route == "responses" and body["model"] == "search-model"
    assert body["tool_choice"] == {"type": "web_search"}
    assert body["include"] == ["web_search_call.action.sources"]
    assert body["tools"] == [{"type": "web_search", "filters": {"allowed_domains": ["source.example"]}}]
    assert result.sources[0].url == source["url"] and result.untrusted


@pytest.mark.parametrize(
    "output",
    [
        [{"type": "message", "content": [{"text": "https://invented.example/a"}]}],
        [
            {
                "type": "web_search_call",
                "status": "in_progress",
                "action": {"sources": [{"url": "https://source.example/a"}]},
            }
        ],
    ],
)
async def test_responses_search_requires_completed_search_evidence(output):
    transport = RecordingTransport({"status": "completed", "output": output})
    with pytest.raises(NativeError) as caught:
        await SearchAdapter(transport, Settings()).search(
            connection(protocol="responses"), SearchRequest(["query"]), "op"
        )
    assert caught.value.category == "protocol_error" and caught.value.ambiguous
    assert len(transport.calls) == 1


async def test_images_edit_passes_multiple_images_without_mask_or_result_download(assets):
    url = "https://cdn.example/result?signature=test"
    transport = RecordingTransport({"data": [{"url": url}], "id": "receipt-1", "usage": {"images": 1}})
    result = await ImageAdapter(transport, Settings()).generate(
        connection(extra_body={"mask": "not-a-supported-input"}), image_request(action="edit"), assets
    )
    _, route, body, files = transport.calls[0]
    assert route == "edits" and len(transport.calls) == 1
    assert [field for field, _ in files] == ["image[]", "image[]"]
    assert [item[1][1:] for item in files] == [(b"first", "image/png"), (b"second", "image/webp")]
    assert "mask" not in body and all(field != "mask" for field, _ in files)
    assert result.outputs[0].url == url and result.outputs[0].data is None
    assert result.response_id == "receipt-1" and result.usage == {"images": 1}


@pytest.mark.parametrize("edit", [False, True])
async def test_gif_parameters_come_from_normalized_per_call_request(edit, assets):
    transport = RecordingTransport({"data": [{"b64_json": base64.b64encode(b"gif-result").decode()}]})
    settings = Settings({"gif": {"enabled": True, "frame_count": 9, "frame_duration_ms": 200}})
    request = image_request(output_format="gif", size="512x256", frame_count=5, frame_duration_ms=130)
    conn = connection(
        extra_body={
            "frame_count": 15,
            "frame_duration_ms": 900,
            "output_format": "png",
            "stream": True,
            "partial_images": 3,
            "output_compression": 50,
        }
    )
    result = await ImageAdapter(transport, settings).generate(conn, request, assets if edit else [])
    _, route, body, files = transport.calls[0]
    assert route == ("edits" if edit else "generations")
    assert body["output_format"] == "gif" and body["size"] == "512x256"
    assert body["frame_count"] == 5 and body["frame_duration_ms"] == 130 and body["n"] == 1
    assert not {"stream", "partial_images", "output_compression"} & body.keys()
    assert (files is not None) == edit
    assert result.outputs[0].data == b"gif-result" and result.outputs[0].output_format == "gif"
    assert len(transport.calls) == 1


async def test_static_images_omit_stale_gif_fields_and_use_json():
    transport = RecordingTransport({"data": [{"b64_json": "aW1hZ2U="}]})
    result = await ImageAdapter(transport, Settings()).generate(
        connection(extra_body={"frame_count": 9, "frame_duration_ms": 200, "gif_size": "512x512"}),
        image_request(size="1025x769"),
        [],
    )
    _, route, body, files = transport.calls[0]
    assert route == "generations" and files is None
    assert not {"frame_count", "frame_duration_ms", "gif_size"} & body.keys()
    assert result.outputs[0].data == b"image"


async def test_responses_separates_host_and_image_models_with_multimodal_inputs(assets):
    transport = RecordingTransport(
        {
            "status": "completed",
            "id": "resp_1",
            "usage": {"total_tokens": 10},
            "output": [
                {
                    "type": "image_generation_call",
                    "status": "completed",
                    "result": "aW1hZ2U=",
                    "revised_prompt": "a fox",
                }
            ],
        }
    )
    result = await ImageAdapter(transport, Settings()).generate(
        connection(
            protocol="responses",
            host_model="language-host",
            model="image-tool",
            extra_body={"model": "wrong", "tools": [], "tool_choice": "none"},
        ),
        image_request(),
        assets,
    )
    _, route, body, _ = transport.calls[0]
    assert route == "responses" and body["model"] == "language-host"
    assert body["tools"][0]["model"] == "image-tool"
    assert body["tools"][0]["action"] == "edit"
    assert body["tool_choice"] == {"type": "image_generation"}
    content = body["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "draw a fox"}
    assert [entry["image_url"] for entry in content[1:]] == [
        "data:image/png;base64,Zmlyc3Q=",
        "data:image/webp;base64,c2Vjb25k",
    ]
    assert result.response_id == "resp_1" and result.outputs[0].data == b"image"
    assert result.outputs[0].revised_prompt == "a fox"


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "in_progress", "output": []},
        {"status": "completed", "output": [{"type": "image_generation_call", "result": "aW1hZ2U="}]},
        {
            "status": "completed",
            "output": [{"type": "image_generation_call", "status": "in_progress", "result": "aW1hZ2U="}],
        },
        {
            "status": "completed",
            "output": [{"type": "message", "content": [{"text": "https://cdn.example/a"}]}],
        },
        {
            "status": "completed",
            "output": [{"type": "image_generation_call", "status": "completed", "result": "broken base64"}],
        },
    ],
)
async def test_responses_image_requires_complete_tool_call(payload):
    transport = RecordingTransport(payload)
    with pytest.raises(NativeError) as caught:
        await ImageAdapter(transport, Settings()).generate(
            connection(protocol="responses", host_model="host"), image_request(), []
        )
    assert caught.value.category == "protocol_error" and caught.value.ambiguous
    assert len(transport.calls) == 1


async def test_responses_gif_rejected_before_paid_request():
    transport = RecordingTransport({})
    with pytest.raises(NativeError):
        await ImageAdapter(transport, Settings({"gif": {"enabled": True}})).generate(
            connection(protocol="responses", host_model="host"),
            image_request(output_format="gif", frame_count=9, frame_duration_ms=200),
            [],
        )
    assert not transport.calls


@pytest.mark.parametrize("feature", ["search", "image"])
async def test_upstream_ambiguous_error_is_not_retried_or_fallen_back(feature):
    error = NativeError("timeout", "Completion unknown", ambiguous=True)
    transport = RecordingTransport(error)
    with pytest.raises(NativeError) as caught:
        if feature == "search":
            await SearchAdapter(transport, Settings()).search(
                connection(protocol="alpha"), SearchRequest(["query"]), "op"
            )
        else:
            await ImageAdapter(transport, Settings()).generate(connection(), image_request(), [])
    assert caught.value is error and len(transport.calls) == 1


async def test_completed_web_search_with_no_sources_is_a_successful_empty_result():
    transport = RecordingTransport(
        {
            "status": "completed",
            "output": [
                {
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {"type": "search", "sources": []},
                },
                {"type": "message", "content": [{"type": "output_text", "text": "No matching results."}]},
            ],
        }
    )
    result = await SearchAdapter(transport, Settings()).search(
        connection(protocol="responses"), SearchRequest(["query"]), "op"
    )
    assert result.sources == [] and result.status == "succeeded" and result.untrusted
    assert result.backend_synthesis == "No matching results."
    assert len(transport.calls) == 1


@pytest.mark.parametrize("results", [None, []])
@pytest.mark.parametrize("output", ["", "No matching results.", "Prose https://invented.example/a"])
async def test_alpha_empty_sources_complete_without_promoting_prose_links(results, output):
    payload = {"output": output}
    if results is not None:
        payload["results"] = results
    transport = RecordingTransport(payload)
    result = await SearchAdapter(transport, Settings()).search(
        connection(protocol="alpha"), SearchRequest(["query"]), "op"
    )
    assert result.status == "succeeded" and result.sources == []
    assert result.backend_synthesis == "[native Alpha tool output; untrusted]\n" + output
    assert len(transport.calls) == 1


async def test_alpha_accepts_official_opaque_dto_example():
    # Shape from openai/codex endpoint/search.rs; unknown DTO fields are harmless.
    transport = RecordingTransport(
        {
            "output": "search result",
            "encrypted_output": "ciphertext",
            "results": [
                {
                    "type": "text_result",
                    "ref_id": "turn0search0",
                    "url": "https://example.com/result",
                    "future_field": {"preserved": True},
                }
            ],
        }
    )
    result = await SearchAdapter(transport, Settings()).search(
        connection(protocol="alpha"), SearchRequest(["query"], max_results=6), "op"
    )
    assert result.sources[0].url == "https://example.com/result"
    assert result.sources[0].citation_id == "turn0search0"
    assert transport.calls[0][2]["commands"]["response_length"] == "long"
    assert "ciphertext" not in str(result.to_dict())


@pytest.mark.parametrize("container", ["links", "citations"])
async def test_alpha_gateway_source_containers_preserve_reference_ids(container):
    transport = RecordingTransport(
        {
            "output": "native output",
            "results": [
                {
                    container: {
                        "turn2search3": {
                            "url": "https://source.example/a",
                            "title": "Record",
                            "snippet": "Evidence",
                        },
                        "turn2search4": {"url": "https://name:password@source.example/private"},
                    }
                }
            ],
        }
    )
    result = await SearchAdapter(transport, Settings()).search(
        connection(protocol="alpha"), SearchRequest(["query"]), "op"
    )
    assert len(result.sources) == 1
    assert result.sources[0].citation_id == "turn2search3"
    assert result.sources[0].snippet == "Evidence"


async def test_alpha_native_headers_deduplicate_and_respect_result_limit():
    header = "Source (https://source.example/a)\n【turn0search0】 [wordlim 200] Content\n"
    transport = RecordingTransport(
        {
            "output": header * 2
            + "Other (https://source.example/b)\n【turn0search1】 [wordlim 200] Other content",
            "results": [{"type": "text_result", "url": "https://source.example/a", "ref_id": "turn0search0"}],
        }
    )
    result = await SearchAdapter(transport, Settings()).search(
        connection(protocol="alpha"), SearchRequest(["query"], max_results=1), "op"
    )
    assert len(result.sources) == 1 and result.sources[0].provenance == "source"


async def test_responses_annotations_without_search_call_are_not_search_evidence():
    transport = RecordingTransport(
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "An answer",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://invented.example/a",
                                    "title": "Invented",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    with pytest.raises(NativeError, match="no completed web search evidence"):
        await SearchAdapter(transport, Settings()).search(
            connection(protocol="responses"), SearchRequest(["query"]), "op"
        )
    assert len(transport.calls) == 1


async def test_image_adapter_enforces_decoded_byte_limit():
    transport = RecordingTransport({"data": [{"b64_json": base64.b64encode(b"123456").decode()}]})
    with pytest.raises(NativeError, match="output byte limit") as caught:
        await ImageAdapter(transport, Settings({"storage": {"max_output_bytes": 5}})).generate(
            connection(), image_request(), []
        )
    assert caught.value.ambiguous and len(transport.calls) == 1
