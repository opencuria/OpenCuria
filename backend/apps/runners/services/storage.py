"""Runner storage inventory: runner scan intake and admin inspection.

Runners report scans over Socket.IO; REST exposes the admin views.
"""

from asgiref.sync import sync_to_async

from apps.organizations.services import OrganizationService
from common.exceptions import NotFoundError

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
            for activity in ActivityService.project(detail)
        ]
        # Internal workflow facts are used for grouping, not exposed as raw records.
        detail.pop("recreate_requests")
        detail.pop("deletions")
        return detail

    async def refresh(self, transport, user, organization_id, runner_id) -> dict:
        runner = await sync_to_async(self.authorized_runner)(
            user, organization_id, runner_id
        )
        return await sync_to_async(InventoryRepository.request_refresh)(runner)
