"""The image recipe and runtime use the same canonical desktop startup."""

from pathlib import Path

from src.services.sessions.desktop import DESKTOP_SESSION_COMMAND, DesktopManager


def test_image_session_matches_runtime_asset() -> None:
    """A standalone VNC launch receives identical accessibility initialization."""
    recipe = (
        Path(__file__).resolve().parents[2]
        / "backend/apps/runners/scripts/qemu_desktop_session.sh"
    ).read_text()
    image_session = recipe.split("<<'DESKTOP_SESSION'\n", 1)[1].split(
        "\nDESKTOP_SESSION", 1
    )[0]
    assert image_session + "\n" == DESKTOP_SESSION_COMMAND
    assert "exec /root/.vnc/opencuria-session" in recipe


def test_generated_startup_is_valid_bash(tmp_path: Path) -> None:
    """Heredoc boundaries remain valid when the canonical asset is updated."""
    import subprocess

    script = tmp_path / "start.sh"
    script.write_text(DesktopManager._desktop_start_command(1280, 720))
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
