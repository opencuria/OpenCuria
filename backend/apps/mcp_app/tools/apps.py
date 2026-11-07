"""App tools: send an Expo app built in the workspace to the user's phone."""

from __future__ import annotations

from typing import Any

from asgiref.sync import sync_to_async

from apps.harness.access.runner_accessor import RunnerAccessorError
from apps.mobile_apps.export import MAX_NOTES_CHARS, ExportError
from apps.mobile_apps.runtime import app_runtime
from apps.mobile_apps.services import AppService
from common.exceptions import ServiceError

from ..context import ToolContext
from ..errors import ToolError
from ..registry import ToolSpec, registrar
from ..results import compact, paginate
from ..schema import OFFSET, limit, obj, string

TOOLS: list[ToolSpec] = []
tool = registrar(TOOLS)

_RUNTIME = app_runtime()
_SDK = _RUNTIME.sdk_major


def _platforms(sizes: dict[str, int]) -> list[str]:
    return sorted(sizes)


@tool(
    "app_publish",
    title="Publish app",
    schema=obj(
        {
            "project_path": string(
                "Expo project directory in this workspace (absolute or relative "
                "to /workspace), e.g. '/workspace/todo-app'."
            ),
            "output_dir": string(
                "Directory `npx expo export` wrote, relative to project_path "
                "(default 'dist')."
            ),
            "notes": string(
                "What is new in this version, shown to the user "
                f"(max {MAX_NOTES_CHARS} characters).",
                maxLength=MAX_NOTES_CHARS,
            ),
        },
        ("project_path",),
    ),
    description=f"""
        Send an Expo app you built in this workspace to the user's OpenCuria
        mobile app. The user installs it under Apps and runs it inside OpenCuria
        like in Expo Go, also offline. Publishing the same Expo slug again
        offers the installed app an update. The user sees the Expo config's
        name and icon.
        Requirements: Expo SDK {_SDK}; start new projects with
        `npx create-expo-app@latest <dir> --template blank-typescript@sdk-{_SDK}`
        and add dependencies with `npx expo install`. Only the native modules
        Expo Go provides are available; custom native code and config plugins
        do not run.
        Build first, in project_path:
        `npx expo export --platform android --platform ios`
    """,
)
async def app_publish(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await AppService().publish(
            owner_id=ctx.user.id,
            organization_id=ctx.organization_id,
            workspace_id=ctx.workspace_id,
            project_path=args["project_path"],
            output_dir=args.get("output_dir") or "dist",
            notes=args.get("notes") or "",
        )
    except ExportError as exc:
        raise ToolError(str(exc)) from exc
    except (RunnerAccessorError, ServiceError) as exc:
        message = getattr(exc, "message", None) or str(exc)
        raise ToolError(
            f"Could not read the build from the workspace: {message}"
        ) from exc

    def describe() -> dict[str, Any]:
        version = result.version
        sizes = AppService().version_sizes(version)
        return compact(
            {
                "app_id": version.app_id,
                "version_id": version.id,
                "name": version.app.name,
                "slug": version.app.slug,
                "version": version.number,
                "version_name": version.version_name,
                "platforms": _platforms(sizes),
                "warnings": list(result.warnings),
                "next": (
                    "Tell the user to install it from Apps in the OpenCuria mobile app."
                    if version.number == 1
                    else "The user's installed copy offers this update under Apps."
                ),
            }
        )

    return await sync_to_async(describe)()


@tool(
    "app_list",
    title="List apps",
    read_only=True,
    schema=obj({"limit": limit(20, 100), "offset": OFFSET}),
    description="""
        List the Expo apps published to the user, newest first, with their
        latest version, Expo slug and the workspace and project path that
        hold their source. Use it to find an app's project before updating it.
    """,
)
async def app_list(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        listings = AppService().list_apps(
            organization_id=ctx.organization_id, owner_id=ctx.user.id
        )
        page, meta = paginate(
            listings, limit=args.get("limit", 20), offset=args.get("offset", 0)
        )
        items = []
        for listing in page:
            app, version = listing.app, listing.latest
            items.append(
                compact(
                    {
                        "app_id": app.id,
                        "name": app.name,
                        "slug": app.slug,
                        "version": version.number if version else None,
                        "version_name": version.version_name if version else None,
                        "published_at": version.created_at if version else None,
                        "platforms": (
                            _platforms(listing.platforms(version)) if version else []
                        ),
                        "workspace": app.workspace.name if app.workspace else None,
                        "workspace_id": app.workspace_id,
                        "project_path": app.project_path,
                    }
                )
            )
        return {
            "apps": items,
            **meta,
            "runtime": {
                "expo_sdk": _RUNTIME.sdk_version,
                "react_native": _RUNTIME.react_native_version,
            },
        }

    return await sync_to_async(build)()
