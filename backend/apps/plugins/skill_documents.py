"""SKILL.md documents: parse authored skills and render them for agents.

Pure functions without database access, shared by the plugin service, the
runtime snapshot and the harness engines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Agent Skills limits (name and description) shared by Claude and Codex.
MAX_SKILL_NAME_LEN = 64
MAX_DESCRIPTION_LEN = 1024

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):[ \t]*(.*)$")
_NAME_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class SkillDocument:
    """A skill as authored: optional name/description plus Markdown body."""

    name: str
    description: str
    body: str


def _scalar(raw: str) -> str:
    """Unquote one YAML scalar written on a single line."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value.strip()


def parse_skill_markdown(text: str) -> SkillDocument:
    """Split a SKILL.md into its frontmatter ``name``/``description`` and body.

    Only single-line ``key: value`` scalars (plus YAML folded/literal block
    scalars for the description) are read; other frontmatter is dropped.
    Text without frontmatter is returned as the body.
    """
    source = text or ""
    match = _FRONTMATTER_RE.match(source)
    if not match:
        return SkillDocument(name="", description="", body=source.strip())
    values: dict[str, str] = {}
    lines = match.group(1).splitlines()
    index = 0
    while index < len(lines):
        found = _KEY_RE.match(lines[index])
        index += 1
        if not found:
            continue
        key, raw = found.group(1).lower(), found.group(2)
        if raw.strip() in {"|", ">", "|-", ">-", "|+", ">+"}:
            block: list[str] = []
            while index < len(lines) and (
                not lines[index].strip() or lines[index][:1] in {" ", "\t"}
            ):
                block.append(lines[index].strip())
                index += 1
            joiner = "\n" if raw.strip().startswith("|") else " "
            values[key] = joiner.join(part for part in block if part).strip()
        else:
            values[key] = _scalar(raw)
    return SkillDocument(
        name=values.get("name", ""),
        description=values.get("description", ""),
        body=source[match.end() :].strip(),
    )


def default_description(body: str) -> str:
    """First prose line of *body*, used when a skill has no description."""
    for line in (body or "").splitlines():
        text = line.strip().lstrip("#").strip()
        if text and not text.startswith(("---", "```")):
            return text[:MAX_DESCRIPTION_LEN]
    return ""


def skill_identifier(slug: str) -> str:
    """Agent Skills ``name``: lowercase letters, digits and hyphens, ≤64."""
    value = _NAME_RE.sub("-", (slug or "").lower()).strip("-")
    return (value or "skill")[:MAX_SKILL_NAME_LEN].strip("-") or "skill"


def _yaml_text(value: str) -> str:
    """Quote a single-line YAML scalar safely."""
    flat = " ".join((value or "").split())
    return '"' + flat.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_skill_markdown(*, identifier: str, description: str, body: str) -> str:
    """Render a SKILL.md: frontmatter with name and description, then body."""
    return (
        "---\n"
        f"name: {skill_identifier(identifier)}\n"
        f"description: {_yaml_text(description or default_description(body))}\n"
        "---\n\n"
        f"{(body or '').strip()}\n"
    )
