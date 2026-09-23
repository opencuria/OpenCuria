"""xdotool key helpers (pure, stateless).

Canonical home (Step 1) for the xdotool key normalisation helpers
previously living in :mod:`src.service`. All functions here are pure
and have no dependency on ``WorkspaceService`` (stdlib + typing only).

Extraction owner: Step 1 (pure helpers). Stateful xdotool action
handlers stay on ``WorkspaceService`` until the sessions group step.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

# Ubuntu 22.04 ships xdotool 3.20160805, which has almost no key aliases
# and treats a bare "--" as an invalid option. Map LLM-friendly names to
# X11 keysyms that this version actually sends.
_XDOTOOL_KEY_ALIASES = {
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "space",
    "spacebar": "space",
    "backspace": "BackSpace",
    "bksp": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "arrowup": "Up",
    "arrowdown": "Down",
    "arrowleft": "Left",
    "arrowright": "Right",
    "pageup": "Page_Up",
    "pagedown": "Page_Down",
    "pgup": "Page_Up",
    "pgdn": "Page_Down",
    "home": "Home",
    "end": "End",
    "insert": "Insert",
    "ins": "Insert",
    "capslock": "Caps_Lock",
}
_XDOTOOL_MODIFIER_ALIASES = {
    "control": "ctrl",
    "ctrl": "ctrl",
    "command": "super",
    "cmd": "super",
    "meta": "super",
    "win": "super",
    "windows": "super",
    "super": "super",
    "option": "alt",
    "alt": "alt",
    "shift": "shift",
}
_XDOTOOL_KEY_FAILURE_MARKERS = (
    "No such key name",
    "Ignoring it",
    "Invalid --option",
)
_XDOTOOL_FUNCTION_KEY_RE = re.compile(r"^f([1-9]|1[0-9]|2[0-4])$")


def _collapse_xdotool_token(token: str) -> str:
    """Return a case- and separator-insensitive lookup key."""
    return token.strip().lower().replace("-", "").replace("_", "").replace(" ", "")


def _normalize_xdotool_token(token: str, *, modifier: bool = False) -> str:
    """Map one key or modifier name to an xdotool 3.20160805 token."""
    raw = str(token).strip()
    if not raw:
        raise ValueError("key token must not be empty")
    collapsed = _collapse_xdotool_token(raw)
    if modifier:
        alias = _XDOTOOL_MODIFIER_ALIASES.get(collapsed)
        if alias is not None:
            return alias
        key_alias = _XDOTOOL_KEY_ALIASES.get(collapsed)
        if key_alias is not None:
            return key_alias
        return raw
    alias = _XDOTOOL_KEY_ALIASES.get(collapsed)
    if alias is not None:
        return alias
    modifier_alias = _XDOTOOL_MODIFIER_ALIASES.get(collapsed)
    if modifier_alias is not None:
        return modifier_alias
    function_key = _XDOTOOL_FUNCTION_KEY_RE.fullmatch(collapsed)
    if function_key:
        return f"F{function_key.group(1)}"
    return raw


def _normalize_xdotool_key_combo(key: str, modifiers: list[Any]) -> str:
    """Build an xdotool key combo from a key name and optional modifiers."""
    if not isinstance(modifiers, list):
        raise ValueError("modifiers must be a list")
    mod_tokens: list[str] = []
    for item in modifiers:
        if not isinstance(item, str):
            raise ValueError("modifiers must be a list of strings")
        stripped = item.strip()
        if not stripped:
            continue
        mod_tokens.append(_normalize_xdotool_token(stripped, modifier=True))
    parts = [part.strip() for part in str(key).split("+") if part.strip()]
    if not parts:
        raise ValueError("key must not be empty")
    *combo_mods, key_part = parts
    tokens = [
        *mod_tokens,
        *(
            _normalize_xdotool_token(part, modifier=True)
            for part in combo_mods
        ),
        _normalize_xdotool_token(key_part),
    ]
    return "+".join(tokens)


def _xdotool_type_command(text: str) -> str:
    """Build an xdotool type command compatible with Ubuntu 22.04."""
    quoted = shlex.quote(text)
    if text.startswith("-"):
        return (
            f"printf '%s' {quoted} | "
            "xdotool type --delay 0 --clearmodifiers --file -"
        )
    return f"xdotool type --delay 0 --clearmodifiers {quoted}"


def _xdotool_key_failed(exit_code: int, output: str) -> bool:
    """Return True when xdotool did not actually deliver the key."""
    if exit_code != 0:
        return True
    return any(marker in output for marker in _XDOTOOL_KEY_FAILURE_MARKERS)
