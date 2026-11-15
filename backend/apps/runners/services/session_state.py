"""Instance-owned routing state shared by desktop/session collaborators."""

from dataclasses import dataclass, field


@dataclass
class TerminalStart:
    """Who requested a terminal whose runner confirmation is still pending."""

    workspace_id: str
    user_id: str | None
    client_id: str | None
    requested_at: float


@dataclass
class TerminalRoute:
    """One runner PTY and the frontend connections that keep it attached.

    A terminal lives while at least one owner stays connected and subscribed
    to its workspace; the runner closes it once lease renewals stop.
    """

    workspace_id: str
    runner_id: str | None
    owners: set[str] = field(default_factory=set)


@dataclass
class InteractiveSessionState:
    """Per-RunnerService terminal routes and generation-fenced desktop state."""

    terminals: dict[str, TerminalRoute] = field(default_factory=dict)
    terminal_starts: dict[str, TerminalStart] = field(default_factory=dict)
    desktops: dict[str, dict] = field(default_factory=dict)
    desktop_runners: dict[str, str] = field(default_factory=dict)
    desktop_versions: dict[str, tuple[str, str, int, bool]] = field(
        default_factory=dict
    )
    runner_epochs: dict[str, str] = field(default_factory=dict)
