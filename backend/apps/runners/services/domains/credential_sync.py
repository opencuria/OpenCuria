"""Workspace credential resolution and sync onto running workspaces.

This mixin is the runner service's only path into the credentials context:
lifecycle flows resolve a requested credential selection through
:meth:`CredentialSyncMixin._resolve_credential_selection` and the
credentials attached to a workspace through
:meth:`CredentialSyncMixin._credential_resolve_call`. Adapters (REST, MCP)
pass credential IDs only and never handle resolved secrets.

The mixin relies on the facade (``RunnerService``) providing
``self.workspaces``, ``self.tasks``, ``self._pending_credential_inject``
plus sibling helpers (``_call_runner`` / ``_emit_to_runner`` via
``RunnerTransportMixin``, ``_forward_workspace_status`` via
``FrontendBusMixin``, ``_validate_task_runner`` via ``OwnershipMixin``).
Attached-credential resolution goes through ``self._credential_resolver``
when the facade was constructed with one (injected port, see
``services/ports.py``); otherwise ``CredentialSvc`` is used.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from apps.credentials.services import CredentialSvc, ResolvedCredentials
from common.exceptions import ConflictError
from common.utils import generate_uuid

from ...enums import TaskStatus, TaskType, WorkspaceStatus
from ...exceptions import RunnerOfflineError

if TYPE_CHECKING:
    from ...models import Workspace

logger = logging.getLogger(__name__)

#: ACK timeout for blocking credential-inject calls (seconds).
_CREDENTIAL_INJECT_TIMEOUT_SECONDS = 30


class CredentialSyncMixin:
    """Credential inject/reconcile flows shared by RunnerService."""

    async def _resolve_credential_selection(
        self,
        credential_ids: list[uuid.UUID],
        *,
        user,
        organization_id: uuid.UUID | None,
    ) -> ResolvedCredentials:
        """Resolve credentials requested for a workspace on behalf of *user*.

        Validates visibility, ownership, OAuth bindings and per-service
        uniqueness before any workspace state changes.

        Raises:
            ConflictError: When the user or organization is missing, or the
                selection violates a credential rule.
            NotFoundError: When a credential is missing or not visible.
        """
        if user is None or organization_id is None:
            raise ConflictError(
                "User and organization are required to resolve workspace credentials"
            )
        return await sync_to_async(CredentialSvc().resolve_credentials)(
            list(credential_ids), org_id=organization_id, user=user
        )

    def _credential_resolve_call(self, workspace):
        """Return the sync ``resolve_workspace_credentials`` callable to use.

        When the facade was constructed with ``credential_resolver=...``,
        its bound ``resolve_workspace_credentials`` method is used;
        otherwise ``CredentialSvc``'s. Callers wrap the result with
        ``sync_to_async``.
        """
        resolver = getattr(self, "_credential_resolver", None)
        if resolver is not None:
            return resolver.resolve_workspace_credentials
        return CredentialSvc().resolve_workspace_credentials

    @staticmethod
    def _has_credential_material(resolved: ResolvedCredentials | None) -> bool:
        """Return whether resolved credentials write anything into a workspace."""
        return resolved is not None and bool(
            resolved.env_vars or resolved.files or resolved.ssh_keys
        )

    @staticmethod
    def _resolved_credentials_payload(resolved) -> dict:
        """Serialize resolved credentials for a runner task payload."""
        return {
            "env_vars": dict(getattr(resolved, "env_vars", None) or {}),
            "files": [
                {
                    "target_path": file.target_path,
                    "content": file.content,
                    "mode": file.mode,
                }
                for file in (getattr(resolved, "files", None) or [])
            ],
            "ssh_keys": list(getattr(resolved, "ssh_keys", None) or []),
        }

    async def _dispatch_credential_inject(
        self,
        workspace: Workspace,
        *,
        resolved: ResolvedCredentials | None = None,
        wait: bool = False,
    ) -> bool | None:
        """Replace persisted credentials on a running workspace.

        Args:
            workspace: Workspace whose on-disk secrets should match the
                desired attachment.
            resolved: Pre-resolved secrets. When omitted, secrets are loaded
                from the current workspace attachment (heartbeat path).
            wait: When True, wait for the runner acknowledgement and return
                whether credential material is present on disk.

        Returns:
            Acknowledged state when waiting, otherwise ``None``.
        """
        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))
        if resolved is None:
            fresh = await sync_to_async(self.workspaces.get_by_id)(workspace.id)
            if fresh is None:
                return None
            # Heartbeat candidates may have been queued before capture acquired
            # its parent hold. Only ordinary reconciliation is fenced here;
            # lifecycle delivery supplies explicitly resolved credentials.
            if fresh.active_operation or fresh.current_task_id:
                return None
            workspace = fresh
            runner = workspace.runner
            resolved = await sync_to_async(self._credential_resolve_call(workspace))(
                workspace
            )

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.INJECT_CREDENTIALS,
            workspace=workspace,
        )
        payload = {
            "task_id": str(task_id),
            "workspace_id": str(workspace.id),
            **self._resolved_credentials_payload(resolved),
        }
        try:
            if wait:
                await sync_to_async(self.tasks.mark_in_progress)(task)
                response = await self._call_runner(
                    runner,
                    "task:inject_credentials",
                    payload,
                    timeout=_CREDENTIAL_INJECT_TIMEOUT_SECONDS,
                )
                # An ACK is another delivery of the exact journal result. Use
                # the common callback boundary; never complete an unbound task
                # or infer credential state from RPC success alone.
                identity = response.get("_operation_identity")
                acknowledged_present = response.get("credentials_present")
                accepted = False
                if isinstance(identity, dict) and (
                    response.get("ok") is False
                    or isinstance(acknowledged_present, bool)
                ):
                    accepted = await sync_to_async(self.apply_operation_result)(
                        str(runner.id),
                        "workspace:error"
                        if response.get("ok") is False
                        else "workspace:credentials_injected",
                        {
                            "task_id": str(task.id),
                            "workspace_id": str(workspace.id),
                            "credentials_present": acknowledged_present,
                            "execution_finished": True,
                            "outcome_known": response.get("outcome_known", True)
                            if response.get("ok") is not False
                            else response.get("outcome_known", False),
                            "error": str(
                                response.get("error") or "Credential inject failed"
                            ),
                            **identity,
                        },
                    )
                if response.get("ok") is False:
                    raise ConflictError(
                        str(response.get("error") or "Credential inject failed")
                    )
                if accepted and isinstance(acknowledged_present, bool):
                    return acknowledged_present
                return None

            await self._emit_to_runner(runner, "task:inject_credentials", payload)
            await sync_to_async(self.tasks.mark_in_progress)(task)
            return None
        except Exception as exc:
            # The durable command keeps the workspace fence on transport loss.
            # A late secret write must settle or be sealed before capture/reset.
            # Delivery/recovery resolves the task after backend/runner restart.
            if wait:
                raise ConflictError(
                    f"Failed to apply credentials to the running workspace: {exc}"
                ) from exc
            raise

    async def dispatch_credential_reconcile(
        self, workspace_ids: list[uuid.UUID]
    ) -> None:
        """Apply desired credentials onto running workspaces (heartbeat)."""
        for workspace_id in workspace_ids:
            try:
                workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
                if workspace is None:
                    continue
                if workspace.status != WorkspaceStatus.RUNNING:
                    continue
                await self._dispatch_credential_inject(workspace, wait=False)
            except Exception:
                logger.exception(
                    "Failed to reconcile credentials for workspace %s",
                    workspace_id,
                )

    def handle_credentials_injected(
        self,
        task_id: str,
        workspace_id: str,
        credentials_present: bool,
        runner_id: str | None = None,
    ) -> None:
        """Handle workspace:credentials_injected event from a runner."""
        task = self.tasks.get_by_id(uuid.UUID(task_id))
        if task is None:
            logger.warning(
                "Received workspace:credentials_injected for unknown task: %s",
                task_id,
            )
            return

        if not self._validate_task_runner(task, runner_id):
            return

        workspace = task.workspace
        if (
            task.type != TaskType.INJECT_CREDENTIALS
            or str(task.workspace_id) != workspace_id
            or task.status not in {TaskStatus.PENDING, TaskStatus.IN_PROGRESS}
            or not isinstance(credentials_present, bool)
            or workspace is None
            or workspace.current_task_id != task.id
        ):
            return

        if workspace:
            self.workspaces.update_credentials_present(
                workspace, bool(credentials_present)
            )
            self._pending_credential_inject.discard(
                (str(workspace.runner_id), str(workspace.id))
            )
            self._forward_workspace_status(workspace, task_id=task_id)
        self.tasks.complete(task)
        logger.info(
            "Workspace credentials injected: %s present=%s",
            workspace_id,
            credentials_present,
        )
