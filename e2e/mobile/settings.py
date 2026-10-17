"""Isolated settings for the mobile live-backend E2E fixture.

This module is selected only by the opt-in fixture commands. It always uses a
run-scoped SQLite database and Django's in-memory Channels backend.
"""

from __future__ import annotations

import os
from pathlib import Path

from config.settings.development import *

RUN_DIR = Path(os.environ.get("MOBILE_E2E_RUN_DIR", "")).resolve()
SAFE_ROOT = Path("/tmp/opencuria-mobile-e2e").resolve()
if not RUN_DIR.is_relative_to(SAFE_ROOT) or RUN_DIR == SAFE_ROOT:
    raise RuntimeError(
        "MOBILE_E2E_RUN_DIR must be a private child of /tmp/opencuria-mobile-e2e"
    )
RUN_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(RUN_DIR, 0o700)

SECRET_KEY = "mobile-e2e-only-not-a-production-secret"
DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(RUN_DIR / "backend.sqlite3"),
        "OPTIONS": {"transaction_mode": "IMMEDIATE", "timeout": 20},
    }
}
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CORS_ALLOW_ALL_ORIGINS = True
SIO_CORS_ALLOWED_ORIGINS = "*"
JWT_ACCESS_TOKEN_LIFETIME_MINUTES = 180
JWT_REFRESH_TOKEN_LIFETIME_DAYS = 1
MCP_OAUTH_CALLBACK_URL = "http://127.0.0.1:8022/api/v1/mcp-oauth/callback/"
MCP_OAUTH_FRONTEND_RETURN_URL = "http://127.0.0.1:8022/"

# Do not let an accidental settings import choose a shared Redis-backed layer.
assert CHANNEL_LAYERS["default"]["BACKEND"] == "channels.layers.InMemoryChannelLayer"
assert str(DATABASES["default"]["NAME"]).startswith(str(RUN_DIR))
