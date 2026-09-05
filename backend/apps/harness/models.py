"""
Database models for the harness app.

Holds the org-wide LLM provider configuration (M1), the permission
request flow (M3), and the persistent agent-harness session store
(M6). M6 models are purely additive: nothing from earlier milestones
is removed here (removal is M8 scope).

``PermissionRequest.session_id`` stays a plain UUID field: pointing it
at ``HarnessSession`` as an FK would create a hard model dependency
between the permissions subpackage and the session tables, and would
force every permission test to create a full workspace/session graph.
The plain UUID keeps permission lifecycle independent while still
identifying the owning harness session.
"""

from __future__ import annotations

import uuid

from django.db import models

from .permissions.models import PermissionAllowlist, PermissionRequest

__all__ = [
    "HarnessMessage",
    "HarnessPart",
    "HarnessSession",
    "PermissionAllowlist",
    "PermissionRequest",
    "ProviderConfig",
    "QuestionRequest",
    "Todo",
]


class HarnessSessionStatus(models.TextChoices):
    """Lifecycle states of a harness session."""

    BUSY = "busy", "Busy"
    IDLE = "idle", "Idle"


class HarnessSessionMode(models.TextChoices):
    """Agent execution mode for a harness session."""

    PLAN = "plan", "Plan"
    BUILD = "build", "Build"


class ProviderConfig(models.Model):
    """
    Org-wide LLM provider configuration.

    Exactly one record per organization (OneToOne). Stores the
    encrypted provider API key plus endpoint and model defaults.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="harness_provider_config",
        help_text="Owning organization (exactly one config per org).",
    )
    api_key_encrypted = models.TextField(
        help_text="Fernet-encrypted provider API key.",
    )
    base_url = models.URLField(
        max_length=1024,
        default="https://openrouter.ai/api/v1",
        help_text="OpenAI-compatible API base URL.",
    )
    default_model = models.CharField(
        max_length=255,
        default="",
        blank=True,
        help_text="Default model for agentic runs.",
    )
    small_model = models.CharField(
        max_length=255,
        default="",
        blank=True,
        help_text="Cheaper model for title/compaction tasks.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "harness_provider_config"
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        """Return a short representation of the config."""
        return f"ProviderConfig(org={self.organization_id})"


class HarnessSession(models.Model):
    """One persistent agent conversation bound to a workspace.

    Child sessions (subagents, M5) link to their parent via ``parent``.
    The owning workspace is referenced by string FK (``runners.Workspace``)
    so the harness app never imports the runners models at module level.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "runners.Workspace",
        on_delete=models.CASCADE,
        related_name="harness_sessions",
        help_text="Workspace this session runs in.",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
        help_text="Parent session for subagent runs (null for top-level).",
    )
    organization_id = models.UUIDField(
        help_text="Owning organization for scoping (mirrors workspace org).",
    )
    title = models.CharField(max_length=255, blank=True, default="")
    mode = models.CharField(
        max_length=16,
        choices=HarnessSessionMode.choices,
        default=HarnessSessionMode.BUILD,
    )
    agent_name = models.CharField(max_length=64, default="build")
    model = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=HarnessSessionStatus.choices,
        default=HarnessSessionStatus.IDLE,
    )
    cost = models.FloatField(default=0.0)
    tokens = models.JSONField(
        default=dict,
        blank=True,
        help_text="Aggregated token usage {prompt, completion, total}.",
    )
    skill_ids = models.JSONField(
        default=list,
        blank=True,
        help_text="Selected skill UUIDs persisted for subsequent runs.",
    )
    last_read_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the user last opened this session (null = never read).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "harness_session"
        ordering = ["created_at"]

    def __str__(self) -> str:
        """Return a short representation of the session."""
        return f"HarnessSession({str(self.id)[:8]}, {self.mode}, {self.status})"


class HarnessMessageRole(models.TextChoices):
    """Roles of persisted harness messages."""

    USER = "user", "User"
    ASSISTANT = "assistant", "Assistant"


class HarnessMessage(models.Model):
    """One user prompt or assistant answer inside a harness session.

    The assistant message acts as the shell for streamed parts:
    ``HarnessRunner`` events append deltas to its child ``HarnessPart``
    rows and accumulate cost/tokens here.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        HarnessSession,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    role = models.CharField(max_length=16, choices=HarnessMessageRole.choices)
    content = models.TextField(blank=True, default="")
    model = models.CharField(max_length=255, blank=True, default="")
    provider = models.CharField(max_length=64, blank=True, default="")
    cost = models.FloatField(default=0.0)
    tokens = models.JSONField(
        default=dict,
        blank=True,
        help_text="Usage for this message {prompt, completion, total}.",
    )
    finish = models.CharField(max_length=32, blank=True, default="")
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "harness_message"
        ordering = ["created_at"]

    def __str__(self) -> str:
        """Return a short representation of the message."""
        return f"HarnessMessage({self.role}, session={str(self.session_id)[:8]})"


class HarnessPartType(models.TextChoices):
    """Block types of a persisted harness part."""

    TEXT = "text", "Text"
    REASONING = "reasoning", "Reasoning"
    TOOL = "tool", "Tool"
    STEP_START = "step-start", "Step start"
    STEP_FINISH = "step-finish", "Step finish"
    SUBTASK = "subtask", "Subtask"
    PATCH = "patch", "Patch"
    AGENT = "agent", "Agent"


class HarnessPartState(models.TextChoices):
    """Lifecycle states of a persisted harness part."""

    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    ERROR = "error", "Error"


class HarnessPart(models.Model):
    """One streamed block (text/tool/step/subtask) of an assistant message."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(
        HarnessMessage,
        on_delete=models.CASCADE,
        related_name="parts",
    )
    type = models.CharField(max_length=16, choices=HarnessPartType.choices)
    state = models.CharField(
        max_length=16,
        choices=HarnessPartState.choices,
        default=HarnessPartState.PENDING,
    )
    call_id = models.CharField(max_length=255, blank=True, default="")
    input = models.JSONField(default=dict, blank=True)
    output = models.TextField(blank=True, default="")
    title = models.CharField(max_length=512, blank=True, default="")
    meta = models.JSONField(
        default=dict,
        blank=True,
        help_text="Extra payload (step number, cost, tokens, subtask id).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "harness_part"
        ordering = ["created_at"]

    def __str__(self) -> str:
        """Return a short representation of the part."""
        return f"HarnessPart({self.type}, {self.state})"


class QuestionRequestStatus(models.TextChoices):
    """Lifecycle states of a harness question request."""

    PENDING = "pending", "Pending"
    ANSWERED = "answered", "Answered"
    REJECTED = "rejected", "Rejected"
    TIMED_OUT = "timed_out", "Timed out"


class QuestionRequest(models.Model):
    """A structured question waiting for (or resolved by) user answers."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization_id = models.UUIDField(
        help_text="Owning organization (scoping, no FK to avoid cycles).",
    )
    workspace_id = models.UUIDField(null=True, blank=True)
    session_id = models.UUIDField(help_text="Harness session UUID.")
    message_id = models.UUIDField(null=True, blank=True)
    call_id = models.CharField(max_length=255, blank=True, default="")
    questions = models.JSONField(
        default=list,
        help_text="Structured question schema shown to the user.",
    )
    answers = models.JSONField(
        default=list,
        blank=True,
        help_text="User-provided answers (list aligned with questions).",
    )
    status = models.CharField(
        max_length=16,
        choices=QuestionRequestStatus.choices,
        default=QuestionRequestStatus.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "harness_question_request"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        """Return a short representation of the question request."""
        return f"QuestionRequest({self.status}, session={str(self.session_id)[:8]})"


class TodoStatus(models.TextChoices):
    """Lifecycle states of a session todo."""

    PENDING = "pending", "Pending"
    IN_PROGRESS = "in_progress", "In progress"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


class Todo(models.Model):
    """One persistent todo entry of a harness session (M5/M6)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        HarnessSession,
        on_delete=models.CASCADE,
        related_name="todos",
    )
    content = models.TextField()
    status = models.CharField(
        max_length=16,
        choices=TodoStatus.choices,
        default=TodoStatus.PENDING,
    )
    priority = models.CharField(max_length=16, blank=True, default="medium")
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "harness_todo"
        ordering = ["session", "order", "created_at"]

    def __str__(self) -> str:
        """Return a short representation of the todo."""
        return f"Todo({self.status}, {self.content[:32]})"
