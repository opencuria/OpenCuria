"""Grounding + OCR helpers (exact Agent-S text/coordinate behaviour).

Portions adapted from simular-ai/Agent-S (https://github.com/simular-ai/Agent-S,
commit 3aa272d, Apache License 2.0). See THIRD_PARTY_NOTICES.md at the
repository root. The surrounding OpenCuria project remains AGPL-3.0-only.

Reference (``gui_agents/s3/agents/grounding.py``):

- grounding prompt is ``f"Query:{ref_expr}\\nOutput only the coordinate of "
  "one point in your response.\\n"`` sent with ``put_text_last=True`` and the
  ``LMMAgent`` default system prompt (``"You are a helpful assistant."`` —
  Agent-S never passes an explicit prompt for the grounding model);
- coordinates parse via ``re.findall(r"\\d+", response)`` and
  ``assert len(numericals) >= 2``, taking the first two;
- ``resize_coordinates`` is ``round(coord * actual / grounding_dimension)``;
- OCR words are cleaned with
  ``re.sub(r"^[^a-zA-Z\\s.,!?;:\\-\\+]+|[^a-zA-Z\\s.,!?;:\\-\\+]+$", "", word)``
  and grouped per ``block_num`` (``word_num`` counts per block); the text
  table is ``"Text Table:\\nWord id\\tText\\n"`` plus ``f"{id}\\t{text}\\n"``;
- ``generate_text_coords`` sends the alignment-aware prompt + table first,
  then ``"Screenshot:\\n"`` with the image, parses the **last** number
  (defaulting to ``0`` when none), and computes start/end/center coords
  with integer ``// 2`` halves (no resize scaling — OCR pixels are used
  directly).
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from . import messages as _messages
from . import prompts as _prompts
from .llm import call_llm_safe
from .ports import CompletionPort, Usage

GROUNDING_RESPONSE_TEMPLATE = (
    "Query:{ref_expr}\nOutput only the coordinate of one point in your response.\n"
)

# Agent-S never passes an explicit system prompt for the grounding model, so
# ``LMMAgent`` falls back to its default (``gui_agents/s3/core/mllm.py``).
GROUNDING_DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."

_OCR_CLEAN_PATTERN = r"^[^a-zA-Z\s.,!?;:\-\+]+|[^a-zA-Z\s.,!?;:\-\+]+$"
_OCR_TABLE_HEADER = "Text Table:\nWord id\tText\n"


def build_grounding_prompt(ref_expr: str) -> str:
    return GROUNDING_RESPONSE_TEMPLATE.format(ref_expr=ref_expr)


def parse_grounding_coords(response: str) -> list[int]:
    numericals = re.findall(r"\d+", response)
    assert len(numericals) >= 2
    return [int(numericals[0]), int(numericals[1])]


def resize_coordinates(
    coordinates: list[int],
    *,
    width: int,
    height: int,
    grounding_width: int,
    grounding_height: int,
) -> list[int]:
    """Scale grounding-model coords into desktop pixels (exact formula)."""
    return [
        round(coordinates[0] * width / grounding_width),
        round(coordinates[1] * height / grounding_height),
    ]


def clean_ocr_word(word: str) -> str:
    return re.sub(_OCR_CLEAN_PATTERN, "", word)


def build_ocr_table(ocr_rows: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Build the OCR text table + element list from raw OCR rows.

    ``ocr_rows`` entries carry ``text/block_num/left/top/width/height``
    (as produced by the :class:`OcrPort <apps.harness.agent_s.ports.OcrPort>`
    adapter). Empty cleaned words are skipped, exactly like Agent-S.
    """
    grouping_map: dict[Any, list[str]] = defaultdict(list)
    ocr_table = _OCR_TABLE_HEADER
    ocr_elements: list[dict[str, Any]] = []
    ocr_id = 0
    for row in ocr_rows:
        text = clean_ocr_word(row.get("text", ""))
        if text:
            block_num = row.get("block_num")
            grouping_map[block_num].append(text)
            ocr_table += f"{ocr_id}\t{text}\n"
            ocr_elements.append(
                {
                    "id": ocr_id,
                    "text": text,
                    "group_num": block_num,
                    "word_num": len(grouping_map[block_num]),
                    "left": row.get("left"),
                    "top": row.get("top"),
                    "width": row.get("width"),
                    "height": row.get("height"),
                }
            )
            ocr_id += 1
    return ocr_table, ocr_elements


_FIRST_WORD_PROMPT = (
    "**Important**: Output the word id of the FIRST word in the provided phrase.\n"
)
_LAST_WORD_PROMPT = (
    "**Important**: Output the word id of the LAST word in the provided phrase.\n"
)


def alignment_prompt(alignment: str) -> str:
    if alignment == "start":
        return _FIRST_WORD_PROMPT
    if alignment == "end":
        return _LAST_WORD_PROMPT
    return ""


def build_text_span_first_message(
    phrase: str, ocr_table: str, alignment: str = ""
) -> str:
    return alignment_prompt(alignment) + "Phrase: " + phrase + "\n" + ocr_table


def parse_text_span_id(response: str) -> int:
    numericals = re.findall(r"\d+", response)
    if len(numericals) > 0:
        return int(numericals[-1])
    return 0


def text_span_coords(elem: dict[str, Any], alignment: str = "") -> list[int]:
    if alignment == "start":
        return [elem["left"], elem["top"] + (elem["height"] // 2)]
    if alignment == "end":
        return [elem["left"] + elem["width"], elem["top"] + (elem["height"] // 2)]
    return [
        elem["left"] + (elem["width"] // 2),
        elem["top"] + (elem["height"] // 2),
    ]


async def generate_coords(
    completion: CompletionPort,
    screenshot: bytes,
    ref_expr: str,
    *,
    record_usage=None,
    sleep=None,
) -> tuple[list[int], list[Usage]]:
    """Ground a referring expression (temperature 0, text-last message)."""
    wire: list[dict[str, Any]] = [
        _messages.system_message(GROUNDING_DEFAULT_SYSTEM_PROMPT)
    ]
    _messages.append_message(
        wire,
        build_grounding_prompt(ref_expr),
        image_content=screenshot,
        role="user",
        put_text_last=True,
    )
    text, usages = await call_llm_safe(
        completion,
        wire,
        purpose="grounding",
        temperature=0.0,
        use_thinking=False,
        record_usage=record_usage,
        sleep=sleep,
    )
    return parse_grounding_coords(text), usages


async def generate_text_coords(
    completion: CompletionPort,
    ocr_rows: list[dict[str, Any]],
    screenshot: bytes,
    phrase: str,
    *,
    alignment: str = "",
    record_usage=None,
    sleep=None,
) -> tuple[list[int], dict[str, Any], list[Usage]]:
    """Ground a text phrase via the OCR table (temperature 0)."""
    ocr_table, ocr_elements = build_ocr_table(ocr_rows)
    wire: list[dict[str, Any]] = [
        _messages.system_message(_prompts.PHRASE_TO_WORD_COORDS_PROMPT)
    ]
    _messages.append_text_message(
        wire, build_text_span_first_message(phrase, ocr_table, alignment), role="user"
    )
    _messages.append_message(
        wire, "Screenshot:\n", image_content=screenshot, role="user"
    )
    text, usages = await call_llm_safe(
        completion,
        wire,
        purpose="text_span",
        temperature=0.0,
        use_thinking=False,
        record_usage=record_usage,
        sleep=sleep,
    )
    elem = ocr_elements[parse_text_span_id(text)]
    return text_span_coords(elem, alignment), elem, usages


def wants_thinking(model: str) -> bool:
    """Claude thinking-model list (``Worker.__init__`` behaviour)."""
    return model in _prompts.CLAUDE_THINKING_MODELS
