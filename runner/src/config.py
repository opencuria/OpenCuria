"""Runner configuration via pydantic-settings.

All settings are loaded from environment variables with the prefix RUNNER_
and optionally from a .env file in the runner directory.
"""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class RunnerSettings(BaseSettings):
    """Central configuration for the runner process."""

    model_config = SettingsConfigDict(
        env_prefix="RUNNER_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    state_dir: str = str(Path.home() / ".local/share/opencuria/runner")

    # Backend connection
    backend_url: str = "wss://api.opencuria.example.com"
    # backend_url: str = "http://localhost:8000"
    api_token: str = ""
    socketio_path: str = "/ws/runner"

    # QEMU/KVM settings
    qemu_image_cache_dir: str = "/var/lib/opencuria/images"
    qemu_disk_dir: str = "/var/lib/opencuria/disks"
    qemu_snapshot_dir: str = "/var/lib/opencuria/snapshots"
    qemu_vcpus: int = 2
    qemu_memory_mb: int = 4096
    qemu_disk_size_gb: int = 50
    qemu_ssh_key_path: str = str(Path.home() / ".local/share/opencuria/ssh/runner_key")
    qemu_ssh_user: str = "root"
    qemu_ssh_timeout: int = 60  # seconds to wait for VM SSH readiness

    # Heartbeat
    inventory_interval: int = 120
    heartbeat_interval: int = 15  # seconds between heartbeats to backend

    # Durable ownership maintenance runs even while disconnected.
    desktop_lease_interval: float = 15
    desktop_lease_timeout: float = 10
    desktop_lease_workers: int = 4
    stream_shutdown_timeout: float = 10

    # Logging
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "console"



def get_settings() -> RunnerSettings:
    """Create and return a RunnerSettings instance."""
    return RunnerSettings()
