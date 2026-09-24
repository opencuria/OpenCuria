"""In-memory desktop session state (Phase 2 infra mixin).

Byte-identical method bodies extracted from the former monolithic
``apps/runners/services/__init__.py``. No logic changes. The backing
dicts ``_active_desktops`` / ``_desktop_workspace_runner`` intentionally
remain class attributes on the ``RunnerService`` facade (tests read and
write them directly, e.g. ``service._active_desktops[...] = ...``); this
mixin only accesses them via ``self``. ``_active_terminals`` /
``_terminal_workspace_runner`` likewise stay on the facade -- they belong
to the Phase 3 ``interactive_sessions`` extraction.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class SessionStoreMixin:
    """Desktop session cache shared by RunnerService."""

    def is_desktop_active(self, workspace_id: str) -> bool:
        """Check if a desktop session is active for a workspace."""
        return workspace_id in self._active_desktops

    def get_desktop_info(self, workspace_id: str) -> dict | None:
        """Get cached desktop session info if the runner reported one active."""
        return self._active_desktops.get(workspace_id)

    @staticmethod
    def _normalize_desktop_state(desktop_state: dict) -> dict:
        """Normalize runner desktop payloads to the backend cache shape."""
        return {
            "port": desktop_state.get("port", 6901),
            "container_ip": desktop_state.get("container_ip", ""),
            "network_name": desktop_state.get("network_name", ""),
            "viewer": bool(desktop_state.get("viewer", False)),
            "computer_use": bool(desktop_state.get("computer_use", False)),
        }

    def _record_active_desktop(
        self,
        workspace_id: str,
        desktop_state: dict,
        *,
        runner_id: str | None = None,
    ) -> None:
        """Persist backend desktop state without performing network I/O."""
        self._active_desktops[workspace_id] = self._normalize_desktop_state(
            desktop_state
        )
        if runner_id:
            self._desktop_workspace_runner[workspace_id] = runner_id

    def _sync_desktop_state_from_heartbeat(
        self,
        workspace_id: str,
        desktop_state: dict | None,
        *,
        runner_id: str,
    ) -> None:
        """Reconcile cached desktop state from runner heartbeats."""
        if not desktop_state:
            self._cleanup_desktop_state(workspace_id)
            return

        normalized = self._normalize_desktop_state(desktop_state)
        current = self._active_desktops.get(workspace_id)
        current_runner = self._desktop_workspace_runner.get(workspace_id)
        if current == normalized and current_runner == runner_id:
            return

        self._cleanup_desktop_state(workspace_id)
        self._record_active_desktop(
            workspace_id,
            normalized,
            runner_id=runner_id,
        )

    def _cleanup_desktop_state(self, workspace_id: str) -> None:
        """Remove in-memory desktop state for a workspace."""
        self._active_desktops.pop(workspace_id, None)
        self._desktop_workspace_runner.pop(workspace_id, None)
