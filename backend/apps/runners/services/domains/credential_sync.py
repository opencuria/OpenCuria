"""Credential sync onto running workspaces (Phase 3 domain mixin).

Byte-identical method bodies extracted from the former monolithic
``apps/runners/services/__init__.py``. No logic changes; the mixin relies
on the facade (``RunnerService``) providing ``self.workspaces``,
``self.tasks``, ``self._pending_credential_inject`` plus sibling helpers
(``_call_runner`` / ``_emit_to_runner`` via ``RunnerTransportMixin``,
``_forward_workspace_status`` via ``FrontendBusMixin``,
``_validate_task_runner`` via ``OwnershipMixin``). Phase 4: the credential
resolution goes through ``self._credential_resolver`` when the facade was
constructed with one (injected port, see ``services/ports.py``); otherwise
the previous ``CredentialSvc()`` path is kept as fallback, so the
top-level ``apps.credentials`` import stays (fallback coupling only).
"""

from __future__ import annotations

import logging
import uuid

from asgiref.sync import sync_to_async

from apps.credentials.services import ResolvedCredentials
from common.exceptions import ConflictError
from common.utils import generate_uuid

from ...enums import TaskStatus, TaskType, WorkspaceStatus
from ...exceptions import RunnerOfflineError

logger = logging.getLogger(__name__)

#: ACK timeout for blocking credential-inject calls (seconds).
_CREDENTIAL_INJECT_TIMEOUT_SECONDS = 30


class CredentialSyncMixin:
    """Credential inject/reconcile flows shared by RunnerService."""

    def _credential_resolve_call(self, workspace):
        """Return the sync ``resolve_workspace_credentials`` callable to use.

        Phase-4 port: when the facade was constructed with
        ``credential_resolver=...``, its bound
        ``resolve_workspace_credentials`` method is used; otherwise the
        legacy ``CredentialSvc()`` fallback applies. Callers wrap the
        result with ``sync_to_async`` exactly as before.
        """
        resolver = getattr(self, "_credential_resolver", None)
        if resolver is not None:
            return resolver.resolve_workspace_credentials
        from apps.credentials.services import CredentialSvc

        return CredentialSvc().resolve_workspace_credentials

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
        workspace: "Workspace",
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
            ``credentials_present`` when *wait* is True, otherwise ``None``.
        """
        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))
        key = (str(runner.id), str(workspace.id))

        if resolved is None:
            fresh = await sync_to_async(self.workspaces.get_by_id)(workspace.id)
            if fresh is None:
                return None
            workspace = fresh
            runner = workspace.runner
            resolved = await sync_to_async(self._credential_resolve_call(workspace))(
                workspace
            )

        if not wait and key in self._pending_credential_inject:
            return None

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.INJECT_CREDENTIALS,
            workspace=workspace,
        )
        self._pending_credential_inject.add(key)
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
                if response.get("ok") is False:
                    error = str(response.get("error") or "Credential inject failed")
                    self._pending_credential_inject.discard(key)
                    await sync_to_async(self.tasks.fail)(task, error)
                    raise ConflictError(error)
                if "credentials_present" in response:
                    credentials_present = bool(response["credentials_present"])
                else:
                    secrets = self._resolved_credentials_payload(resolved)
                    credentials_present = bool(
                        secrets["env_vars"]
                        or secrets["files"]
                        or secrets["ssh_keys"]
                    )
                workspace = await sync_to_async(
                    self.workspaces.update_credentials_present
                )(workspace, credentials_present)
                await sync_to_async(self.tasks.complete)(task)
                self._pending_credential_inject.discard(key)
                self._forward_workspace_status(workspace, task_id=str(task_id))
                return credentials_present

            await self._emit_to_runner(
                runner,
                "task:inject_credentials",
                payload,
            )
            await sync_to_async(self.tasks.mark_in_progress)(task)
            return None
        except ConflictError:
            raise
        except Exception as exc:
            self._pending_credential_inject.discard(key)
            await sync_to_async(self.tasks.fail)(task, str(exc))
            if wait:
                raise ConflictError(
                    "Failed to apply credentials to the running workspace: "
                    f"{exc}"
                ) from exc
            raise

    async def dispatch_credential_reconcile(
        self, workspace_ids: list[uuid.UUID]
    ) -> None:
        """Apply desired credentials onto running workspaces (heartbeat)."""
        for workspace_id in workspace_ids:
            try:
                workspace = await sync_to_async(self.workspaces.get_by_id)(
                    workspace_id
                )
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
        if task.status == TaskStatus.COMPLETED:
            if workspace:
                self._pending_credential_inject.discard(
                    (str(workspace.runner_id), str(workspace.id))
                )
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
