"""Process-wide registration point for the ``HarnessGateway`` port.

The harness app depends on runners, never the other way round. It
registers its gateway implementation from ``HarnessConfig.ready()`` and
the runner service resolves it here, so ``apps.runners.services`` stays
free of ``apps.harness`` imports.
"""

from __future__ import annotations

from ..ports import HarnessGateway

_gateway: HarnessGateway | None = None


def register_harness_gateway(gateway: HarnessGateway) -> None:
    """Install the process-wide harness gateway."""
    global _gateway
    _gateway = gateway


def get_harness_gateway() -> HarnessGateway:
    """Return the registered harness gateway.

    Raises:
        RuntimeError: If the harness app has not registered a gateway.
    """
    if _gateway is None:
        raise RuntimeError("No harness gateway registered")
    return _gateway
