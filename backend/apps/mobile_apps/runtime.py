"""The native runtime the OpenCuria mobile app offers published apps.

Apps run inside the OpenCuria app like experiences in Expo Go: their
JavaScript comes from the agent's build, every native module from the
OpenCuria app. ``runtime.json`` lists that runtime (Expo SDK and native
packages with their exact versions); ``mobile/scripts/sync-app-runtime.mjs``
generates it from the mobile app's dependencies.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

RUNTIME_FILE = Path(__file__).with_name("runtime.json")


@dataclass(frozen=True)
class AppRuntime:
    """Expo SDK and native packages compiled into the OpenCuria mobile app."""

    sdk_version: str
    react_native_version: str
    packages: dict[str, str]

    @property
    def sdk_major(self) -> int:
        """Major Expo SDK version, e.g. 57; apps must match it exactly."""
        return int(self.sdk_version.split(".", 1)[0])


@lru_cache(maxsize=1)
def app_runtime() -> AppRuntime:
    """Return the runtime spec shipped with this backend release."""
    data = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
    return AppRuntime(
        sdk_version=str(data["sdk_version"]),
        react_native_version=str(data["react_native_version"]),
        packages={str(k): str(v) for k, v in dict(data["packages"]).items()},
    )


_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
#: Dependency names that usually ship native code. Unknown packages outside
#: these families are assumed to be plain JavaScript.
_NATIVE_FAMILIES = re.compile(r"^(expo-|react-native-|@react-native|@expo/ui$)")


def _parse(version: str) -> tuple[int, int, int] | None:
    match = _VERSION.match(version.strip().lstrip("v="))
    if not match:
        return None
    return int(match[1]), int(match[2]), int(match[3])


def range_allows(spec: str, version: str) -> bool | None:
    """Whether npm range *spec* admits *version*; ``None`` when undecidable.

    Supports the forms Expo projects use: exact versions, ``^``, ``~`` and
    ``x``/``*`` wildcards. Anything else (tags, URLs, ``||``) is undecidable.
    """
    spec = spec.strip()
    target = _parse(version)
    if target is None:
        return None
    if spec in {"", "*", "x", "latest"}:
        return True
    if any(token in spec for token in ("||", " ", ":", "/")):
        return None
    operator = spec[0] if spec[0] in "^~" else ""
    body = spec[1:] if operator else spec
    parts = body.lstrip("v=").split(".")
    if not parts or not parts[0].isdigit():
        return None
    wildcard = next(
        (i for i, part in enumerate(parts) if part in {"x", "X", "*"}), None
    )
    if wildcard is not None:
        fixed = [int(p) for p in parts[:wildcard] if p.isdigit()]
        return list(target[: len(fixed)]) == fixed
    base = _parse(body)
    if base is None:
        return None
    if operator == "":
        return target == base
    if target < base:
        return False
    if operator == "~":
        return target[:2] == base[:2]
    # Caret: the left-most non-zero component is fixed.
    if base[0]:
        return target[0] == base[0]
    if base[1]:
        return target[:2] == base[:2]
    return target == base


def compatible(spec: str, provided: str) -> bool | None:
    """Whether a dependency range fits the runtime's native version.

    Patch releases within a minor version keep native APIs stable, so the
    range's base version must match the runtime's major and minor version
    (``~57.0.23`` fits 57.0.22, ``^4.5.0`` does not fit 4.9.1). Wildcards and
    other forms fall back to :func:`range_allows`.
    """
    base = _parse(spec.strip().lstrip("^~"))
    target = _parse(provided)
    if base is None or target is None:
        return range_allows(spec, provided)
    return base[:2] == target[:2]


def dependency_warnings(dependencies: dict[str, str], runtime: AppRuntime) -> list[str]:
    """Explain dependencies that would not match the app's native runtime."""
    warnings: list[str] = []
    for name, spec in sorted(dependencies.items()):
        # The Expo SDK itself is checked against ``sdk_version``.
        if not isinstance(spec, str) or name == "expo":
            continue
        provided = runtime.packages.get(name)
        if provided is None:
            if _NATIVE_FAMILIES.match(name):
                warnings.append(
                    f"{name} is not part of the OpenCuria app runtime; the app "
                    "fails where it uses native code from it."
                )
            continue
        if compatible(spec, provided) is False:
            warnings.append(
                f"{name}@{spec} does not match the runtime's {provided}; "
                f"install it with `npx expo install {name}`."
            )
    return warnings
