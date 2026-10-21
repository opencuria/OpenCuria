"""Compatibility alias for provider-independent shellpolicy helpers.

Keep the module object shared so existing imports and monkeypatch seams (notably
media limits and no-follow readers) retain their original behavior.
"""

import sys

from ..shared import shellpolicy as _shared

sys.modules[__name__] = _shared
