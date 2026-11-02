"""Keep inline image bytes out of stored and restored Codex rollouts."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from ...media.tool_media import OMITTED_MEDIA_NOTICE

#: Image content item type -> text item type of the same rollout schema.
_TEXT_TYPES = {"input_image": "input_text", "image": "text", "inputImage": "inputText"}
_URL_KEYS = ("image_url", "imageUrl")
_MARKER = b"data:image/"


def _is_inline_image(value: dict[str, Any]) -> bool:
    """Match one rollout image item whose URL carries base64 bytes."""
    return value.get("type") in _TEXT_TYPES and any(
        isinstance(value.get(key), str) and value[key].startswith("data:image/")
        for key in _URL_KEYS
    )


def _strip(value: Any) -> Any:
    """Replace inline image items with a same-schema text notice."""
    if isinstance(value, list):
        return [_strip(item) for item in value]
    if not isinstance(value, dict):
        return value
    if _is_inline_image(value):
        return {"type": _TEXT_TYPES[value["type"]], "text": OMITTED_MEDIA_NOTICE}
    return {key: _strip(item) for key, item in value.items()}


def _strip_rollout(data: bytes) -> bytes:
    """Rewrite only rollout lines that carry inline images."""
    lines = []
    for line in data.split(b"\n"):
        rewritten = line
        if _MARKER in line:
            try:
                parsed = json.loads(line)
            except ValueError:
                parsed = None
            if parsed is not None:
                rewritten = json.dumps(
                    _strip(parsed), ensure_ascii=False, separators=(",", ":")
                ).encode()
        lines.append(rewritten)
    return b"\n".join(lines)


def strip_snapshot_media(record: dict[str, Any]) -> dict[str, Any]:
    """Return a checkpoint whose rollout files no longer embed image bytes.

    Unchanged files keep their exact bytes and digests; rewritten rollouts get
    a fresh SHA-256 so validation and the in-workspace restore still pass.
    """
    files = []
    for item in record["files"]:
        if not item["path"].endswith(".jsonl"):
            files.append(item)
            continue
        data = base64.b64decode(item["data"], validate=True)
        if _MARKER not in data:
            files.append(item)
            continue
        stripped = _strip_rollout(data)
        files.append(
            {
                "path": item["path"],
                "data": base64.b64encode(stripped).decode("ascii"),
                "sha256": hashlib.sha256(stripped).hexdigest(),
            }
        )
    return {**record, "files": files}
