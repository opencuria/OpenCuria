"""Shared authorization boundary for REST and MCP deletion commands."""

from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError, ConflictError, NotFoundError

from ..deletion_repository import DeletionRepository
from ..image_lines import ImageLineRepository


class DeletionService:
    """Owners may defer their captures; only org admins approve cascades."""

    @staticmethod
    def authorize(user, org_id, kind, target_id, force=False):
        orgs = OrganizationService()
        orgs.require_membership(user, org_id)
        target, _, _ = DeletionRepository.target(org_id, kind, target_id)
        admin = orgs.get_user_role(user, org_id) in ["owner", "admin"]
        if force or kind not in {"image", "captured_image"}:
            orgs.require_admin(user, org_id)
        elif not admin and (
            target.created_by_id != user.id
            or (kind == "image" and target.origin_type != "workspace_capture")
        ):
            raise AuthenticationError(
                "Only the capture owner or organization admin may delete this image"
            )
        return target

    def preview(self, user, org_id, kind, target_id):
        self.authorize(user, org_id, kind, target_id, force=True)
        return DeletionRepository.preview(org_id, kind, target_id)

    def request(self, user, org_id, kind, target_id, mode="deferred", fingerprint=""):
        target = self.authorize(user, org_id, kind, target_id, force=mode == "force")
        if kind == "image" and target.captured_image_id:
            latest = ImageLineRepository.latest_for(target)
            if latest is not None and latest.id == target.id:
                raise ConflictError(
                    "The latest version can only be removed by deleting the image"
                )
        return DeletionRepository.request(
            org_id, user, kind, target_id, mode, fingerprint
        )

    def list(self, user, org_id, request_id=None):
        OrganizationService().require_membership(user, org_id)
        rows = DeletionRepository.list(org_id, request_id)
        # Listing another owner's request requires the same target permission.
        visible = []
        for row in rows:
            try:
                self.authorize(user, org_id, row["target_type"], row["target_id"])
            except AuthenticationError:
                continue
            visible.append(row)
        if request_id and not visible:
            raise NotFoundError("Deletion request", str(request_id))
        return visible

    def cancel(self, user, org_id, request_id):
        row = self.list(user, org_id, request_id)[0]
        self.authorize(user, org_id, row["target_type"], row["target_id"])
        return DeletionRepository.cancel(org_id, request_id)
