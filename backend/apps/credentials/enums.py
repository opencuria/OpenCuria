"""
Enums for the credentials app.
"""

from __future__ import annotations

from django.db import models


class CredentialType(models.TextChoices):
    """How a credential is injected into a workspace."""

    ENV = "env", "Environment Variable"
    FILE = "file", "Credential File"
    SSH_KEY = "ssh_key", "SSH Key"
