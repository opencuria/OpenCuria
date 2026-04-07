"""Settings module — imports the active environment settings."""

from __future__ import annotations

import os

environment = os.getenv("DJANGO_ENV", "development")

if environment == "production":
    from .production import *  # noqa: F401, F403
else:
    from .development import *  # noqa: F401, F403
