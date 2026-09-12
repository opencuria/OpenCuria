"""Typed Agent-S run configuration (ORM-free config port).

Adapted in part from simular-ai/Agent-S (https://github.com/simular-ai/Agent-S,
commit 3aa272d, Apache License 2.0). See THIRD_PARTY_NOTICES.md at the
repository root. The surrounding OpenCuria project remains AGPL-3.0-only.

Configuration for the second-layer Agent-S integration
(:mod:`apps.harness.agent_s.adapters` /
:mod:`apps.harness.agent_s.harness`). This module deliberately has no
Django/ORM imports: it is the typed configuration port populated and
persisted by the provider-config/API/UI layer
(:class:`apps.harness.services.AgentSConfigService`). The harness
resolves an effective config per run
(see :func:`apps.harness.agent_s.harness.resolve_run_config`):

- ``main_model``: the effective harness model of the run;
- ``grounding_model``: controlled fallback to ``main_model`` when no
  separate grounding model is configured;
- ``desktop_width``/``desktop_height``: real desktop geometry when
  known, otherwise 1920x1080 (``OSWorldACI`` dimensions);
- ``grounding_width``/``grounding_height``: the grounding model's
  output coordinate system. Upstream Agent-S has no universal default —
  the CLI default depends on the grounding model — so OpenCuria keeps
  one explicit default everywhere: the live desktop geometry
  (1920x1080, the recommended UI-TARS 1.5 coordinate system). The
  persisted service (``AgentSConfigService.DEFAULTS``) and this domain
  default agree, so stored, unstored, and explicit-config runs behave
  identically unless a per-org override is saved.

Defaults mirror the Agent-S CLI (``gui_agents/s3/cli_app.py``): 15 max
steps, trajectory length 8, reflection on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Default outer-loop step budget (Agent-S CLI ``run_agent``: ``range(15)``).
DEFAULT_MAX_STEPS = 15

#: Default trajectory length (Agent-S CLI ``--max_trajectory_length``).
DEFAULT_MAX_TRAJECTORY_LENGTH = 8

#: Default screenshot long-edge cap (Agent-S CLI ``max_dim_size=2400``).
DEFAULT_SCREENSHOT_MAX_DIMENSION = 2400

#: Default pre/post action delays (Agent-S CLI ``time.sleep(1.0)``).
DEFAULT_ACTION_PRE_DELAY = 1.0
DEFAULT_ACTION_POST_DELAY = 1.0

#: Default WAIT-signal delay (Agent-S CLI ``time.sleep(5)``).
DEFAULT_WAIT_DELAY = 5.0

#: Fallback desktop geometry while the real geometry is unknown.
FALLBACK_DESKTOP_WIDTH = 1920
FALLBACK_DESKTOP_HEIGHT = 1080


@dataclass(frozen=True)
class AgentSRunConfig:
    """Validated Agent-S run configuration (no ORM).

    ``desktop_width``/``desktop_height`` are the real desktop dimensions
    (``OSWorldACI`` width/height); ``grounding_width``/``grounding_height``
    are the grounding model's output coordinate system.
    ``model_temperature=None`` means the Agent-S SDK default (the provider
    default wiring: the temperature is forwarded as ``None``).
    """

    main_model: str = ""
    grounding_model: str = ""
    desktop_width: int = FALLBACK_DESKTOP_WIDTH
    desktop_height: int = FALLBACK_DESKTOP_HEIGHT
    grounding_width: int = FALLBACK_DESKTOP_WIDTH
    grounding_height: int = FALLBACK_DESKTOP_HEIGHT
    model_temperature: float | None = None
    max_steps: int = DEFAULT_MAX_STEPS
    max_trajectory_length: int = DEFAULT_MAX_TRAJECTORY_LENGTH
    enable_reflection: bool = True
    enable_code_agent: bool = True
    screenshot_max_dimension: int = DEFAULT_SCREENSHOT_MAX_DIMENSION
    action_pre_delay: float = DEFAULT_ACTION_PRE_DELAY
    action_post_delay: float = DEFAULT_ACTION_POST_DELAY
    wait_delay: float = DEFAULT_WAIT_DELAY
    extra: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate every value (raises ``ValueError`` on misuse)."""
        if not self.main_model or not self.main_model.strip():
            raise ValueError("AgentSRunConfig.main_model must not be empty")
        if not self.grounding_model or not self.grounding_model.strip():
            raise ValueError("AgentSRunConfig.grounding_model must not be empty")
        if self.model_temperature is not None:
            temp = self.model_temperature
            if (
                not isinstance(temp, (int, float))
                or isinstance(temp, bool)
                or temp < 0
                or temp > 2
            ):
                raise ValueError(
                    "AgentSRunConfig.model_temperature must be None or a number "
                    f"in 0..2, got {self.model_temperature!r}"
                )
        for name in (
            "desktop_width",
            "desktop_height",
            "grounding_width",
            "grounding_height",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 1
                or value > 7680
            ):
                raise ValueError(
                    f"AgentSRunConfig.{name} must be an int in 1..7680, "
                    f"got {value!r}"
                )
        if (
            not isinstance(self.max_steps, int)
            or isinstance(self.max_steps, bool)
            or self.max_steps < 1
        ):
            raise ValueError(
                f"AgentSRunConfig.max_steps must be a positive int, "
                f"got {self.max_steps!r}"
            )
        if (
            not isinstance(self.max_trajectory_length, int)
            or isinstance(self.max_trajectory_length, bool)
            or self.max_trajectory_length < 1
        ):
            raise ValueError(
                "AgentSRunConfig.max_trajectory_length must be a positive int, "
                f"got {self.max_trajectory_length!r}"
            )
        if (
            not isinstance(self.screenshot_max_dimension, int)
            or isinstance(self.screenshot_max_dimension, bool)
            or self.screenshot_max_dimension < 1
            or self.screenshot_max_dimension > 7680
        ):
            raise ValueError(
                "AgentSRunConfig.screenshot_max_dimension must be an int "
                f"in 1..7680, got {self.screenshot_max_dimension!r}"
            )
        for name in ("action_pre_delay", "action_post_delay", "wait_delay"):
            value = getattr(self, name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value < 0
                or value > 600
            ):
                raise ValueError(
                    f"AgentSRunConfig.{name} must be a number in 0..600, "
                    f"got {value!r}"
                )

    @property
    def effective_grounding_model(self) -> str:
        """Return the grounding model (controlled fallback to main)."""
        grounded = (self.grounding_model or "").strip()
        return grounded or self.main_model.strip()
