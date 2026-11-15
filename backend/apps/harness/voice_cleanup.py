"""Conservative final normalization for model-edited dictation."""

from __future__ import annotations

import re

# Restrict this fallback to isolated hesitation at the start of prose. Quotes,
# backticks and ordinary words deliberately cannot match. In particular, "um"
# is a meaningful German preposition and must not be stripped as an English filler.
_LEADING_HESITATION = re.compile(
    r"^(?:ähm?|ehm|uhm?|erm)(?:\s*(?:[,…]+|\.{2,})\s*|\s*$)",
    flags=re.IGNORECASE,
)


def normalize_edited_dictation(text: str) -> str:
    """Remove unmistakable leading hesitation while preserving quoted literals.

    The editing model handles contextual cleanup and punctuation. This small
    fallback catches an isolated leading hesitation it occasionally leaves in
    otherwise edited prose, without deleting words from quotations, code or
    sentences that discuss a filler word. Keep the remaining casing, since the
    first substantive token may be a case-sensitive command or identifier.
    """
    result = text.strip()
    while match := _LEADING_HESITATION.match(result):
        result = result[match.end() :].lstrip()
    return result
