"""Generation-fenced desktop routing shared by lifecycle events and heartbeats."""

from __future__ import annotations


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
        """Select the current runner's complete desktop state for the cache."""
        return {
            key: desktop_state[key]
            for key in (
                "port",
                "guest_ip",
                "viewer",
                "computer_use",
                "mcp",
                "holder_count",
                "generation",
                "epoch",
            )
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

    def _record_active_desktop(
        self,
        workspace_id: str,
        desktop_state: dict,
        *,
        runner_id: str,
    ) -> bool:
        """Persist backend desktop state unless it predates a fenced update."""
        normalized = self._normalize_desktop_state(desktop_state)
        if self._desktop_state_is_stale(
            workspace_id,
            epoch=normalized["epoch"],
            generation=normalized["generation"],
            runner_id=runner_id,
        ):
            return False
        self._active_desktops[workspace_id] = normalized
        self._desktop_workspace_runner[workspace_id] = runner_id
        self._remember_desktop_state(
            workspace_id,
            epoch=normalized["epoch"],
            generation=normalized["generation"],
            runner_id=runner_id,
            active=True,
        )
        return True

    def _desktop_state_is_stale(
        self,
        workspace_id: str,
        *,
        epoch: str,
        generation: int,
        runner_id: str,
        active: bool = True,
    ) -> bool:
        """Fence stale updates by runner epoch, generation, and stopped state."""
        expected_epoch = getattr(self, "_runner_desktop_epochs", {}).get(runner_id)
        if (
            not epoch
            or epoch != expected_epoch
            or type(generation) is not int
            or generation < 0
        ):
            return True
        known = getattr(self, "_desktop_state_versions", {}).get(workspace_id)
        if known is None:
            return False
        known_runner, known_epoch, known_generation, known_active = known
        if runner_id != known_runner:
            return False
        if epoch != known_epoch:
            return False
        if generation < known_generation:
            return True
        # A stop at the current incarnation is authoritative and idempotent;
        # a late active announcement must never resurrect a stopped tombstone.
        return generation == known_generation and active and not known_active

    def _remember_desktop_state(
        self,
        workspace_id: str,
        *,
        epoch: str,
        generation: int,
        runner_id: str,
        active: bool,
    ) -> None:
        """Remember the latest desktop incarnation, including stopped tombstones."""
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
        desktop_epoch: str,
        desktop_generation: int,
        desktop_session_tracked: bool,
    ) -> None:
        """Reconcile cached desktop state from a generation-fenced heartbeat."""
        if not desktop_state and desktop_session_tracked:
            # A failed liveness probe cannot prove that a tracked desktop stopped.
            return
        if self._desktop_state_is_stale(
            workspace_id,
            epoch=desktop_epoch,
            generation=desktop_generation,
            runner_id=runner_id,
            active=bool(desktop_state),
        ):
            return
        if not desktop_state:
            self._remember_desktop_state(
                workspace_id,
                epoch=desktop_epoch,
                generation=desktop_generation,
                runner_id=runner_id,
                active=False,
            )
            self._cleanup_desktop_state(workspace_id)
            return

        self._record_active_desktop(
            workspace_id,
            desktop_state,
            runner_id=runner_id,
        )

    def _cleanup_desktop_state(self, workspace_id: str) -> None:
        """Remove in-memory desktop state for a workspace."""
        self._active_desktops.pop(workspace_id, None)
        self._desktop_workspace_runner.pop(workspace_id, None)
