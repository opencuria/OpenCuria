"""
Base exception classes for opencuria backend.

All app-level exceptions should inherit from ServiceError to enable
consistent error handling in API views and consumers.
"""

from __future__ import annotations


class ServiceError(Exception):
    """Base exception for all service-layer errors."""

    def __init__(self, message: str, code: str = "service_error") -> None:
        self.message = message
        self.code = code
        super().__init__(message)


class NotFoundError(ServiceError):
    """Raised when a requested resource does not exist."""

    def __init__(self, resource: str, identifier: str) -> None:
        super().__init__(
            message=f"{resource} '{identifier}' not found",
            code="not_found",
        )
        self.resource = resource
        self.identifier = identifier


class ConflictError(ServiceError):
    """Raised when an operation conflicts with the current resource state."""

    def __init__(self, message: str) -> None:
        super().__init__(message=message, code="conflict")


class AuthenticationError(ServiceError):
    """Raised when authentication fails."""

    def __init__(self, message: str = "Authentication failed") -> None:
        super().__init__(message=message, code="authentication_error")
