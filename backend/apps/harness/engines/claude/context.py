"""Compatibility alias for provider-independent media helpers.

Keep the module object shared so existing imports and monkeypatch seams (notably
media limits and no-follow readers) retain their original behavior.
"""

import sys

from ..shared import media as _shared

sys.modules[__name__] = _shared
