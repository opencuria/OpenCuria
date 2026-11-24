"""Personal plugins, skill descriptions and plugin-chosen credentials.

Skills now only exist inside plugins. Standalone skills move into one
"Organization skills" plugin per organization (released for it) and one
"My skills" plugin per user. Skill IDs are kept, so skill selections of
chats, messages and scheduled tasks stay valid. The old ``skills_skill``
table is dropped afterwards.
"""

from __future__ import annotations

import re
import uuid

from django.conf import settings
from django.db import migrations, models
from django.utils.text import slugify

_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_DESCRIPTION = re.compile(r"^description:[ \t]*(.*)$")
_BLOCK_MARKERS = {"|", ">", "|-", ">-", "|+", ">+"}


def _frontmatter_description(frontmatter: str) -> str:
    """Read ``description`` incl. folded (``>``) and literal (``|``) blocks."""
    lines = frontmatter.splitlines()
    for index, line in enumerate(lines):
        found = _DESCRIPTION.match(line)
        if not found:
            continue
        value = found.group(1).strip()
        if value not in _BLOCK_MARKERS:
            return value.strip("'\"")
        block = []
        for follow in lines[index + 1 :]:
            if follow.strip() and not follow[:1].isspace():
                break
            block.append(follow.strip())
        return " ".join(part for part in block if part)
    return ""


def _split(body: str) -> tuple[str, str]:
    """Return (description, body without frontmatter) of a stored skill."""
    text = (body or "").strip()
    description = ""
    match = _FRONTMATTER.match(text)
    if match:
        description = _frontmatter_description(match.group(1))
        text = text[match.end() :].strip()
    if not description:
        for line in text.splitlines():
            line = line.strip().lstrip("#").strip()
            if line and not line.startswith(("---", "```")):
                description = line
                break
    return " ".join(description.split())[:1024], text or (body or "").strip()


def _unique(base: str, taken: set[str], fallback: str) -> str:
    slug = (slugify(base) or fallback)[:240]
    candidate, index = slug, 2
    while candidate in taken:
        candidate = f"{slug}-{index}"
        index += 1
    taken.add(candidate)
    return candidate


def _uuid(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def migrate_skills(apps, schema_editor) -> None:
    connection = schema_editor.connection
    if "skills_skill" not in connection.introspection.table_names():
        return
    Plugin = apps.get_model("plugins", "Plugin")
    PluginSkill = apps.get_model("plugins", "PluginSkill")
    OrgPluginActivation = apps.get_model("plugins", "OrgPluginActivation")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, name, body, user_id, organization_id, created_by_id "
            "FROM skills_skill ORDER BY name"
        )
        rows = cursor.fetchall()

    groups: dict[tuple[str, object], list[tuple]] = {}
    for row in rows:
        _, _, _, user_id, organization_id, _ = row
        key = ("user", user_id) if user_id is not None else ("org", organization_id)
        groups.setdefault(key, []).append(row)

    for (kind, owner), skills in groups.items():
        creator = next((row[5] for row in skills if row[5] is not None), None)
        if kind == "org":
            org_id = _uuid(owner)
            taken = set(
                Plugin.objects.filter(organization_id=org_id).values_list(
                    "slug", flat=True
                )
            )
            plugin = Plugin.objects.create(
                organization_id=org_id,
                name="Organization skills",
                slug=_unique("organization-skills", taken, "skills"),
                description="Skills shared with everyone in the organization.",
                created_by_id=creator,
            )
            OrgPluginActivation.objects.get_or_create(
                organization_id=org_id, plugin=plugin
            )
        else:
            taken = set(
                Plugin.objects.filter(owner_id=owner).values_list("slug", flat=True)
            )
            plugin = Plugin.objects.create(
                owner_id=owner,
                name="My skills",
                slug=_unique("my-skills", taken, "skills"),
                description="Your personal skills.",
                created_by_id=owner,
            )
        slugs: set[str] = set()
        for position, (skill_id, name, body, *_rest) in enumerate(skills):
            description, text = _split(body)
            PluginSkill.objects.create(
                id=_uuid(skill_id),
                plugin=plugin,
                name=(name or "Skill")[:255],
                slug=_unique(name or "skill", slugs, "skill"),
                description=description,
                body=text,
                position=position,
            )
    schema_editor.execute("DROP TABLE skills_skill")


def publish_org_plugins(apps, schema_editor) -> None:
    """Fold the unpublished state of organization plugins into the release.

    Clients no longer edit ``enabled``/``published``; an organization plugin
    is available exactly when it is released. Unpublished ones lose their
    release, so nothing becomes available that was not before.
    """
    Plugin = apps.get_model("plugins", "Plugin")
    hidden = Plugin.objects.filter(organization__isnull=False).exclude(
        enabled=True, published=True
    )
    apps.get_model("plugins", "OrgPluginActivation").objects.filter(
        plugin__in=hidden
    ).delete()
    hidden.update(enabled=True, published=True)


OPENCURIA_PLUGIN_ID = uuid.UUID("128eb61c-0bc6-4c64-8a1b-2c9dc4e440b6")
OPENCURIA_DESCRIPTION = (
    "Lets the agent manage OpenCuria itself through its MCP server: "
    "workspaces, chats, plugins (skills and MCP servers, including ones it "
    "adds for itself) and scheduled tasks, and send Expo apps it builds to the "
    "user's OpenCuria mobile app. The agent acts as the workspace owner."
)


def describe_builtin_skills(apps, schema_editor) -> None:
    """Describe the seeded Playwright skill and the OpenCuria plugin anew."""
    apps.get_model("plugins", "Plugin").objects.filter(id=OPENCURIA_PLUGIN_ID).update(
        description=OPENCURIA_DESCRIPTION
    )
    apps.get_model("plugins", "PluginSkill").objects.filter(
        plugin__slug="playwright",
        plugin__organization__isnull=True,
        slug="playwright-basics",
        description="",
    ).update(
        description=(
            "Drive a browser with the Playwright MCP server: end-to-end tests, "
            "web inspection, screenshots and form automation."
        )
    )


class Migration(migrations.Migration):
    dependencies = [
        ("plugins", "0016_opencuria_plugin_apps"),
        ("organizations", "0001_initial"),
        ("credentials", "0014_personal_credential_services"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="plugin",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Set for personal plugins (mutually exclusive with organization)."
                ),
                null=True,
                on_delete=models.deletion.CASCADE,
                related_name="personal_plugins",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="plugin",
            name="organization",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Null for built-in and personal plugins; set for org plugins."
                ),
                null=True,
                on_delete=models.deletion.CASCADE,
                related_name="plugins",
                to="organizations.organization",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="plugin",
            name="unique_global_plugin_slug",
        ),
        migrations.AddConstraint(
            model_name="plugin",
            constraint=models.UniqueConstraint(
                condition=models.Q(organization__isnull=True, owner__isnull=True),
                fields=("slug",),
                name="unique_global_plugin_slug",
            ),
        ),
        migrations.AddConstraint(
            model_name="plugin",
            constraint=models.UniqueConstraint(
                condition=models.Q(owner__isnull=False),
                fields=("owner", "slug"),
                name="unique_personal_plugin_slug",
            ),
        ),
        migrations.AddConstraint(
            model_name="plugin",
            constraint=models.CheckConstraint(
                condition=~models.Q(organization__isnull=False, owner__isnull=False),
                name="plugin_scope_exclusive",
            ),
        ),
        migrations.AddField(
            model_name="pluginskill",
            name="description",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "What the skill does and when to use it; agents see only this "
                    "until they load the skill."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="pluginskill",
            name="body",
            field=models.TextField(help_text="Markdown instructions of the skill."),
        ),
        migrations.AddField(
            model_name="plugincredentialrequirement",
            name="credential",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Optional credential of credential_service the plugin always "
                    "uses; otherwise each workspace attaches its own."
                ),
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="plugin_requirements",
                to="credentials.credential",
            ),
        ),
        migrations.AlterField(
            model_name="pluginmcpserver",
            name="startup_timeout_seconds",
            field=models.PositiveIntegerField(default=60),
        ),
        migrations.AlterField(
            model_name="pluginmcpserver",
            name="request_timeout_seconds",
            field=models.PositiveIntegerField(default=600),
        ),
        migrations.RunPython(publish_org_plugins, migrations.RunPython.noop),
        migrations.RunPython(describe_builtin_skills, migrations.RunPython.noop),
        migrations.RunPython(migrate_skills, migrations.RunPython.noop),
    ]
