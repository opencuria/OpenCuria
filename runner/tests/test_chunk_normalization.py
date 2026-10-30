"""Keep file-transfer whitespace semantics while avoiding regex scan overhead."""

from __future__ import annotations

import base64
import random
import re
import sys

import pytest
from src.chunking import (
    CHUNK_B64_SIZE,
    normalize_base64,
    should_chunk,
    split_base64_chunks,
)


def test_all_unicode_whitespace_matches_existing_wire_contract() -> None:
    """Include ASCII controls and every Unicode whitespace codepoint."""
    whitespace = "".join(
        character
        for character in map(chr, range(sys.maxunicode + 1))
        if re.fullmatch(r"\s", character)
    )
    assert normalize_base64(f"{whitespace}QU{whitespace}JD{whitespace}==") == "QUJD=="
    assert normalize_base64(whitespace) == ""


@pytest.mark.parametrize("invalid", ["!", "\x00", "ñ", "\u200b", "\ufeff", "\ud800"])
def test_non_whitespace_is_preserved_for_downstream_validation(invalid: str) -> None:
    assert normalize_base64(f" \nQUJD{invalid}REVG\t") == f"QUJD{invalid}REVG"


def test_mixed_payloads_match_whitespace_projection() -> None:
    """Changing normalization cannot accept, reject or alter encoded characters."""
    generator = random.Random(0)
    alphabet = "QUJDREVG+/=!?\x00\t\n\r\v\f\x1c\x85\u00a0\u2003\u2028\u200bñ"
    for _ in range(100):
        payload = "".join(generator.choices(alphabet, k=1000))
        assert normalize_base64(payload) == re.sub(r"\s+", "", payload)


@pytest.mark.parametrize("wrapped", [False, True])
def test_large_binary_transfer_preserves_bytes_and_chunk_boundaries(
    wrapped: bool,
) -> None:
    content = bytes(range(256)) * 8193
    encoded = base64.b64encode(content).decode()
    payload = (
        "\r\n".join(
            encoded[offset : offset + 76] for offset in range(0, len(encoded), 76)
        )
        if wrapped
        else encoded
    )
    normalized = normalize_base64(payload)
    chunks = split_base64_chunks(payload)
    assert normalized == encoded == "".join(chunks)
    assert all(len(chunk) == CHUNK_B64_SIZE for chunk in chunks[:-1])
    assert len(chunks[-1]) <= CHUNK_B64_SIZE
    assert base64.b64decode(normalized, validate=True) == content


@pytest.mark.parametrize("payload", ["", " \t\n", " QU JD\n", "Q U J D R A==\r\n"])
def test_chunk_threshold_uses_normalized_length(payload: str) -> None:
    assert should_chunk(payload, limit=4) == (len(re.sub(r"\s+", "", payload)) > 4)
