"""
Django base settings for opencuria backend.

Shared settings across all environments.
"""

from __future__ import annotations

import os
from pathlib import Path

from corsheaders.defaults import default_headers
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# Build paths: BASE_DIR = backend/
BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "insecure-dev-key-change-in-production")

DEBUG = False

ALLOWED_HOSTS: list[str] = []


# --- Application definition ---

INSTALLED_APPS = [
    # Django
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "channels",
    "corsheaders",
    # Local apps
    "apps.accounts",
    "apps.organizations",
    "apps.runners",
    "apps.credentials",
    "apps.skills",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# --- Database ---
# Overridden per environment

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}


# --- Auth ---

AUTH_USER_MODEL = "accounts.User"

# JWT settings (used by DjangoJWTBackend, ignored by KeycloakBackend)
JWT_ACCESS_TOKEN_LIFETIME_MINUTES = int(os.getenv("JWT_ACCESS_TOKEN_LIFETIME_MINUTES", "1440"))
JWT_REFRESH_TOKEN_LIFETIME_DAYS = int(os.getenv("JWT_REFRESH_TOKEN_LIFETIME_DAYS", "30"))

# Pluggable auth backend — swap to KeycloakBackend class path to use Keycloak
AUTH_BACKEND_CLASS = os.getenv(
    "AUTH_BACKEND_CLASS",
    "apps.accounts.auth_backends.DjangoJWTBackend",
)

# Optional external SSO (paid feature)
SSO_ENABLED = os.getenv("SSO_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
SSO_PROVIDER = os.getenv("SSO_PROVIDER", "keycloak").strip().lower() or "keycloak"
SSO_KEYCLOAK_BASE_URL = os.getenv("SSO_KEYCLOAK_BASE_URL", "").strip().rstrip("/")
SSO_KEYCLOAK_REALM = os.getenv("SSO_KEYCLOAK_REALM", "").strip()
SSO_KEYCLOAK_CLIENT_ID = os.getenv("SSO_KEYCLOAK_CLIENT_ID", "").strip()
SSO_KEYCLOAK_CLIENT_SECRET = os.getenv("SSO_KEYCLOAK_CLIENT_SECRET", "").strip()
SSO_KEYCLOAK_SCOPE = os.getenv("SSO_KEYCLOAK_SCOPE", "openid email profile").strip()

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# --- Internationalization ---

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# --- Static files ---

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}


# --- CORS ---

CORS_ALLOW_HEADERS = list(default_headers) + [
    "X-Organization-Id",
]


# --- Django Channels ---

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}


# --- Misc ---

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Socket.IO CORS — default allows all in dev, restricted in production.py
SIO_CORS_ALLOWED_ORIGINS = "*"


# --- Logging (structlog) ---

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "()": "django.utils.log.ServerFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structured",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.getenv("DJANGO_LOG_LEVEL", "INFO"),
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "apps": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
    },
}
