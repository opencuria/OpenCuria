"""REST adapter for harness-generated commit subjects.

The harness owns model invocation; runners provides workspace authorization
and git execution. URL composition mounts this router under the existing
workspace git path without reversing that dependency direction.
"""

from __future__ import annotations

import uuid

from asgiref.sync import sync_to_async
from django.http import HttpRequest
from ninja import Router

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from apps.runners.git_presenters import git_error_to_response
from apps.runners.services import get_runner_service
from common.request_context import get_organization_id
from common.schemas import ErrorOut

from .commit_message_schemas import GitCommitMessageIn, GitCommitMessageOut
from .harness_service import get_harness_service

commit_message_router = Router(tags=["workspaces-git"])


@commit_message_router.post(
    "/{workspace_id}/git/commit-message/",
    response={
        200: GitCommitMessageOut,
        400: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
        502: ErrorOut,
        504: ErrorOut,
    },
    operation_id="apps_runners_api_git_commit_message",
    summary="Suggest a commit subject with the small model",
)
async def git_commit_message(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    payload: GitCommitMessageIn,
) -> tuple[int, GitCommitMessageOut | ErrorOut]:
    """Suggest a commit subject for the staged (else all) changes of one repo.

    Requires ``workspaces:git_read`` (reads the diff) and ``harness:run``
    (prompts the organization's small model).
    """
    for permission in (
        APIKeyPermission.WORKSPACES_GIT_READ,
        APIKeyPermission.HARNESS_RUN,
    ):
        if not check_api_key_permission(request, permission):
            return 403, ErrorOut(
                detail=f"API key lacks permission: {permission.value}",
                code="permission_denied",
            )
    org_id = get_organization_id(request)
    await sync_to_async(OrganizationService().require_membership)(request.user, org_id)

    try:
        await sync_to_async(get_runner_service().get_workspace_for_user)(
            workspace_id, user=request.user, organization_id=org_id
        )
        message = await get_harness_service().generate_commit_message(
            workspace_id=workspace_id,
            repo_path=payload.repo_path,
            organization_id=org_id,
            user_id=request.user.id,
        )
        return 200, GitCommitMessageOut(message=message)
    except Exception as exc:
        return git_error_to_response(exc)
