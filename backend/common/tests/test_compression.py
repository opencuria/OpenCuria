"""JSON gzip: only large buffered JSON for clients that accept it."""

from __future__ import annotations

import gzip
import json

import pytest
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.test import RequestFactory

from common.compression import MIN_COMPRESSED_BYTES, JsonGzipMiddleware

BODY = {"rows": [{"key": "value", "n": index} for index in range(400)]}


def _run(response: HttpResponse, accept: str | None = "gzip, deflate"):
    extra = {} if accept is None else {"HTTP_ACCEPT_ENCODING": accept}
    request = RequestFactory().get("/", **extra)
    return JsonGzipMiddleware(lambda _request: response)(request)


def test_large_json_is_compressed_and_round_trips() -> None:
    plain = JsonResponse(BODY)
    size = len(plain.content)
    assert size > MIN_COMPRESSED_BYTES
    response = _run(plain)
    assert response["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in response["Vary"]
    assert int(response["Content-Length"]) == len(response.content) < size // 4
    assert json.loads(gzip.decompress(response.content)) == BODY


@pytest.mark.parametrize("accept", [None, "", "identity", "br", "gzip;q=0"])
def test_clients_without_gzip_get_the_plain_body(accept: str | None) -> None:
    response = _run(JsonResponse(BODY), accept)
    assert not response.has_header("Content-Encoding")
    assert json.loads(response.content) == BODY
    assert "Accept-Encoding" in response["Vary"]


def test_small_json_is_left_alone() -> None:
    response = _run(JsonResponse({"ok": True}))
    assert not response.has_header("Content-Encoding")
    assert not response.has_header("Vary")


def test_streams_other_types_and_encoded_bodies_are_left_alone() -> None:
    stream = StreamingHttpResponse(iter([b"x" * 4096]), content_type="application/json")
    assert not _run(stream).has_header("Content-Encoding")
    text = HttpResponse("x" * 4096, content_type="text/plain")
    assert not _run(text).has_header("Content-Encoding")
    encoded = JsonResponse(BODY)
    encoded["Content-Encoding"] = "br"
    assert _run(encoded)["Content-Encoding"] == "br"


def test_strong_etag_becomes_weak() -> None:
    response = JsonResponse(BODY)
    response["ETag"] = '"abc"'
    assert _run(response)["ETag"] == 'W/"abc"'
