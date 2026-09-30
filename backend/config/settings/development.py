"""
Development settings for opencuria backend.

SQLite, DEBUG=True, relaxed security.
"""

from __future__ import annotations

import os

from .base import *  # noqa: F401, F403
from .base import BASE_DIR

DEBUG = True

MCP_OAUTH_CALLBACK_URL = os.getenv(
    "MCP_OAUTH_CALLBACK_URL", "http://127.0.0.1:8000/api/v1/mcp-oauth/callback/"
).strip()
MCP_OAUTH_FRONTEND_RETURN_URL = os.getenv(
    "MCP_OAUTH_FRONTEND_RETURN_URL", "http://127.0.0.1:5173/?settings=plugins"
).strip()

ALLOWED_HOSTS = ["*"]

# SQLite for local development
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.getenv("SQLITE_PATH", str(BASE_DIR / "db.sqlite3")),
    }
}

# CORS — allow all origins in development
CORS_ALLOW_ALL_ORIGINS = True

# In-memory channel layer for development
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}
