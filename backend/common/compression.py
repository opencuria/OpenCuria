"""Gzip for large JSON API responses.

Chat transcripts repeat the same keys thousands of times and shrink about
twelvefold. The bundled Caddy proxy does not compress, and a custom proxy may
not either, so the API compresses itself. Only buffered JSON is touched: file
downloads and app bundles stream through their own transports.
"""

from __future__ import annotations

import gzip
import re

from django.http import HttpRequest, HttpResponse
from django.utils.cache import patch_vary_headers
from django.utils.deprecation import MiddlewareMixin

#: Smaller bodies cost more to compress than they save on the wire.
MIN_COMPRESSED_BYTES = 1024
#: Level 5 is nearly as small as 9 on JSON, at roughly a third of the CPU time.
COMPRESSION_LEVEL = 5

_ACCEPTS_GZIP = re.compile(r"\bgzip\b(?!\s*;\s*q=0(?:\.0*)?\s*(?:,|$))")


class JsonGzipMiddleware(MiddlewareMixin):
    """Compress buffered ``application/json`` responses for gzip-capable clients."""

    def process_response(
        self, request: HttpRequest, response: HttpResponse
    ) -> HttpResponse:
        """Return *response*, gzip-encoded when that is allowed and smaller."""
        if (
            response.streaming
            or response.has_header("Content-Encoding")
            or not response.get("Content-Type", "").startswith("application/json")
            or len(response.content) < MIN_COMPRESSED_BYTES
        ):
            return response
        patch_vary_headers(response, ("Accept-Encoding",))
        if not _ACCEPTS_GZIP.search(request.META.get("HTTP_ACCEPT_ENCODING", "")):
            return response
        compressed = gzip.compress(
            response.content, compresslevel=COMPRESSION_LEVEL, mtime=0
        )
        if len(compressed) >= len(response.content):
            return response
        response.content = compressed
        response.headers["Content-Length"] = str(len(compressed))
        response.headers["Content-Encoding"] = "gzip"
        etag = response.get("ETag")
        if etag and etag.startswith('"'):
            response.headers["ETag"] = f"W/{etag}"
        return response
