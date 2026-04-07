"""
REST API endpoints for the credentials app.

Two routers:
- credential_service_router -> /api/v1/credential-services/
- credential_router         -> /api/v1/credentials/
"""

from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from apps.runners.schemas import ErrorOut
from common.exceptions import AuthenticationError, NotFoundError

from .schemas import (
    CredentialCreateIn,
    CredentialOut,
    CredentialServiceCreateIn,
    CredentialServiceOut,
    CredentialServiceWithActivationOut,
    CredentialServiceActivationToggleIn,
    CredentialUpdateIn,
    PublicKeyOut,
)
from .services import CredentialServiceSvc, CredentialSvc


def _perm_denied(permission: APIKeyPermission):
    """Return a 403 error tuple for a denied API key permission."""
    return 403, ErrorOut(
        detail=f"API key lacks permission: {permission.value}",
        code="permission_denied",
    )


def _get_org_id(request: HttpRequest) -> uuid.UUID:
    """Extract the organization ID from the X-Organization-Id header."""
    org_id_str = request.headers.get("X-Organization-Id")
    if not org_id_str:
        raise AuthenticationError("X-Organization-Id header is required")
    try:
        return uuid.UUID(org_id_str)
    except ValueError:
        raise AuthenticationError("Invalid X-Organization-Id header")


def _get_org_service() -> OrganizationService:
    return OrganizationService()


def _credential_to_out(cred) -> CredentialOut:
    """Map a Credential model instance to its output schema."""
    svc = cred.service
    scope = "personal" if cred.user_id is not None else "organization"
    return CredentialOut(
        id=cred.id,
        name=cred.name,
        scope=scope,
        service_id=svc.id,
        service_name=svc.name,
        service_slug=svc.slug,
        credential_type=svc.credential_type,
        env_var_name=svc.env_var_name,
        has_public_key=bool(cred.public_key),
        created_by_id=cred.created_by_id,
        created_at=cred.created_at,
        updated_at=cred.updated_at,
    )


def _credential_service_to_out(service, *, is_active: bool | None = None):
    payload = dict(
        id=service.id,
        name=service.name,
        slug=service.slug,
        description=service.description,
        credential_type=service.credential_type,
        env_var_name=service.env_var_name,
        label=service.label,
    )
    if is_active is None:
        return CredentialServiceOut(**payload)
    return CredentialServiceWithActivationOut(**payload, is_active=is_active)


# ===========================================================================
# Credential Service Router
# ===========================================================================

credential_service_router = Router(tags=["credential-services"])


@credential_service_router.get(
    "/",
    response={200: list[CredentialServiceOut], 403: ErrorOut},
    summary="List credential services",
)
def list_credential_services(request: HttpRequest):
    """Return all credential services."""
    if not check_api_key_permission(request, APIKeyPermission.CREDENTIALS_READ):
        return 403, ErrorOut(
            detail="API key lacks permission: credentials:read",
            code="permission_denied",
        )
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    svc = CredentialServiceSvc()
    services = svc.list_services()

    return [_credential_service_to_out(s) for s in services]


# ===========================================================================
# Credential Router
# ===========================================================================

credential_router = Router(tags=["credentials"])


@credential_router.get(
    "/",
    response={200: list[CredentialOut], 403: ErrorOut},
    summary="List credentials",
)
def list_credentials(request: HttpRequest):
    """Return all credentials visible to the user in the active organization.

    Includes personal credentials (owned by the user) and org credentials.
    """
    if not check_api_key_permission(request, APIKeyPermission.CREDENTIALS_READ):
        return 403, ErrorOut(detail="API key lacks permission: credentials:read", code="permission_denied")
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    svc = CredentialSvc()
    creds = svc.list_credentials(request.user, org_id)
    return 200, [_credential_to_out(c) for c in creds]


@credential_router.post(
    "/",
    response={201: CredentialOut, 400: ErrorOut, 403: ErrorOut, 404: ErrorOut},
    summary="Create a credential",
)
def create_credential(request: HttpRequest, payload: CredentialCreateIn):
    """Create a personal or organization credential.

    Organization credentials require admin role.
    Personal credentials can be created by any org member.
    """
    if not check_api_key_permission(request, APIKeyPermission.CREDENTIALS_WRITE):
        return 403, ErrorOut(detail="API key lacks permission: credentials:write", code="permission_denied")
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    svc = CredentialSvc()
    try:
        if payload.organization_credential:
            is_admin = org_service.get_user_role(request.user, org_id) == "admin"
            if not is_admin:
                return 403, ErrorOut(
                    detail="Only org admins can create organization credentials",
                    code="forbidden",
                )
            cred = svc.create_org_credential(
                organization_id=org_id,
                service_id=payload.service_id,
                name=payload.name or None,
                value=payload.value,
                user=request.user,
            )
        else:
            cred = svc.create_personal_credential(
                service_id=payload.service_id,
                name=payload.name or None,
                value=payload.value,
                user=request.user,
            )
        return 201, _credential_to_out(cred)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ValueError as e:
        return 400, ErrorOut(detail=str(e), code="validation_error")


@credential_router.get(
    "/{credential_id}/public-key/",
    response={200: PublicKeyOut, 403: ErrorOut, 404: ErrorOut},
    summary="Get SSH public key",
)
def get_public_key(request: HttpRequest, credential_id: uuid.UUID):
    """Return the SSH public key for a credential."""
    if not check_api_key_permission(request, APIKeyPermission.CREDENTIALS_READ):
        return 403, ErrorOut(
            detail="API key lacks permission: credentials:read",
            code="permission_denied",
        )
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    svc = CredentialSvc()
    try:
        public_key = svc.get_public_key(
            credential_id,
            org_id=org_id,
            user=request.user,
        )
        return 200, PublicKeyOut(public_key=public_key)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)


@credential_router.patch(
    "/{credential_id}/",
    response={200: CredentialOut, 403: ErrorOut, 404: ErrorOut},
    summary="Update a credential",
)
def update_credential(
    request: HttpRequest,
    credential_id: uuid.UUID,
    payload: CredentialUpdateIn,
):
    """Update a credential's name and/or value.

    Personal credentials: only owner may edit.
    Org credentials: only org admins may edit.
    """
    if not check_api_key_permission(request, APIKeyPermission.CREDENTIALS_WRITE):
        return 403, ErrorOut(detail="API key lacks permission: credentials:write", code="permission_denied")
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    is_admin = org_service.get_user_role(request.user, org_id) == "admin"

    svc = CredentialSvc()
    try:
        cred = svc.update_credential(
            credential_id=credential_id,
            org_id=org_id,
            user=request.user,
            is_admin=is_admin,
            name=payload.name,
            value=payload.value,
        )
        return 200, _credential_to_out(cred)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except AuthenticationError as e:
        return 403, ErrorOut(detail=e.message, code=e.code)


@credential_router.delete(
    "/{credential_id}/",
    response={204: None, 403: ErrorOut, 404: ErrorOut},
    summary="Delete a credential",
)
def delete_credential(request: HttpRequest, credential_id: uuid.UUID):
    """Delete a credential.

    Personal credentials: only owner may delete.
    Org credentials: only org admins may delete.
    """
    if not check_api_key_permission(request, APIKeyPermission.CREDENTIALS_WRITE):
        return 403, ErrorOut(detail="API key lacks permission: credentials:write", code="permission_denied")
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    is_admin = org_service.get_user_role(request.user, org_id) == "admin"

    svc = CredentialSvc()
    try:
        svc.delete_credential(
            credential_id,
            org_id=org_id,
            user=request.user,
            is_admin=is_admin,
        )
        return 204, None
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except AuthenticationError as e:
        return 403, ErrorOut(detail=e.message, code=e.code)


# ===========================================================================
# Org Credential Service Activation Router — /api/v1/org-credential-services/
# ===========================================================================

org_credential_service_router = Router(tags=["org-credential-services"])


@org_credential_service_router.get(
    "/",
    response={200: list[CredentialServiceWithActivationOut], 403: ErrorOut},
    summary="List all credential services with activation status (admin only)",
)
def list_org_credential_services(request: HttpRequest):
    """Return all credential services with their activation status for the org."""
    from .models import CredentialService, OrgCredentialServiceActivation

    if not check_api_key_permission(request, APIKeyPermission.ORG_CREDENTIAL_SERVICES_READ):
        return _perm_denied(APIKeyPermission.ORG_CREDENTIAL_SERVICES_READ)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    if org_service.get_user_role(request.user, org_id) != "admin":
        return 403, ErrorOut(detail="Admin role required", code="forbidden")

    activated_ids = set(
        OrgCredentialServiceActivation.objects.filter(
            organization_id=org_id
        ).values_list("credential_service_id", flat=True)
    )

    services = CredentialService.objects.all().order_by("name")
    return 200, [
        _credential_service_to_out(s, is_active=s.id in activated_ids)
        for s in services
    ]


@org_credential_service_router.post(
    "/",
    response={201: CredentialServiceWithActivationOut, 400: ErrorOut, 403: ErrorOut},
    summary="Create a credential service for organization settings",
)
def create_org_credential_service(request: HttpRequest, payload: CredentialServiceCreateIn):
    """Create a new credential service and activate it for the current organization."""
    from .models import OrgCredentialServiceActivation

    if not check_api_key_permission(request, APIKeyPermission.ORG_CREDENTIAL_SERVICES_WRITE):
        return _perm_denied(APIKeyPermission.ORG_CREDENTIAL_SERVICES_WRITE)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    if org_service.get_user_role(request.user, org_id) != "admin":
        return 403, ErrorOut(detail="Admin role required", code="forbidden")
    if not request.user.is_staff:
        return 403, ErrorOut(
            detail="Only staff users can create credential services",
            code="forbidden",
        )

    svc = CredentialServiceSvc()
    try:
        service = svc.create_service(
            name=payload.name,
            slug=payload.slug,
            description=payload.description,
            credential_type=payload.credential_type,
            env_var_name=payload.env_var_name,
            label=payload.label,
        )
    except ValueError as e:
        return 400, ErrorOut(detail=str(e), code="validation_error")

    OrgCredentialServiceActivation.objects.get_or_create(
        organization_id=org_id,
        credential_service=service,
    )
    return 201, _credential_service_to_out(service, is_active=True)


@org_credential_service_router.post(
    "/{service_id}/activation/",
    response={200: CredentialServiceWithActivationOut, 403: ErrorOut, 404: ErrorOut},
    summary="Toggle activation of a credential service for the org",
)
def toggle_org_credential_service_activation(
    request: HttpRequest,
    service_id: uuid.UUID,
    payload: CredentialServiceActivationToggleIn,
):
    """Activate or deactivate a credential service for the organization."""
    from .models import CredentialService, OrgCredentialServiceActivation

    if not check_api_key_permission(request, APIKeyPermission.ORG_CREDENTIAL_SERVICES_WRITE):
        return _perm_denied(APIKeyPermission.ORG_CREDENTIAL_SERVICES_WRITE)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    if org_service.get_user_role(request.user, org_id) != "admin":
        return 403, ErrorOut(detail="Admin role required", code="forbidden")
    if not request.user.is_staff:
        return 403, ErrorOut(
            detail="Only staff users can modify credential service activation",
            code="forbidden",
        )

    svc = CredentialService.objects.filter(id=service_id).first()
    if svc is None:
        return 404, ErrorOut(detail="Credential service not found", code="not_found")

    if payload.active:
        OrgCredentialServiceActivation.objects.get_or_create(
            organization_id=org_id, credential_service=svc
        )
        is_active = True
    else:
        OrgCredentialServiceActivation.objects.filter(
            organization_id=org_id, credential_service=svc
        ).delete()
        is_active = False

    return 200, _credential_service_to_out(svc, is_active=is_active)
