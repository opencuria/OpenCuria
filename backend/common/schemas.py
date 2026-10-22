"""
Response schemas shared by the REST adapters of all apps.

Kept app-neutral so feature apps never import another app's schemas just
to describe an error response.
"""

from __future__ import annotations

from ninja import Schema


class ErrorOut(Schema):
    """Standard error response with optional safe dependency-gap details."""

    detail: str
    code: str = "error"
    # For missing_plugin_credentials: plugin_id, plugin_name, key, service_id,
    # service_name, credential_type. Contains metadata only; never secret data.
    gaps: list[dict] | None = None
