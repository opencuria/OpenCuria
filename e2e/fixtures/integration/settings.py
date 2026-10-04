"""Opt-in localhost PostgreSQL overlay; never imports production credentials."""

import os

from config.settings.development import *

if os.environ.get("INTEGRATION_DB_NAME") != "opencuria_integration":
    raise RuntimeError("Integration overlay requires the dedicated integration DB")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": os.environ.get("INTEGRATION_DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("INTEGRATION_DB_PORT", "5432"),
        "NAME": os.environ["INTEGRATION_DB_NAME"],
        "USER": os.environ["INTEGRATION_DB_USER"],
        "PASSWORD": os.environ["INTEGRATION_DB_PASSWORD"],
    }
}
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [os.environ["REDIS_URL"]],
            "prefix": "opencuria-integration",
        },
    }
}
