"""Pure pinned Codex process configuration; no credential values in arguments."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .auth import CodexAuthConfiguration

# Applied at process startup and again to every thread start/resume. Codex
# merges project configuration, so safety settings must be CLI-level overrides.
DISABLED_FEATURES = (
    "multi_agent",
    "multi_agent_v2",
    "multi_agent_v2_dynamic_tools",
    "collab",
    "plugins",
    "plugin_hooks",
    "hooks",
    "codex_hooks",
    "remote_plugin",
    "recommended_plugins",
    "remote_models",
    "apps",
    "connectors",
    "browser_use",
    "computer_use",
    "memories",
    "memory_tool",
    "shell_snapshot",
    "shell_snapshot_v2",
    "js_repl",
    "code_mode",
    "tool_search",
    "skill_search",
    "skill_mcp_dependency_install",
    "external_agent_memory_import",
    "remote_control",
    "responses_websockets",
    "responses_websockets_v2",
    "unbounded_connection_retries",
    "workspace_dependencies",
    "image_generation",
    "imagegenext",
    "view_image",
)


def _toml(value: Any) -> str:
    """Serialize a bounded runtime setting to its TOML inline representation."""
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(json.dumps(k) + " = " + _toml(v) for k, v in value.items())
            + " }"
        )
    if isinstance(value, list):
        return "[" + ", ".join(_toml(x) for x in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def runtime_settings(
    auth: CodexAuthConfiguration, model: str, *, readonly: bool
) -> tuple[dict[str, Any], str, str]:
    """Return authoritative pinned-CLI overrides and the provider wire model."""
    provider = "openrouter" if auth.provider_id == "openrouter" else "openai"
    bare = model.split("/", 1)[1]
    # Official OpenRouter Codex guide requires ~ before the vendor/model slug.
    wire_model = "~" + bare if provider == "openrouter" else bare
    settings = {
        "model": wire_model,
        "model_provider": provider,
        "approval_policy": "never",
        "sandbox_mode": "read-only" if readonly else "danger-full-access",
        "agents.enabled": False,
        "web_search": "disabled",
        "mcp_servers": {},
        "projects": {},
        "notify": [],
        "project_doc_max_bytes": 0,
        "check_for_update_on_startup": False,
        "analytics.enabled": False,
        "feedback.enabled": False,
        "cli_auth_credentials_store": "ephemeral",
        "allow_symlinked_codex_home": False,
        "allow_login_shell": False,
        "skills.include_instructions": False,
        "features.skip_host_skill_discovery": True,
        "features.shell_tool": not readonly,
        # Models can require exec/wait even with optional code mode disabled.
        # The host executes their wrappers; managed tools still use OpenCuria's
        # dispatcher and native tools retain the thread's sandbox policy.
        "features.code_mode_host": True,
    }
    settings.update({f"features.{key}": False for key in DISABLED_FEATURES})
    if provider == "openrouter":
        settings["model_providers.openrouter"] = {
            "name": "openrouter",
            "base_url": auth.base_url or "https://openrouter.ai/api/v1",
            "wire_api": "responses",
            "requires_openai_auth": False,
            "supports_websockets": False,
            "request_max_retries": 0,
            "stream_max_retries": 0,
            "auth": {
                "command": "/bin/sh",
                "args": ["-c", 'printf %s "$OPENCURIA_CODEX_API_KEY"'],
                "timeout_ms": 5000,
            },
        }
    return settings, provider, wire_model


def runtime_argv(binary: str, settings: dict[str, Any]) -> list[str]:
    """Compact feature overrides to respect the runner's 64-argument ceiling.

    A single authoritative inline features table retains CLI precedence over
    project configuration without one pair of arguments for every feature.
    """
    features = {
        key.removeprefix("features."): value
        for key, value in settings.items()
        if key.startswith("features.")
    }
    overrides = {
        key: value for key, value in settings.items() if not key.startswith("features.")
    }
    overrides["features"] = features
    argv = [binary]
    for key, value in overrides.items():
        argv.extend(["-c", f"{key}={_toml(value)}"])
    if len(argv) > 64 or any(len(part) > 4096 for part in argv):
        raise ValueError("Codex configuration exceeds managed process limits")
    return argv
