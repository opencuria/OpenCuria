"""Privacy-preserving owner labels shared by storage and deletion views.

Workspace names are private to the workspace owner. Views that list other
users' workspaces (runner storage, image deletion, runner activity) show
them by owner label only.
"""

from typing import Any


def owner_label(user: Any | None) -> str:
    """Return a safe display name, never an email or authentication identifier."""
    if not user:
        return "Unknown owner"
    name = user.get_full_name().strip()
    return name if name and "@" not in name else f"User {user.pk}"


def private_workspace_name(name: str | None, *, owner_id: Any, viewer_id: Any) -> str:
    """Return the workspace name for its owner and an empty name for anyone else."""
    if owner_id is None or viewer_id is None or str(owner_id) != str(viewer_id):
        return ""
    return name or ""


def workspace_title(
    name: str | None, *, owner_id: Any, owner: str | None, viewer_id: Any
) -> str:
    """Title a workspace by name for its owner, otherwise by its owner label."""
    visible = private_workspace_name(name, owner_id=owner_id, viewer_id=viewer_id)
    if visible:
        return visible
    return f"Workspace · {owner}" if owner else "Workspace"
