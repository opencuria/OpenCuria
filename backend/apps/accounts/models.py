"""
Custom User model for opencuria backend.

Using a custom User model from the start to allow future extension
without migration headaches.
"""

from __future__ import annotations

import enum
import uuid

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class APIKeyPermission(str, enum.Enum):
    """
    Fine-grained permissions that can be assigned to an API key.

    Applies to both the REST API and MCP interface.
    """

    # Workspaces
    WORKSPACES_READ = "workspaces:read"
    WORKSPACES_CREATE = "workspaces:create"
    WORKSPACES_UPDATE = "workspaces:update"
    WORKSPACES_STOP = "workspaces:stop"
    WORKSPACES_RESUME = "workspaces:resume"
    WORKSPACES_DELETE = "workspaces:delete"

    # Prompts
    PROMPTS_RUN = "prompts:run"
    PROMPTS_CANCEL = "prompts:cancel"

    # Terminal
    TERMINAL_ACCESS = "terminal:access"

    # Runners
    RUNNERS_READ = "runners:read"
    RUNNERS_CREATE = "runners:create"

    # Organizations
    ORGANIZATIONS_READ = "organizations:read"
    ORGANIZATIONS_WRITE = "organizations:write"

    # Agents
    AGENTS_READ = "agents:read"
    ORG_AGENT_DEFINITIONS_READ = "org_agent_definitions:read"
    ORG_AGENT_DEFINITIONS_WRITE = "org_agent_definitions:write"

    # Credentials
    CREDENTIALS_READ = "credentials:read"
    CREDENTIALS_WRITE = "credentials:write"
    ORG_CREDENTIAL_SERVICES_READ = "org_credential_services:read"
    ORG_CREDENTIAL_SERVICES_WRITE = "org_credential_services:write"

    # Conversations
    CONVERSATIONS_READ = "conversations:read"

    # Image artifacts
    IMAGES_READ = "images:read"
    IMAGES_CREATE = "images:create"
    IMAGES_DELETE = "images:delete"
    IMAGES_CLONE = "images:clone"

    # Image definitions
    IMAGE_DEFINITIONS_READ = "image_definitions:read"
    IMAGE_DEFINITIONS_WRITE = "image_definitions:write"
    IMAGE_DEFINITIONS_MANAGE_RUNNERS = "image_definitions:manage_runners"

    # Skills
    SKILLS_READ = "skills:read"
    SKILLS_WRITE = "skills:write"

    # MCP
    MCP_ACCESS = "mcp:access"

    @classmethod
    def all_values(cls) -> list[str]:
        """Return all permission string values."""
        return [p.value for p in cls]


class UserManager(BaseUserManager):
    """Custom manager that uses email as the unique identifier."""

    def create_user(self, email: str, password: str | None = None, **extra_fields):
        """Create and return a regular user with the given email and password."""
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        extra_fields.setdefault("username", email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str | None = None, **extra_fields):
        """Create and return a superuser."""
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    """
    Custom user model for opencuria.

    Uses email as the primary login field instead of username.
    """

    email = models.EmailField(unique=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()  # type: ignore[assignment]

    class Meta:
        db_table = "accounts_user"
        verbose_name = "user"
        verbose_name_plural = "users"

    def __str__(self) -> str:
        return self.email


class APIKey(models.Model):
    """
    Long-lived API key for external integrations (n8n, Zapier, custom scripts).

    The raw token is never stored — only its SHA-256 hash. The token is
    returned exactly once at creation time and cannot be retrieved again.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_keys")
    name = models.CharField(max_length=255)
    key_hash = models.CharField(max_length=64, unique=True)
    key_prefix = models.CharField(max_length=12)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    permissions = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "List of permission strings. "
            "An empty list grants no access."
        ),
    )

    class Meta:
        db_table = "accounts_apikey"
        verbose_name = "API key"
        verbose_name_plural = "API keys"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.key_prefix}...)"

    def has_permission(self, permission: APIKeyPermission | str) -> bool:
        """
        Check if this API key has the given permission.

        Permissions are an explicit allowlist.
        """
        perm_value = permission.value if isinstance(permission, APIKeyPermission) else permission
        return perm_value in (self.permissions or [])
