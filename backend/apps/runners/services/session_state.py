"""Instance-owned routing state shared by desktop/session collaborators."""

from dataclasses import dataclass, field


@dataclass
class InteractiveSessionState:
    """One facade's terminal routes and generation-fenced desktop observations."""

    terminals: dict[str, str] = field(default_factory=dict)
    terminal_runners: dict[str, str] = field(default_factory=dict)
    desktops: dict[str, dict] = field(default_factory=dict)
    desktop_runners: dict[str, str] = field(default_factory=dict)
    desktop_versions: dict[str, tuple[str, str, int, bool]] = field(
        default_factory=dict
    )
    runner_epochs: dict[str, str] = field(default_factory=dict)
