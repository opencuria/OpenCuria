"""HTTP error payloads shared by adapters."""


def api_key_permission_denied(permission: str) -> tuple[int, dict[str, str]]:
    """Return the HTTP error for a missing API key permission."""
    return 403, {
        "detail": f"API key lacks permission: {permission}",
        "code": "permission_denied",
    }
