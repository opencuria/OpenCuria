"""In-memory desktop session state (Phase 2 infra mixin).

Generation-fenced routing state shared by lifecycle events and heartbeats.
The backing dicts ``_active_desktops`` / ``_desktop_workspace_runner`` intentionally
remain class attributes on the ``RunnerService`` facade (tests read and
write them directly, e.g. ``service._active_desktops[...] = ...``); this
mixin only accesses them via ``self``. Terminal session routing is stored
in the database through ``TerminalSessionRepository``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class SessionStoreMixin:
    """Desktop session cache shared by RunnerService."""

    def _set_runner_desktop_epoch(self, runner_id: str, epoch: str | None) -> None:
        """Set the active desktop epoch advertised by an authenticated runner."""
        if not runner_id or not epoch:
            return
        epochs = getattr(self, "_runner_desktop_epochs", None)
        if epochs is None:
            self._runner_desktop_epochs = epochs = {}
        epochs[runner_id] = epoch

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
            "mcp": bool(
                desktop_state.get("mcp", desktop_state.get("mcp_active", False))
            ),
            "holder_count": int(desktop_state.get("holder_count", 0)),
            **{
                key: desktop_state[key]
                for key in ("generation", "epoch")
                if key in desktop_state
            },
        }

    def _desktop_public_summary(self, workspace_id: str) -> dict:
        """Expose aggregate process state, never individual lease identities."""
        state = self.get_desktop_info(workspace_id) or {}
        return {
            "viewer_held": bool(state.get("viewer")),
            "mcp_active": bool(state.get("mcp")),
            "holder_count": int(state.get("holder_count", 0)),
            **{key: state[key] for key in ("generation", "epoch") if key in state},
        }

    def _inherit_current_desktop_version(
        self,
        workspace_id: str,
        *,
        runner_id: str | None,
        epoch: str | None,
        generation: int | None,
    ) -> tuple[str | None, int | None]:
        """Use cached fencing metadata for a partial event from its current owner."""
        current = self._active_desktops.get(workspace_id) or {}
        current_runner = self._desktop_workspace_runner.get(workspace_id)
        owner_id = runner_id or current_runner
        if not current or current_runner != owner_id:
            return epoch, generation
        return (
            epoch if epoch is not None else current.get("epoch"),
            generation if generation is not None else current.get("generation"),
        )

    def _record_active_desktop(
        self,
        workspace_id: str,
        desktop_state: dict,
        *,
        runner_id: str | None = None,
    ) -> bool:
        """Persist backend desktop state unless it predates a fenced update."""
        normalized = self._normalize_desktop_state(desktop_state)
        current = self._active_desktops.get(workspace_id)
        current_runner = self._desktop_workspace_runner.get(workspace_id)
        owner_id = runner_id or current_runner
        if current and current_runner == owner_id:
            # Partial lifecycle events update only supplied aggregates. Keep the
            # current incarnation and process-wide state when a field is omitted.
            for key in (
                "generation",
                "epoch",
                "viewer",
                "computer_use",
                "holder_count",
            ):
                if key not in desktop_state and key in current:
                    normalized[key] = current[key]
            if "mcp" not in desktop_state and "mcp_active" not in desktop_state:
                if "mcp" in current:
                    normalized["mcp"] = current["mcp"]
        if self._desktop_state_is_stale(
            workspace_id,
            epoch=normalized.get("epoch"),
            generation=normalized.get("generation"),
            runner_id=owner_id,
        ):
            return False
        if normalized.get("epoch") is None:
            normalized.pop("epoch", None)
        self._active_desktops[workspace_id] = normalized
        if owner_id:
            self._desktop_workspace_runner[workspace_id] = owner_id
        self._remember_desktop_state(
            workspace_id,
            epoch=normalized.get("epoch"),
            generation=normalized.get("generation"),
            runner_id=owner_id,
            active=True,
        )
        return True

    def _desktop_state_is_stale(
        self,
        workspace_id: str,
        *,
        epoch: str | None,
        generation: int | None,
        runner_id: str | None,
        active: bool = True,
    ) -> bool:
        """Fence stale updates by runner epoch, generation, and stopped state."""
        known = getattr(self, "_desktop_state_versions", {}).get(workspace_id)
        if epoch is None:
            current = self._active_desktops.get(workspace_id) or {}
            current_runner = self._desktop_workspace_runner.get(workspace_id)
            if active and current and current_runner == runner_id:
                # A legacy active event from the cached owner inherits this
                # incarnation below; stopped tombstones remain a hard fence.
                return bool(known and known[0] == runner_id and not known[3])
            return bool(
                known
                and known[0] == runner_id
                or current_runner == runner_id
                and "generation" in current
            )
        expected_epoch = getattr(self, "_runner_desktop_epochs", {}).get(runner_id)
        if expected_epoch is None:
            current = self._active_desktops.get(workspace_id) or {}
            if current.get("epoch") is not None:
                expected_epoch = current["epoch"]
        if expected_epoch is not None and epoch != expected_epoch:
            return True
        if expected_epoch is None and known is not None and epoch != known[1]:
            return True
        if known is None:
            return False
        known_runner, known_epoch, known_generation, known_active = known
        if runner_id != known_runner:
            return False
        if epoch != known_epoch:
            # A registered epoch may replace state from its predecessor;
            # until one is registered (tests/legacy), only this latest epoch
            # may update the workspace.
            return expected_epoch != epoch if expected_epoch is not None else True
        if generation is None or type(generation) is not int:
            return True
        if generation < known_generation:
            return True
        # A stop at the current incarnation is authoritative and idempotent;
        # only a late active announcement may resurrect a stopped tombstone.
        return generation == known_generation and active and not known_active

    def _remember_desktop_state(
        self,
        workspace_id: str,
        *,
        epoch: str | None,
        generation: int | None,
        runner_id: str | None,
        active: bool,
    ) -> None:
        """Remember the latest desktop incarnation, including stopped tombstones."""
        if epoch is None or type(generation) is not int or runner_id is None:
            return
        versions = getattr(self, "_desktop_state_versions", None)
        if versions is None:
            self._desktop_state_versions = versions = {}
        current = versions.get(workspace_id)
        if current is None or current[0] != runner_id or current[1] != epoch:
            versions[workspace_id] = (runner_id, epoch, generation, active)
        elif generation > current[2] or generation == current[2] and not active:
            versions[workspace_id] = (runner_id, epoch, generation, active)
        elif generation == current[2] and active:
            versions[workspace_id] = (runner_id, epoch, generation, True)

    def _sync_desktop_state_from_heartbeat(
        self,
        workspace_id: str,
        desktop_state: dict | None,
        *,
        runner_id: str,
        desktop_epoch: str | None = None,
        desktop_generation: int | None = None,
        desktop_session_tracked: bool | None = None,
    ) -> None:
        """Reconcile cached desktop state from a generation-fenced heartbeat."""
        if not desktop_state:
            current = self._active_desktops.get(workspace_id) or {}
            if desktop_session_tracked is None:
                # Compatibility for old heartbeat shape: it cannot distinguish
                # an ended desktop from a failed liveness probe for a cached one.
                if current.get("epoch") is not None:
                    return
                self._cleanup_desktop_state(workspace_id)
                return
            if desktop_session_tracked:
                # Null plus a tracked live session means the liveness probe failed;
                # keep its route and avoid writing a stopped tombstone.
                return
            if self._desktop_state_is_stale(
                workspace_id,
                epoch=desktop_epoch,
                generation=desktop_generation,
                runner_id=runner_id,
                active=False,
            ):
                return
            self._remember_desktop_state(
                workspace_id,
                epoch=desktop_epoch,
                generation=desktop_generation,
                runner_id=runner_id,
                active=False,
            )
            self._cleanup_desktop_state(workspace_id)
            return

        normalized = self._normalize_desktop_state(desktop_state)
        if desktop_generation is not None and self._desktop_state_is_stale(
            workspace_id,
            epoch=desktop_epoch,
            generation=desktop_generation,
            runner_id=runner_id,
        ):
            return
        if normalized.get("epoch") is None and desktop_epoch is not None:
            normalized["epoch"] = desktop_epoch
        if normalized.get("generation") is None and desktop_generation is not None:
            normalized["generation"] = desktop_generation
        if self._desktop_state_is_stale(
            workspace_id,
            epoch=normalized.get("epoch"),
            generation=normalized.get("generation"),
            runner_id=runner_id,
        ):
            return
        self._record_active_desktop(
            workspace_id,
            normalized,
            runner_id=runner_id,
        )

    def _cleanup_desktop_state(self, workspace_id: str) -> None:
        """Remove in-memory desktop state for a workspace."""
        self._active_desktops.pop(workspace_id, None)
        self._desktop_workspace_runner.pop(workspace_id, None)
