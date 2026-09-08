"""Shared request-lowering helpers (OpenCode protocol parity).

Lowering is the only place that knows how harness messages map onto
provider wire formats. Mirrors (read-only references, do not edit):

- OpenCode ``ToolSchemaProjection.openAI`` (``tool-schema.ts:47-63``):
  top-level ``anyOf`` record variants are merged (first variant wins on
  key conflicts), ``type`` is forced to ``"object"``, the flattened
  branch always gets ``additionalProperties: false``, and ``null``
  variants are stripped via ``removeNullSchemas``.
- OpenCode ``ProviderShared.wrapSystemUpdate`` (``shared.ts:119-120``):
  chronological system updates become visible ``<system-update>`` user
  text with XML-escaped content.
- OpenCode ``BedrockMedia.lower`` MIME routing
  (``bedrock-media.ts:30-87``): known image/document MIMEs lower into a
  typed block; anything else fails with a clear error instead of
  silently degrading.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

from .base import ProviderResponseError

#: Image MIME → Bedrock Converse image format (bedrock-media.ts).
IMAGE_FORMATS: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}

#: MIME → Bedrock Converse document format (bedrock-media.ts).
DOCUMENT_FORMATS: dict[str, str] = {
    "application/pdf": "pdf",
    "text/csv": "csv",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "text/html": "html",
    "text/plain": "txt",
    "text/markdown": "md",
}


def _remove_null_schemas(value: Any) -> Any:
    """Recursively strip ``anyOf``/``null``-type schema variants.

    Mirrors ``removeNullSchemas`` in ``tool-schema.ts``: ``anyOf`` keys
    are removed everywhere; non-null ``anyOf`` variants are re-attached
    (a single remaining record variant is merged into the parent).
    """
    if isinstance(value, list):
        return [_remove_null_schemas(item) for item in value]
    if not isinstance(value, dict):
        return value
    fields = {
        key: _remove_null_schemas(field)
        for key, field in value.items()
        if key != "anyOf"
    }
    any_of = value.get("anyOf")
    if not isinstance(any_of, list):
        return fields
    variants = [
        _remove_null_schemas(variant)
        for variant in any_of
        if not isinstance(variant, dict) or variant.get("type") != "null"
    ]
    if len(variants) == 1 and isinstance(variants[0], dict):
        return {**fields, **variants[0]}
    return {**fields, "anyOf": variants}


def project_openai_tool_schema(
    parameters: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project a tool input schema onto the OpenAI wire shape.

    Mirrors ``ToolSchemaProjection.openAI`` (``tool-schema.ts:47-63``):
    top-level ``anyOf`` record variants are merged into ``properties``
    (first variant wins on key conflicts, matching the TS
    ``reduce`` order), ``type`` is forced to ``"object"``, and the
    flattened branch always sets ``additionalProperties: false``. The
    passthrough branch (no ``anyOf``) keeps the schema as-is plus
    ``type: "object"``. ``null`` variants are stripped afterwards.
    """
    schema = parameters if isinstance(parameters, dict) else {}
    raw_any_of = schema.get("anyOf")
    variants = (
        [variant for variant in raw_any_of if isinstance(variant, dict)]
        if isinstance(raw_any_of, list)
        else []
    )
    if not variants:
        flattened: dict[str, Any] = {**schema, "type": "object"}
    else:
        merged: dict[str, Any] = {}
        for variant in variants:
            properties = variant.get("properties")
            if isinstance(properties, dict):
                for key, field in properties.items():
                    # First variant wins (TS: {...variant.props, ...acc}).
                    merged.setdefault(key, field)
        flattened = {
            key: value for key, value in schema.items() if key != "anyOf"
        }
        flattened["type"] = "object"
        flattened["properties"] = merged
        flattened["additionalProperties"] = False
    normalized = _remove_null_schemas(flattened)
    return normalized if isinstance(normalized, dict) else {"type": "object"}


def escape_system_update(text: str) -> str:
    """XML-escape system-update text so it cannot close the wrapper."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def wrap_system_update(text: str) -> str:
    """Wrap a chronological system update as visible user text.

    Mirrors ``ProviderShared.wrapSystemUpdate`` (``shared.ts:119-120``).
    """
    return f"<system-update>\n{escape_system_update(text)}\n</system-update>"


_TOOL_ERROR_PATTERNS = (
    re.compile(r"^Error:"),
    # Runner emits "Permission {reason}: {title}" for denied tool calls
    # (runner.py ``_tool_error_outcome`` via ``_dispatch_tool_call``).
    re.compile(r"^Permission\s+\S[^:]*:"),
    # Runner emits "Unknown tool 'x': ..." for unknown tools.
    re.compile(r"^Unknown tool\b"),
    # Runner emits "Tool '{name}' failed: {exc}" for tool exceptions.
    re.compile(r"^Tool\s+'.+?'\s+failed:"),
)


def is_tool_error_text(text: str) -> bool:
    """Heuristically decide whether tool output text reports a failure.

    ``LLMMessage`` carries no error marker; the runner writes failure
    text (``Permission …:``, ``Unknown tool …``, ``Tool '…' failed:``)
    and success text (``result.output`` verbatim) into the same
    ``role="tool"`` channel. This matches those documented runner
    formats plus a generic ``Error:`` prefix. Best-effort by design:
    a successful tool result starting with one of these prefixes would
    be misclassified.
    """
    stripped = text.lstrip()
    if not stripped:
        return False
    return any(pattern.match(stripped) for pattern in _TOOL_ERROR_PATTERNS)


def _decode_data_url(url: str, *, provider: str) -> tuple[str, bytes]:
    """Split a ``data:{mime};base64,{payload}`` URL into MIME and bytes."""
    if not url.startswith("data:"):
        raise ProviderResponseError(
            "Bedrock Converse media must be a base64 data URL, "
            f"got {url[:48]!r}",
            provider=provider,
        )
    header, separator, encoded = url.partition(",")
    if not separator or not encoded or ";base64" not in header:
        raise ProviderResponseError(
            "Bedrock Converse media must be a base64 data URL",
            provider=provider,
        )
    mime = header[len("data:") :].split(";")[0].strip().lower()
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProviderResponseError(
            "Bedrock Converse media data URL is not valid base64",
            provider=provider,
        ) from exc
    return mime, raw


def bedrock_media_block(
    part: dict[str, Any],
    *,
    provider: str = "amazon-bedrock",
) -> dict[str, Any] | None:
    """Lower a harness content part to a Bedrock Converse media block.

    Returns ``None`` for non-media parts (caller keeps them as text).
    Known image/document MIMEs become typed ``image``/``document``
    blocks; unknown image MIMEs (e.g. ``image/svg+xml``) and unknown
    media types raise :class:`ProviderResponseError` instead of being
    silently dropped (bedrock-media.ts parity).

    Supported shapes: ``image_url`` (OpenAI-style, incl. the
    ``images.py`` hydration output), ``input_image``, and explicit
    ``media``/``document``/``file`` parts with ``mediaType``/``mime``
    plus ``data``/``uri``/``url``.
    """
    if not isinstance(part, dict):
        return None
    part_type = part.get("type")
    url: Any = None
    explicit_mime: Any = None
    filename: Any = None
    if part_type == "image_url":
        ref = part.get("image_url")
        url = ref.get("url") if isinstance(ref, dict) else ref
    elif part_type == "input_image":
        ref = part.get("image_url", part.get("url", part.get("data")))
        url = ref.get("url") if isinstance(ref, dict) else ref
    elif part_type in ("media", "document", "file", "image"):
        explicit_mime = part.get("mediaType", part.get("mime"))
        url = part.get("data", part.get("uri", part.get("url")))
        filename = part.get("filename", part.get("name"))
    else:
        return None
    if not isinstance(url, str) or not url:
        raise ProviderResponseError(
            f"Bedrock Converse media part has no data (type={part_type!r})",
            provider=provider,
        )
    mime, raw = _decode_data_url(url, provider=provider)
    if isinstance(explicit_mime, str) and explicit_mime.strip():
        mime = explicit_mime.strip().lower()
    image_format = IMAGE_FORMATS.get(mime)
    if image_format is not None:
        return {
            "image": {"format": image_format, "source": {"bytes": raw}},
        }
    if mime.startswith("image/"):
        raise ProviderResponseError(
            f"Bedrock Converse does not support image media type {mime}",
            provider=provider,
        )
    document_format = DOCUMENT_FORMATS.get(mime)
    if document_format is not None:
        name = (
            filename
            if isinstance(filename, str) and filename
            else f"document.{document_format}"
        )
        return {
            "document": {
                "format": document_format,
                "name": name,
                "source": {"bytes": raw},
            },
        }
    raise ProviderResponseError(
        f"Bedrock Converse does not support media type {mime or 'unknown'}",
        provider=provider,
    )
