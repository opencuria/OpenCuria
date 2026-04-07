"""
Database models for the credentials app.

Provides two models:

- ``CredentialService`` -- admin-managed catalog of external services
  (e.g. GitHub, OpenAI, SSH Key). Each service has exactly one
  credential type (``env`` or ``ssh_key``).
- ``Credential`` -- user- or org-scoped credential instances, holding an
  encrypted value.  Ownership is mutually exclusive: either ``user`` or
  ``organization`` is set, but never both.  Personal credentials are
  visible across all of a user's organizations; org credentials are
  visible to all org members and only editable by admins.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from .enums import CredentialType


class CredentialService(models.Model):
    """
    A global catalog entry for an external service that accepts credentials.

    Each service defines exactly one credential type and injection method.
    Managed by platform admins via the Django admin.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(
        max_length=255,
        help_text="Human-readable service name (e.g. 'GitHub').",
    )
    slug = models.SlugField(
        max_length=255,
        unique=True,
        help_text="URL-safe identifier (e.g. 'github').",
    )
    description = models.TextField(
        blank=True,
        default="",
        help_text="Optional description shown to users.",
    )
    credential_type = models.CharField(
        max_length=20,
        choices=CredentialType.choices,
        default=CredentialType.ENV,
        help_text="How this credential is injected into a workspace.",
    )
    env_var_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Name of the environment variable (e.g. 'GITHUB_TOKEN'). "
        "Only used when credential_type is 'env'.",
    )
    label = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Short label shown to users (e.g. 'Personal Access Token').",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "credentials_service"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Credential(models.Model):
    """
    A user- or org-scoped credential instance.

    Ownership is mutually exclusive: either ``user`` or ``organization``
    is set, never both.

    - user-owned:  visible across all of the user's organizations.
    - org-owned:   visible to all org members; only admins may edit/delete.

    Stores the credential value encrypted (Fernet). The plaintext value
    is never exposed via the API; it is only decrypted server-side when
    dispatching a workspace creation task to a runner.

    For SSH key credentials, ``encrypted_value`` holds the encrypted private
    key and ``public_key`` stores the public key in OpenSSH format.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="personal_credentials",
        help_text="Set for personal credentials (mutually exclusive with organization).",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="credentials",
        help_text="Set for org credentials (mutually exclusive with user).",
    )
    service = models.ForeignKey(
        CredentialService,
        on_delete=models.CASCADE,
        related_name="credentials",
    )
    name = models.CharField(
        max_length=255,
        help_text="User-chosen display name (e.g. 'My GitHub PAT').",
    )
    encrypted_value = models.TextField(
        help_text="Fernet-encrypted credential value.",
    )
    public_key = models.TextField(
        blank=True,
        default="",
        help_text="Public key in OpenSSH format (only for ssh_key type).",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="credentials",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "credentials_credential"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(user__isnull=False, organization__isnull=True)
                    | models.Q(user__isnull=True, organization__isnull=False)
                ),
                name="credential_owner_exclusive",
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.service.name})"


class OrgCredentialServiceActivation(models.Model):
    """
    Controls which credential services are active for an organization.

    Org admins can activate or deactivate which credential services are
    available to their members. When a new organization is created, all
    existing credential services are activated by default.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="credential_service_activations",
    )
    credential_service = models.ForeignKey(
        CredentialService,
        on_delete=models.CASCADE,
        related_name="org_activations",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "credentials_org_service_activation"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "credential_service"],
                name="unique_org_credential_service_activation",
            ),
        ]

    def __str__(self) -> str:
        return f"OrgCredServiceActivation(org={self.organization_id}, svc={self.credential_service_id})"
