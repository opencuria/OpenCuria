"""Agent Skills inside plugins: SKILL.md parsing, rendering and selection.

Skills only exist as components of plugins. A skill has a name, a
description that tells agents when to use it, and Markdown instructions.
Agents see the name and description of every skill of the plugins enabled
in their workspace and load the instructions on demand (progressive
disclosure). Users can also select skills explicitly for a chat or a
scheduled task; those are always applied in full.
"""

from __future__ import annotations

import uuid

from .repositories import OrgPluginActivationRepository, PluginSkillRepository
from .skill_documents import render_skill_markdown


class PluginSkillService:
    """Selection and resolution of plugin skills for chats and runs.

    A skill is *usable* by a user in an organization when its plugin is one
    of the user's personal plugins, or a built-in/organization plugin that
    is released for the organization.
    """

    def __init__(self) -> None:
        self.skills = PluginSkillRepository
        self.org_activations = OrgPluginActivationRepository

    def list_usable(self, *, user_id: int, organization_id: uuid.UUID) -> list:
        """Return the usable skills (with their plugin loaded), ordered."""
        activated = self.org_activations.enabled_plugin_ids(organization_id)
        return list(
            self.skills.list_usable(organization_id, user_id, activated_ids=activated)
        )

    @staticmethod
    def normalize_ids(skill_ids: list[str] | None) -> list[str]:
        """Normalize skill ID strings (deduped, trimmed)."""
        seen: set[str] = set()
        normalized: list[str] = []
        for raw in skill_ids or []:
            value = str(raw).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    def _usable_by_id(
        self, *, user_id: int, organization_id: uuid.UUID
    ) -> dict[uuid.UUID, object]:
        return {
            skill.id: skill
            for skill in self.list_usable(
                user_id=user_id, organization_id=organization_id
            )
        }

    def resolve_bodies(
        self,
        skill_ids: list[str],
        *,
        user_id: int,
        organization_id: uuid.UUID,
    ) -> list[str]:
        """Render the selected skills as SKILL.md documents, in order.

        Raises:
            ValueError: When an ID is invalid or the skill is not usable.
        """
        if not skill_ids:
            return []
        parsed: list[uuid.UUID] = []
        for raw in skill_ids:
            try:
                parsed.append(uuid.UUID(str(raw).strip()))
            except ValueError:
                raise ValueError(f"Invalid skill_id: {raw}") from None
        usable = self._usable_by_id(user_id=user_id, organization_id=organization_id)
        bodies: list[str] = []
        for skill_id in parsed:
            skill = usable.get(skill_id)
            if skill is None:
                raise ValueError(f"Skill not found or not accessible: {skill_id}")
            bodies.append(
                render_skill_markdown(
                    identifier=skill.slug,
                    description=skill.description,
                    body=skill.body,
                )
            )
        return bodies

    def filter_available_ids(
        self,
        skill_ids: list[str],
        *,
        user_id: int,
        organization_id: uuid.UUID,
    ) -> list[str]:
        """Return the normalized *skill_ids* that are still usable, in order.

        Persisted selections outlive their skills: a skill deleted (or no
        longer released) after it was chosen drops out instead of failing
        every later run that inherits the selection.
        """
        normalized = self.normalize_ids(skill_ids)
        if not normalized:
            return []
        usable = {
            str(skill_id)
            for skill_id in self._usable_by_id(
                user_id=user_id, organization_id=organization_id
            )
        }
        result: list[str] = []
        for raw in normalized:
            try:
                key = str(uuid.UUID(raw))
            except ValueError:
                continue
            if key in usable:
                result.append(raw)
        return result

    @staticmethod
    def merge_bodies(
        explicit: list[str] | None, plugin_bodies: list[str] | None
    ) -> list[str]:
        """Merge plugin skill documents before explicit selections (deduped)."""
        merged: list[str] = []
        seen: set[str] = set()
        for body in list(plugin_bodies or []) + list(explicit or []):
            text = str(body or "")
            if not text.strip() or text in seen:
                continue
            seen.add(text)
            merged.append(text)
        return merged
