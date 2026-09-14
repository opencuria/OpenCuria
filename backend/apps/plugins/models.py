"""
Database models for the plugins app.

A ``Plugin`` is either global (``organization`` null, managed by platform
staff) or org-owned. Plugins carry relational components:

- ``PluginSkill`` -- Markdown prompt fragments, ordered by ``position``.
- ``PluginMcpServer`` -- MCP server definitions (stdio or HTTP/SSE).
  ``command`` is a single executable; ``args`` are never joined into a
  shell string. ``env``/``headers`` values may contain credential
  placeholders of the form ``{{credential.KEY}}``.
- ``PluginCredentialRequirement`` -- plugin-wide credential requirements
  referencing a ``CredentialService``. The referenced service may be a
  global service or an org-owned service created exclusively for this
  plugin (``plugin_owned_service`` marker).

Activations:

- ``OrgPluginActivation`` -- row existence means the plugin is released
  org-wide.
- ``WorkspacePluginActivation`` -- per-workspace opt-in. Rows survive org
  deactivation (temporarily ineffective until re-enabled).
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class PluginTransport(models.TextChoices):
    """Supported MCP transports for a plugin MCP server."""

    STDIO = "stdio", "STDIO"
    STREAMABLE_HTTP = "streamable_http", "Streamable HTTP"
    SSE = "sse", "SSE"


class Plugin(models.Model):
    """A plugin definition (global or org-owned)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="plugins",
        help_text="Null for global OpenCuria plugins; set for org-owned plugins.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_plugins",
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    description = models.TextField(blank=True, default="")
    enabled = models.BooleanField(
        default=True,
        help_text="Whether the plugin definition is enabled (staff switch).",
    )
    published = models.BooleanField(
        default=True,
        help_text="Whether the plugin is visible for org/workspace activation.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "plugins_plugin"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["slug"],
                condition=models.Q(organization__isnull=True),
                name="unique_global_plugin_slug",
            ),
            models.UniqueConstraint(
                fields=["organization", "slug"],
                condition=models.Q(organization__isnull=False),
                name="unique_org_plugin_slug",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class PluginSkill(models.Model):
    """A Markdown skill fragment belonging to a plugin."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plugin = models.ForeignKey(
        Plugin,
        on_delete=models.CASCADE,
        related_name="plugin_skills",
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    body = models.TextField(help_text="Markdown content appended to the prompt.")
    position = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "plugins_skill"
        ordering = ["position", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["plugin", "slug"],
                name="unique_plugin_skill_slug",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.plugin_id}:{self.slug}"


class PluginMcpServer(models.Model):
    """An MCP server definition belonging to a plugin."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plugin = models.ForeignKey(
        Plugin,
        on_delete=models.CASCADE,
        related_name="mcp_servers",
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    transport = models.CharField(
        max_length=20,
        choices=PluginTransport.choices,
        default=PluginTransport.STDIO,
    )
    command = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text="Single executable for stdio transport (no shell).",
    )
    args = models.JSONField(
        default=list,
        blank=True,
        help_text="Argument list (never joined into a shell string).",
    )
    cwd = models.CharField(max_length=1024, blank=True, default="/workspace")
    env = models.JSONField(
        default=dict,
        blank=True,
        help_text="Env mapping; values may use {{credential.KEY}} placeholders.",
    )
    url = models.CharField(max_length=2048, blank=True, default="")
    headers = models.JSONField(
        default=dict,
        blank=True,
        help_text="Header mapping; values may use {{credential.KEY}} placeholders.",
    )
    startup_timeout_seconds = models.PositiveIntegerField(default=30)
    request_timeout_seconds = models.PositiveIntegerField(default=60)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "plugins_mcp_server"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["plugin", "slug"],
                name="unique_plugin_mcp_server_slug",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.plugin_id}:{self.slug}"


class PluginCredentialRequirement(models.Model):
    """A plugin-wide credential requirement.

    ``plugin_owned_service`` marks services created exclusively for this
    plugin through the plugin API. Such definitions may only be deleted
    together with the plugin (and only when no credentials reference them).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plugin = models.ForeignKey(
        Plugin,
        on_delete=models.CASCADE,
        related_name="credential_requirements",
    )
    key = models.SlugField(
        max_length=255,
        help_text="Stable requirement identifier within the plugin.",
    )
    credential_service = models.ForeignKey(
        "credentials.CredentialService",
        on_delete=models.PROTECT,
        related_name="plugin_requirements",
    )
    required = models.BooleanField(default=True)
    description = models.TextField(blank=True, default="")
    plugin_owned_service = models.BooleanField(
        default=False,
        help_text=(
            "True when the referenced service was created exclusively for "
            "this plugin via the plugin API."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "plugins_credential_requirement"
        ordering = ["key"]
        constraints = [
            models.UniqueConstraint(
                fields=["plugin", "key"],
                name="unique_plugin_credential_requirement_key",
            ),
            models.UniqueConstraint(
                fields=["plugin", "credential_service"],
                name="unique_plugin_credential_requirement_service",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.plugin_id}:{self.key}"


class OrgPluginActivation(models.Model):
    """Org-wide release of a plugin (row existence = enabled)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="plugin_activations",
    )
    plugin = models.ForeignKey(
        Plugin,
        on_delete=models.CASCADE,
        related_name="org_activations",
    )
    enabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="org_plugin_activations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "plugins_org_activation"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "plugin"],
                name="unique_org_plugin_activation",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"OrgPluginActivation(org={self.organization_id}, plugin={self.plugin_id})"
        )


class WorkspacePluginActivation(models.Model):
    """Per-workspace opt-in for an org-enabled plugin.

    Rows are NOT deleted on org deactivation; they become temporarily
    ineffective until the org activation is restored.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "runners.Workspace",
        on_delete=models.CASCADE,
        related_name="plugin_activations",
    )
    plugin = models.ForeignKey(
        Plugin,
        on_delete=models.CASCADE,
        related_name="workspace_activations",
    )
    enabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workspace_plugin_activations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "plugins_workspace_activation"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "plugin"],
                name="unique_workspace_plugin_activation",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"WorkspacePluginActivation(ws={self.workspace_id}, "
            f"plugin={self.plugin_id})"
        )
