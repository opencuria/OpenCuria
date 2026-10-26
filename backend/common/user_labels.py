"""Privacy-preserving owner labels shared by storage and deletion views."""

from typing import Any


def owner_label(user: Any | None) -> str:
    """Return a safe display name, never an email or authentication identifier."""
    if not user:
        return "Unknown owner"
    name = user.get_full_name().strip()
    return name if name and "@" not in name else f"User {user.pk}"
