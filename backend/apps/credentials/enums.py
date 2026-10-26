"""
Enums for the credentials app.
"""

from __future__ import annotations

from django.db import models


class CredentialType(models.TextChoices):
    """Where a credential is stored and how it may be used."""

    ENV = "env", "Environment Variable"
    FILE = "file", "Credential File"
    SSH_KEY = "ssh_key", "SSH Key"
    MCP_OAUTH = "mcp_oauth", "MCP OAuth (server-side only)"
    SERVER_SECRET = "server_secret", "Server credential (server-side only)"
    PROVIDER_SECRET = "provider_secret", "Internal provider secret (server-side only)"
