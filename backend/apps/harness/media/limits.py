"""Shared ingestion, request and persisted-attachment budgets."""

MAX_MEDIA_INGEST_BYTES = 20 * 1024 * 1024
MAX_CLAUDE_IMAGE_BASE64_CHARS = 5_000_000
MAX_CLAUDE_DOCUMENT_BYTES = MAX_MEDIA_INGEST_BYTES
MAX_USER_IMAGE_BLOCKS = 16
MAX_USER_MEDIA_BLOCKS = 16
MAX_USER_CONTENT_BYTES = 12 * 1024 * 1024
MAX_CLAUDE_TOOL_ARGUMENT_BYTES = 64 * 1024

IMAGE_TOKEN_ESTIMATE = 1000

#: Max tool attachments persisted into ``HarnessPart.meta`` (no DB column).
#: Keeps restarts bounded: the read tool emits at most one attachment per
#: call, so two covers parallel rounds without bloating the JSON meta.
TOOL_ATTACHMENT_MAX_COUNT = 2

#: Max base64 data-URL chars kept per persisted attachment (~6 MB binary).
#: Oversized attachments are dropped (text output survives); base64 can
#: never be truncated without corrupting the image.
TOOL_ATTACHMENT_MAX_CHARS = 8_000_000
