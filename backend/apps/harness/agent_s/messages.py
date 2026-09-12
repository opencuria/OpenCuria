"""Agent-S wire-message helpers (OpenAI-style message dicts).

Portions adapted from simular-ai/Agent-S (https://github.com/simular-ai/Agent-S,
commit 3aa272d, Apache License 2.0). See THIRD_PARTY_NOTICES.md at the
repository root. The surrounding OpenCuria project remains AGPL-3.0-only.

Semantics mirrored from ``LMMAgent`` (``gui_agents/s3/core/mllm.py``):

- messages are ``{"role": ..., "content": [{"type": "text", ...}, ...]}``;
- images are PNG ``data:`` URLs with ``detail="high"``;
- role inference: explicit ``"user"`` stays ``"user"``; any other requested
  role alternates from the previous message (system -> user, user ->
  assistant, assistant -> user);
- ``put_text_last=True`` (grounding calls) moves the text part after images.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant"]

TEXT_TYPE = "text"
IMAGE_URL_TYPE = "image_url"
IMAGE_DETAIL_HIGH = "high"

PNG_DATA_URL_PREFIX = "data:image/png;base64,"


def encode_image_to_data_url(image_bytes: bytes) -> str:
    """Encode raw PNG bytes as an Agent-S image data URL."""
    return PNG_DATA_URL_PREFIX + base64.b64encode(image_bytes).decode("utf-8")


def text_part(text: str) -> dict[str, Any]:
    return {"type": TEXT_TYPE, "text": text}


def image_part(image_bytes: bytes, detail: str = IMAGE_DETAIL_HIGH) -> dict[str, Any]:
    return {
        "type": IMAGE_URL_TYPE,
        "image_url": {
            "url": encode_image_to_data_url(image_bytes),
            "detail": detail,
        },
    }


def infer_role(messages: list[dict[str, Any]], requested_role: str | None) -> Role:
    """Mirror ``LMMAgent.add_message`` role inference exactly."""
    if requested_role == "user":
        return "user"
    if not messages:
        return "user"
    last = messages[-1]["role"]
    if last == "system":
        return "user"
    if last == "user":
        return "assistant"
    return "user"


def system_message(text: str) -> dict[str, Any]:
    return {"role": "system", "content": [text_part(text)]}


def replace_system_prompt(
    messages: list[dict[str, Any]], system_prompt: str
) -> list[dict[str, Any]]:
    """Mirror ``LMMAgent.add_system_prompt`` (replace-or-append index 0)."""
    entry = system_message(system_prompt)
    if messages:
        messages[0] = entry
    else:
        messages.append(entry)
    return messages


def append_message(
    messages: list[dict[str, Any]],
    text_content: str,
    image_content: bytes | list[bytes] | None = None,
    role: str | None = None,
    image_detail: str = IMAGE_DETAIL_HIGH,
    put_text_last: bool = False,
) -> dict[str, Any]:
    """Mirror ``LMMAgent.add_message`` for PNG-bytes screenshots.

    Falsy ``image_content`` (``None``/``b""``/``[]``) adds no image part,
    matching ``if isinstance(...) or image_content`` / ``if image_content``
    in the reference OpenAI/Gemini/vLLM/HuggingFace branches.
    """
    resolved_role = infer_role(messages, role)
    content: list[dict[str, Any]] = [text_part(text_content)]
    images: list[bytes] = []
    if isinstance(image_content, list):
        images = [img for img in image_content if img]
    elif image_content:
        images = [image_content]
    for img in images:
        content.append(image_part(img, detail=image_detail))
    if put_text_last:
        text = content.pop(0)
        content.append(text)
    message = {"role": resolved_role, "content": content}
    messages.append(message)
    return message


def append_text_message(
    messages: list[dict[str, Any]], text: str, role: str | None = None
) -> dict[str, Any]:
    """Append a text-only message (formatting-feedback path included)."""
    return append_message(messages, text, image_content=None, role=role)


@dataclass
class Conversation:
    """Minimal ``LMMAgent`` message store without any engine dependency."""

    system_prompt: str = "You are a helpful assistant."
    messages: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.messages:
            self.reset()

    def reset(self) -> None:
        self.messages = [system_message(self.system_prompt)]

    def add_system_prompt(self, system_prompt: str) -> None:
        self.system_prompt = system_prompt
        replace_system_prompt(self.messages, system_prompt)

    def add_message(
        self,
        text_content: str,
        image_content: bytes | list[bytes] | None = None,
        role: str | None = None,
        image_detail: str = IMAGE_DETAIL_HIGH,
        put_text_last: bool = False,
    ) -> dict[str, Any]:
        return append_message(
            self.messages,
            text_content,
            image_content=image_content,
            role=role,
            image_detail=image_detail,
            put_text_last=put_text_last,
        )


def is_image_part(part: dict[str, Any]) -> bool:
    """Match the ``"image" in part.get("type", "")`` flush-detection quirk."""
    return "image" in str(part.get("type", ""))
