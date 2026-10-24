"""Organization-scoped image recipe management shared by REST and MCP.

Owns who may see or change an image definition and its runner builds,
recipe validation and duplicate naming. Recipes and builds are read and
written through the image repositories; runner-facing build and deletion
orchestration stays on the injected :class:`RunnerService`.
"""

from __future__ import annotations

import uuid
from typing import Any

from asgiref.sync import sync_to_async

from apps.organizations.services import OrganizationService
from common.exceptions import ConflictError

from ..exceptions import (
    AdminRoleRequiredError,
    ImageRecipeNotFoundError,
    ImageRecipeValidationError,
)
from ..repositories import (
    ImageBuildJobRepository,
    ImageDefinitionRepository,
    RunnerRepository,
)

#: Recipe fields an organization admin may set on create and update.
RECIPE_FIELDS = (
    "name",
    "description",
    "base_distro",
    "packages",
    "env_vars",
    "custom_init_script",
    "is_active",
)


class ImageDefinitionService:
    """Authorize and orchestrate image recipe and runner build requests.

    Global recipes (``organization_id is None``) are visible to every
    organization but only org-owned recipes can be edited or removed.
    """

    def __init__(self, runner_service: Any) -> None:
        self._runner_service = runner_service
        self._organizations = OrganizationService()

    # -- Recipes ---------------------------------------------------------

    @staticmethod
    def build_summary(definition) -> dict[str, int]:
        """Return per-status runner build counts for a recipe."""
        return ImageDefinitionRepository.build_summary(definition)

    def list_visible(self, user, organization_id: uuid.UUID) -> list:
        """List global and org-owned recipes that are not deleted."""
        self._organizations.require_membership(user, organization_id)
        return list(ImageDefinitionRepository.list_by_org(organization_id))

    def create(self, user, organization_id: uuid.UUID, recipe: dict[str, Any]):
        """Create an org-owned recipe without requesting a build."""
        self._require_admin(user, organization_id)
        self._validate_base_distro(recipe["base_distro"])
        return ImageDefinitionRepository.create(
            organization_id=organization_id,
            created_by=user,
            **{field: recipe[field] for field in RECIPE_FIELDS},
        )

    def duplicate(
        self,
        user,
        organization_id: uuid.UUID,
        definition_id: uuid.UUID,
        name: str | None = None,
    ):
        """Copy a visible recipe into the organization under a unique name."""
        self._require_admin(user, organization_id)
        source = self._visible_definition(definition_id, organization_id)
        if name is not None and not name.strip():
            raise ImageRecipeValidationError("name cannot be empty")
        return ImageDefinitionRepository.create(
            organization_id=organization_id,
            created_by=user,
            name=ImageDefinitionRepository.copy_name(
                name or source.name, organization_id
            ),
            description=source.description,
            base_distro=source.base_distro,
            packages=list(source.packages or []),
            env_vars=dict(source.env_vars or {}),
            custom_init_script=source.custom_init_script,
            is_active=source.is_active,
        )

    def update(
        self,
        user,
        organization_id: uuid.UUID,
        definition_id: uuid.UUID,
        values: dict[str, Any],
    ):
        """Apply the given non-``None`` recipe fields; never auto-builds."""
        definition = self._owned_definition_for_admin(
            user, organization_id, definition_id
        )
        self._validate_base_distro(values.get("base_distro") or definition.base_distro)
        return ImageDefinitionRepository.update_recipe(
            definition.id,
            {field: values[field] for field in RECIPE_FIELDS if field in values},
        )

    async def delete(self, user, organization_id: uuid.UUID, definition_id: uuid.UUID):
        """Request orchestrated removal; return the recipe, or None once gone."""
        definition = await sync_to_async(self._owned_definition_for_admin)(
            user, organization_id, definition_id
        )
        await self._runner_service.delete_image_definition(definition.id)
        return await sync_to_async(ImageDefinitionRepository.get_by_id)(definition.id)

    async def deactivate(
        self, user, organization_id: uuid.UUID, definition_id: uuid.UUID
    ):
        """Hide a recipe from new workspaces and return its new state."""
        definition = await sync_to_async(self._owned_definition_for_admin)(
            user, organization_id, definition_id
        )
        await self._runner_service.deactivate_image_definition(definition.id)
        return await sync_to_async(ImageDefinitionRepository.get_by_id)(definition.id)

    async def activate(
        self, user, organization_id: uuid.UUID, definition_id: uuid.UUID
    ):
        """Make a deactivated recipe selectable again and return its state."""
        definition = await sync_to_async(self._owned_definition_for_admin)(
            user, organization_id, definition_id
        )
        await self._runner_service.activate_image_definition(definition.id)
        return await sync_to_async(ImageDefinitionRepository.get_by_id)(definition.id)

    # -- Runner builds ---------------------------------------------------

    def list_builds(
        self, user, organization_id: uuid.UUID, definition_id: uuid.UUID
    ) -> list:
        """List a visible recipe's runner builds (``build_log`` deferred)."""
        self._organizations.require_membership(user, organization_id)
        self._visible_definition(definition_id, organization_id)
        return list(
            ImageBuildJobRepository.list_for_definition(
                definition_id, organization_id=organization_id
            )
        )

    def build_log(
        self,
        user,
        organization_id: uuid.UUID,
        definition_id: uuid.UUID,
        runner_id: uuid.UUID,
    ) -> str:
        """Return the stored build log tail of one runner build."""
        self._organizations.require_membership(user, organization_id)
        self._visible_definition(definition_id, organization_id)
        return self._build(definition_id, runner_id, organization_id).build_log

    async def create_build(
        self,
        user,
        organization_id: uuid.UUID,
        definition_id: uuid.UUID,
        runner_id: uuid.UUID,
        *,
        activate: bool = True,
        message: str = "",
    ):
        """Assign a visible recipe to an org runner and request its build."""
        definition, runner = await sync_to_async(self._build_target)(
            user, organization_id, definition_id, runner_id
        )
        return await self._runner_service.trigger_build_job(
            image_definition=definition,
            runner=runner,
            activate=activate,
            created_by=user,
            message=message,
        )

    async def update_build(
        self,
        user,
        organization_id: uuid.UUID,
        definition_id: uuid.UUID,
        runner_id: uuid.UUID,
        action: str,
        *,
        message: str = "",
    ):
        """Apply ``deactivate``, ``activate`` or ``rebuild`` to a runner build."""
        build = await sync_to_async(self._build_for_admin)(
            user, organization_id, definition_id, runner_id
        )
        action = action.strip().lower()
        if action == "deactivate":
            return await sync_to_async(self._runner_service.deactivate_runner_image)(
                build
            )
        if action == "activate":
            return await self._runner_service.activate_build_job(
                build, created_by=user, message=message
            )
        if action == "rebuild":
            return await self._runner_service.trigger_build_job(
                image_definition=build.image_definition,
                runner=build.runner,
                activate=True,
                created_by=user,
                message=message,
            )
        raise ConflictError("action must be one of: deactivate, activate, rebuild")

    async def delete_build(
        self,
        user,
        organization_id: uuid.UUID,
        definition_id: uuid.UUID,
        runner_id: uuid.UUID,
    ) -> dict:
        """Request removal of a runner build and its image versions."""
        build = await sync_to_async(self._build_for_admin)(
            user, organization_id, definition_id, runner_id
        )
        return await self._runner_service.delete_build_job(build.id)

    # -- Access and validation -------------------------------------------

    def _require_admin(self, user, organization_id: uuid.UUID) -> None:
        self._organizations.require_membership(user, organization_id)
        if self._organizations.get_user_role(user, organization_id) != "admin":
            raise AdminRoleRequiredError()

    @staticmethod
    def _visible_definition(definition_id: uuid.UUID, organization_id: uuid.UUID):
        definition = ImageDefinitionRepository.get_by_id_and_org(
            definition_id, organization_id
        )
        if definition is None:
            raise ImageRecipeNotFoundError("Image definition")
        return definition

    def _owned_definition_for_admin(
        self, user, organization_id: uuid.UUID, definition_id: uuid.UUID
    ):
        self._require_admin(user, organization_id)
        definition = self._visible_definition(definition_id, organization_id)
        if definition.organization_id != organization_id:
            raise ImageRecipeNotFoundError("Image definition")
        return definition

    @staticmethod
    def _build(
        definition_id: uuid.UUID, runner_id: uuid.UUID, organization_id: uuid.UUID
    ):
        build = ImageBuildJobRepository.get_for_org(
            definition_id, runner_id, organization_id
        )
        if build is None:
            raise ImageRecipeNotFoundError("Runner image build")
        return build

    def _build_for_admin(
        self,
        user,
        organization_id: uuid.UUID,
        definition_id: uuid.UUID,
        runner_id: uuid.UUID,
    ):
        self._require_admin(user, organization_id)
        return self._build(definition_id, runner_id, organization_id)

    def _build_target(
        self,
        user,
        organization_id: uuid.UUID,
        definition_id: uuid.UUID,
        runner_id: uuid.UUID,
    ):
        self._require_admin(user, organization_id)
        definition = self._visible_definition(definition_id, organization_id)
        runner = RunnerRepository.get_by_id(runner_id)
        if runner is None or runner.organization_id != organization_id:
            raise ImageRecipeNotFoundError("Runner")
        return definition, runner

    @staticmethod
    def _validate_base_distro(base_distro: str | None) -> None:
        if not (base_distro or "").strip().lower().startswith("ubuntu:"):
            raise ConflictError(
                "QEMU image definitions currently require an ubuntu:<version> "
                "base distro",
                code="unsupported_base_distro",
            )
