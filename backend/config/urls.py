"""
URL configuration for opencuria backend.
"""

from __future__ import annotations

from django.contrib import admin
from django.urls import path
from ninja import NinjaAPI

from apps.accounts.api import auth_router
from apps.accounts.api_auth import APIKeyBearer, APIKeyInHeader, JWTAuth
from apps.credentials.api import credential_router, credential_service_router, org_credential_service_router
from apps.skills.api import skill_router
from apps.organizations.api import org_router
from apps.runners.api import (
    agent_router,
    conversation_router,
    image_artifact_router,
    image_definition_router,
    org_agent_def_router,
    runner_router,
    workspace_image_artifact_router,
    workspace_router,
)

api = NinjaAPI(
    title="opencuria API",
    version="1.0.0",
    urls_namespace="api",
    auth=[JWTAuth(), APIKeyBearer(), APIKeyInHeader()],
)


# ---------------------------------------------------------------------------
# Health check (unauthenticated)
# ---------------------------------------------------------------------------

@api.get("/health/", auth=None, tags=["health"])
def health_check(request):
    """Simple health check endpoint for load balancers and monitoring."""
    return {"status": "ok"}


api.add_router("/auth/", auth_router)
api.add_router("/organizations/", org_router)
api.add_router("/runners/", runner_router)
api.add_router("/workspaces/", workspace_router)
api.add_router("/workspaces/", workspace_image_artifact_router)
api.add_router("/image-artifacts/", image_artifact_router)
api.add_router("/agents/", agent_router)
api.add_router("/conversations/", conversation_router)
api.add_router("/credential-services/", credential_service_router)
api.add_router("/credentials/", credential_router)
api.add_router("/skills/", skill_router)
api.add_router("/org-agent-definitions/", org_agent_def_router)
api.add_router("/org-credential-services/", org_credential_service_router)
api.add_router("/image-definitions/", image_definition_router)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", api.urls),
]
