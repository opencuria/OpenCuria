"""Membership-aware profile presentation at the existing authentication URL.

Organizations compose account identity with memberships; accounts never
depends on organizations. The URL composition root mounts this router at
``/auth/`` alongside the account authentication routes.
"""

from __future__ import annotations

from django.http import HttpRequest
from ninja import Router

from .schemas import UserOrgOut, UserWithOrgsOut
from .services import OrganizationService

member_profile_router = Router(tags=["auth"])


@member_profile_router.get(
    "/me/",
    response=UserWithOrgsOut,
    summary="Get current user",
    # Keep the published OpenAPI identifier independent of the module move.
    operation_id="apps_accounts_api_me",
)
def me(request: HttpRequest) -> UserWithOrgsOut:
    """Return the authenticated user's profile with organization memberships."""
    user = request.user
    org_list = OrganizationService().list_user_organizations(user)

    return UserWithOrgsOut(
        id=user.id,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        organizations=[
            UserOrgOut(
                id=o["id"],
                name=o["name"],
                slug=o["slug"],
                role=o["role"],
                created_at=o["created_at"],
            )
            for o in org_list
        ],
    )
