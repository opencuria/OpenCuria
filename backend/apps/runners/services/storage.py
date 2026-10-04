"""Organization-admin storage inspection shared by REST and MCP."""
from asgiref.sync import sync_to_async

from apps.organizations.services import OrganizationService
from common.exceptions import NotFoundError
from ..inventory_repository import InventoryRepository
from ..repositories import RunnerRepository


class StorageService:
    """Read cached observations and explicitly request a fresh full scan."""

    @staticmethod
    def authorized_runner(user, organization_id, runner_id):
        OrganizationService().require_admin(user, organization_id)
        runner = RunnerRepository.get_by_id(runner_id)
        if runner is None or runner.organization_id != organization_id:
            raise NotFoundError('Runner', str(runner_id))
        return runner

    def detail(self, user, organization_id, runner_id) -> dict:
        runner = self.authorized_runner(user, organization_id, runner_id)
        return InventoryRepository.detail(runner)

    async def refresh(self, transport, user, organization_id, runner_id) -> dict:
        runner = await sync_to_async(self.authorized_runner)(
            user, organization_id, runner_id)
        return await sync_to_async(InventoryRepository.request_refresh)(runner)
