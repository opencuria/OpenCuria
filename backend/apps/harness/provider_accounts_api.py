"""REST adapter for provider-account management and device OAuth.

Services own account policy and mutations. This adapter only checks transport
permissions, resolves organization context and maps results to HTTP schemas.
"""

from __future__ import annotations

import uuid

from asgiref.sync import sync_to_async
from django.http import HttpRequest
from ninja import Router

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from common.api_errors import api_key_permission_denied
from common.exceptions import AuthenticationError, NotFoundError
from common.request_context import get_organization_id

from .provider_account_management_service import ProviderAccountManagementService
from .provider_accounts import ProviderAccountService
from .provider_oauth import ProviderOAuthService
from .providers.base import ProviderError
from .schemas import (
    ChatGPTOAuthCancelIn,
    ChatGPTOAuthStartIn,
    ChatGPTOAuthStartOut,
    ChatGPTOAuthStatusOut,
    ProviderAccountIn,
    ProviderAccountOut,
    ProviderSelectionIn,
)

provider_accounts_router = Router(tags=["harness"])

# Keep the published operation IDs stable when moving routes between adapters.


@provider_accounts_router.get(
    "/provider-config/accounts/",
    response={200: list[ProviderAccountOut], 401: dict, 403: dict, 404: dict},
    summary="List visible connected provider accounts",
    operation_id="apps_harness_api_list_provider_accounts",
)
def list_provider_accounts(request: HttpRequest):
    """List connected private/shared accounts visible to the current user."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return api_key_permission_denied(APIKeyPermission.HARNESS_PROVIDERS.value)
    try:
        org_id = get_organization_id(request)
        OrganizationService().require_membership(request.user, org_id)
        rows = ProviderAccountManagementService().list_connected_accounts(
            org_id, int(request.user.id)
        )
        return 200, [ProviderAccountOut(**row) for row in rows]
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@provider_accounts_router.post(
    "/provider-config/accounts/{provider}/",
    response={201: ProviderAccountOut, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Create a named provider account",
    operation_id="apps_harness_api_create_provider_account",
)
def create_provider_account(
    request: HttpRequest, provider: str, payload: ProviderAccountIn
):
    """Create one private account or an admin-created org-shared account."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return api_key_permission_denied(APIKeyPermission.HARNESS_PROVIDERS.value)
    try:
        org_id = get_organization_id(request)
        OrganizationService().require_membership(request.user, org_id)
        row = ProviderAccountManagementService().create_account(
            organization_id=org_id,
            user=request.user,
            provider=provider,
            payload=payload.model_dump(exclude_unset=True),
        )
        return 201, ProviderAccountOut(**row)
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, TypeError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@provider_accounts_router.put(
    "/provider-config/accounts/{provider}/{account_id}/",
    response={200: ProviderAccountOut, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Update a named provider account",
    operation_id="apps_harness_api_update_provider_account",
)
def update_provider_account(
    request: HttpRequest,
    provider: str,
    account_id: uuid.UUID,
    payload: ProviderAccountIn,
):
    """Update an exact visible account after verifying its provider."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return api_key_permission_denied(APIKeyPermission.HARNESS_PROVIDERS.value)
    try:
        org_id = get_organization_id(request)
        OrganizationService().require_membership(request.user, org_id)
        row = ProviderAccountManagementService().update_account(
            organization_id=org_id,
            user=request.user,
            provider=provider,
            payload=payload.model_dump(exclude_unset=True),
            account_id=account_id,
        )
        return 200, ProviderAccountOut(**row)
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, TypeError, KeyError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@provider_accounts_router.delete(
    "/provider-config/accounts/{provider}/{account_id}/",
    response={204: None, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Delete a named provider account",
    operation_id="apps_harness_api_delete_provider_account",
)
def delete_provider_account(request: HttpRequest, provider: str, account_id: uuid.UUID):
    """Delete only the exact account when visible and manageable."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return api_key_permission_denied(APIKeyPermission.HARNESS_PROVIDERS.value)
    try:
        org_id = get_organization_id(request)
        OrganizationService().require_membership(request.user, org_id)
        ProviderAccountManagementService().delete_account(
            org_id, int(request.user.id), provider, account_id
        )
        return 204, None
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@provider_accounts_router.put(
    "/provider-config/selection/{provider}/",
    response={204: None, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Select the active provider account for this user and organization",
    operation_id="apps_harness_api_select_provider_account",
)
def select_provider_account(
    request: HttpRequest, provider: str, payload: ProviderSelectionIn
):
    """Select one visible account or explicitly select no account."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return api_key_permission_denied(APIKeyPermission.HARNESS_PROVIDERS.value)
    try:
        org_id = get_organization_id(request)
        OrganizationService().require_membership(request.user, org_id)
        ProviderAccountService().select_account(
            org_id, int(request.user.id), provider, payload.account_id
        )
        return 204, None
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@provider_accounts_router.post(
    "/provider-config/providers/chatgpt/oauth/start/",
    response={
        200: ChatGPTOAuthStartOut,
        400: dict,
        401: dict,
        403: dict,
        404: dict,
        502: dict,
    },
    summary="Start ChatGPT device OAuth flow",
    operation_id="apps_harness_api_start_chatgpt_oauth",
)
async def start_chatgpt_oauth(request: HttpRequest, payload: ChatGPTOAuthStartIn):
    """Start an isolated durable ChatGPT flow for the current user."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return api_key_permission_denied(APIKeyPermission.HARNESS_PROVIDERS.value)

    try:
        org_id = get_organization_id(request)
        result = await ProviderOAuthService().start(
            organization_id=org_id,
            user=request.user,
            label=payload.label,
            shared=payload.shared,
            account_id=payload.account_id,
        )
        return 200, ChatGPTOAuthStartOut(**result)
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, TypeError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}
    except ProviderError:
        return 502, {
            "detail": "ChatGPT OAuth provider request failed",
            "code": "provider_error",
        }


@provider_accounts_router.get(
    "/provider-config/providers/chatgpt/oauth/status/",
    response={
        200: ChatGPTOAuthStatusOut,
        400: dict,
        401: dict,
        403: dict,
        404: dict,
        410: dict,
        502: dict,
    },
    summary="Poll ChatGPT device OAuth status (single attempt)",
    operation_id="apps_harness_api_poll_chatgpt_oauth_status",
)
async def poll_chatgpt_oauth_status(request: HttpRequest, flow_id: uuid.UUID):
    """Poll one user's flow once; flow IDs are private to their creator."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return api_key_permission_denied(APIKeyPermission.HARNESS_PROVIDERS.value)

    try:
        result = await ProviderOAuthService().status(
            organization_id=get_organization_id(request),
            user=request.user,
            flow_id=flow_id,
        )
        if result["status"] == "no_flow":
            return 404, {
                **result,
                "detail": "No pending ChatGPT OAuth flow",
                "code": "no_flow",
            }
        if result["status"] == "expired":
            return 410, result
        return 200, ChatGPTOAuthStatusOut(**result)
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, TypeError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}
    except ProviderError:
        return 502, {
            "detail": "ChatGPT OAuth provider request failed",
            "code": "provider_error",
        }


@provider_accounts_router.post(
    "/provider-config/providers/chatgpt/oauth/cancel/",
    response={204: None, 400: dict, 401: dict, 403: dict, 404: dict},
    summary="Cancel one pending ChatGPT OAuth flow",
    operation_id="apps_harness_api_cancel_chatgpt_oauth",
)
async def cancel_chatgpt_oauth(request: HttpRequest, payload: ChatGPTOAuthCancelIn):
    """Cancel the caller's explicitly identified OAuth flow."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return api_key_permission_denied(APIKeyPermission.HARNESS_PROVIDERS.value)

    try:
        cancelled = await sync_to_async(ProviderOAuthService().cancel)(
            organization_id=get_organization_id(request),
            user=request.user,
            flow_id=payload.flow_id,
        )
        if not cancelled:
            return 404, {"detail": "No pending OAuth flow", "code": "no_flow"}
        return 204, None
    except (ValueError, TypeError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}
    except AuthenticationError as exc:
        return 403, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
