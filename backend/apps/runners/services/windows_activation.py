"""Secret Windows activation input of create, clone and start deliveries.

Like credentials, it is added only to the in-flight delivery and resolved again
for every redelivery; ``OperationRepository.prepare`` never persists it.
"""

from __future__ import annotations

from typing import Any

from apps.licensing.services import WindowsLicenseService

from ..operations import WINDOWS_ACTIVATION_KEY


def windows_activation_payload(workspace: Any) -> dict[str, dict[str, str]]:
    """Return ``{"windows_activation": ...}`` for Windows guests, else nothing."""
    activation = WindowsLicenseService().activation_payload(workspace)
    return {} if activation is None else {WINDOWS_ACTIVATION_KEY: activation}
