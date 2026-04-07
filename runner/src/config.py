"""Runner configuration via pydantic-settings.

All settings are loaded from environment variables with the prefix RUNNER_
and optionally from a .env file in the runner directory.
"""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# Runtime type identifiers
RUNTIME_DOCKER = "docker"
RUNTIME_QEMU = "qemu"


class RunnerSettings(BaseSettings):
    """Central configuration for the runner process."""

    model_config = SettingsConfigDict(
        env_prefix="RUNNER_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Backend connection
    backend_url: str = "wss://api.opencuria.example.com"
    # backend_url: str = "http://localhost:8000"
    api_token: str = ""
    socketio_path: str = "/ws/runner"

    # Runtime selection — comma-separated list of enabled runtimes.
    # Valid values: "docker", "qemu", "docker,qemu"
    enabled_runtimes: str = "docker"

    # Docker settings
    docker_socket: str = "unix:///var/run/docker.sock"
    docker_network: str = "bridge"

    # QEMU/KVM settings
    qemu_image_cache_dir: str = "/var/lib/opencuria/images"
    qemu_disk_dir: str = "/var/lib/opencuria/disks"
    qemu_snapshot_dir: str = "/var/lib/opencuria/snapshots"
    qemu_vcpus: int = 2
    qemu_memory_mb: int = 4096
    qemu_disk_size_gb: int = 50
    qemu_network: str = "default"
    qemu_ssh_key_path: str = str(Path.home() / ".local/share/opencuria/ssh/runner_key")
    qemu_ssh_user: str = "root"
    qemu_ssh_timeout: int = 60  # seconds to wait for VM SSH readiness

    # Heartbeat
    heartbeat_interval: int = 15  # seconds between heartbeats to backend

    # SSH health check — self-healing for QEMU workspaces that become unreachable
    ssh_health_check_interval: int = 30  # seconds between SSH reachability checks
    ssh_unreachable_timeout: int = 90  # seconds before an unreachable workspace is restarted

    # Logging
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "console"

    @property
    def enabled_runtime_list(self) -> list[str]:
        """Parse the comma-separated enabled_runtimes into a list."""
        return [r.strip() for r in self.enabled_runtimes.split(",") if r.strip()]


def get_settings() -> RunnerSettings:
    """Create and return a RunnerSettings instance."""
    return RunnerSettings()
