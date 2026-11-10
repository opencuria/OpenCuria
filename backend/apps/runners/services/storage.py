"""Runner storage inventory: runner scan intake and admin inspection.

Runners report scans over Socket.IO; REST exposes the admin views.
"""

from asgiref.sync import sync_to_async

from apps.organizations.services import OrganizationService
from common.exceptions import NotFoundError
from common.user_labels import private_workspace_name

from ..inventory_repository import InventoryRepository
from ..repositories import RunnerRepository
from .activity import ActivityService
from .inventory_inspection import InventoryInspectionService


class StorageService:
    """Read cached observations and explicitly request a fresh full scan."""

    @staticmethod
    def record_inventory(runner_id: str, session: str, scan: dict) -> bool:
        """Persist a runner scan unless its session or sequence is obsolete."""
        return InventoryRepository.record(runner_id, session, scan)

    @staticmethod
    def authorized_runner(user, organization_id, runner_id):
        OrganizationService().require_admin(user, organization_id)
        runner = RunnerRepository.get_by_id(runner_id)
        if runner is None or runner.organization_id != organization_id:
            raise NotFoundError("Runner", str(runner_id))
        return runner

    def detail(self, user, organization_id, runner_id) -> dict:
        runner = self.authorized_runner(user, organization_id, runner_id)
        detail = InventoryInspectionService.inspect(runner)
        detail["activities"] = [
            activity.model_dump(mode="json")
            for activity in ActivityService.project(detail, viewer_id=user.id)
        ]
        # Internal workflow facts are used for grouping, not exposed as raw records.
        detail.pop("recreate_requests")
        detail.pop("deletions")
        self._hide_foreign_workspace_names(detail, viewer_id=user.id)
        return detail

    @staticmethod
    def _hide_foreign_workspace_names(detail: dict, *, viewer_id: int) -> None:
        """Keep workspace names only for their owner; admins see owner labels."""
        owners = detail.pop("workspace_owners")

        def visible(workspace_id, name: str | None) -> str:
            owner_id = owners.get(str(workspace_id), {}).get("owner_id")
            return private_workspace_name(name, owner_id=owner_id, viewer_id=viewer_id)

        for runtime in detail["runtimes"]:
            for resource in runtime["resources"]:
                if resource["workspace"]:
                    workspace = resource["workspace"]
                    workspace["name"] = visible(workspace["id"], workspace["name"])
        for generation in detail["generations"]:
            for workspace in generation["dependencies"]:
                workspace["name"] = visible(workspace["id"], workspace["name"])
        for request in detail["capture_requests"]:
            request.pop("workspace__name", None)
        for operation in detail["operations"]:
            operation["task__workspace__name"] = (
                visible(
                    operation["task__workspace_id"], operation["task__workspace__name"]
                )
                or None
            )

    async def refresh(self, transport, user, organization_id, runner_id) -> dict:
        runner = await sync_to_async(self.authorized_runner)(
            user, organization_id, runner_id
        )
        return await sync_to_async(InventoryRepository.request_refresh)(runner)
