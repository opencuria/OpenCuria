"""Conservative sentence reconstruction from measured provider word intervals."""

from __future__ import annotations

import math
import re
from typing import Any

from .voice_schemas import VoiceSegment

_SENTENCE_END = re.compile(r"""[.!?]+["'”’»)\]]*(?=\s|$)|[。！？]+["'”’»)\]]*""")


def compact_text(value: str) -> str:
    """Compare transcript coverage while ignoring punctuation and whitespace."""
    return re.sub(r"[\W_]+", "", value).casefold()


def word_sentences(text: str, items: Any, duration_ms: int) -> list[VoiceSegment]:
    """Use sentence punctuation only where it coincides with a measured word end.

    Prefer punctuation from the complete transcript; some providers omit it in
    their word array. Word punctuation is a fallback when the full text has none.
    Invalid or partial metadata yields no result so callers retain phrase timing
    or the complete untimed transcript. No time is interpolated from text length.
    """
    if not isinstance(items, list) or not items or len(items) > 12_000:
        return []
    try:
        words = []
        last_start = last_end = 0
        for item in items:
            value = item["word"]
            if not isinstance(value, str):
                return []
            start, end = float(item["start"]), float(item["end"])
            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or not 0 <= start <= end <= duration_ms / 1000
            ):
                return []
            start_ms, end_ms = round(start * 1000), round(end * 1000)
            if start_ms < last_start or end_ms < last_end:
                return []
            last_start, last_end = start_ms, end_ms
            normalized = compact_text(value)
            if normalized:
                words.append((value, normalized, start_ms, end_ms))
        if not words or "".join(word[1] for word in words) != compact_text(text):
            return []

        # Store the source character offset for every normalized character. This
        # also handles case folding that expands one character, e.g. ß -> ss.
        source_ends = []
        for index, char in enumerate(text):
            source_ends.extend([index + 1] * len(compact_text(char)))
        boundaries = {
            len(compact_text(text[: match.end()])): match.end()
            for match in _SENTENCE_END.finditer(text)
        }
        if not boundaries:
            count = 0
            for value, normalized, _, _ in words:
                count += len(normalized)
                if _SENTENCE_END.search(value):
                    boundaries[count] = source_ends[count - 1]
        boundaries[len(source_ends)] = len(text)
        result = []
        count = source_start = 0
        sentence_start = words[0][2]
        for index, (_, normalized, _, end_ms) in enumerate(words):
            count += len(normalized)
            source_end = boundaries.get(count)
            if source_end is None:
                continue
            phrase = text[source_start:source_end].strip()
            result.append(
                VoiceSegment(
                    start_ms=sentence_start, end_ms=end_ms, text=phrase, raw_text=phrase
                )
            )
            if len(result) > 3000:
                return []
            source_start = source_end
            if index + 1 < len(words):
                sentence_start = words[index + 1][2]
        return result
    except (KeyError, TypeError, ValueError, OverflowError):
        return []
