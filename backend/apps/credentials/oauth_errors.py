"""Shared OAuth failure contract for protocol workflows and HTTP transport."""


class OAuthError(ValueError):
    """Sanitized OAuth protocol/configuration error."""
