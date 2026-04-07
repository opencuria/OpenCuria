"""
Domain-specific exceptions for the credentials app.
"""

from __future__ import annotations

from common.exceptions import NotFoundError


class CredentialNotFoundError(NotFoundError):
    """Raised when a credential is not found."""

    def __init__(self, credential_id: str) -> None:
        super().__init__("Credential", credential_id)
