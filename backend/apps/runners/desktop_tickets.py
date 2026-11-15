"""Short-lived, single-use tickets that open one workspace desktop viewer.

Access tokens never travel in viewer URLs, where they would reach logs and the
viewer document. A client exchanges its authenticated session for a ticket
over REST; the desktop proxy redeems it once for a path-scoped HttpOnly
cookie. Tickets are signed, bound to one user and workspace and expire after
``DESKTOP_TICKET_MAX_AGE_SECONDS``. Redemption is recorded in the Django
cache, so single use holds across processes that share it.
"""

from __future__ import annotations

import secrets
import uuid

from django.core import signing
from django.core.cache import cache

DESKTOP_TICKET_MAX_AGE_SECONDS = 60
_SALT = "opencuria.desktop-ticket"


def issue_desktop_ticket(workspace_id: uuid.UUID | str, user_id: int) -> str:
    """Return a ticket that lets *user_id* open the desktop of *workspace_id*."""
    payload = {
        "w": str(workspace_id),
        "u": int(user_id),
        "n": secrets.token_urlsafe(16),
    }
    return signing.dumps(payload, salt=_SALT)


def redeem_desktop_ticket(ticket: str, workspace_id: str) -> int | None:
    """Return the ticket's user ID once, or None if invalid, used or expired."""
    try:
        payload = signing.loads(
            ticket, salt=_SALT, max_age=DESKTOP_TICKET_MAX_AGE_SECONDS
        )
    except signing.BadSignature:
        return None
    if not isinstance(payload, dict) or payload.get("w") != str(workspace_id):
        return None
    user_id, nonce = payload.get("u"), payload.get("n")
    if not isinstance(user_id, int) or not isinstance(nonce, str):
        return None
    if not cache.add(
        f"desktop-ticket:{nonce}", True, timeout=DESKTOP_TICKET_MAX_AGE_SECONDS + 5
    ):
        return None
    return user_id
