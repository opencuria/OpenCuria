"""
Pluggable authentication backend abstraction.

Defines an abstract AuthBackend interface that can be swapped between
implementations (e.g. Django-managed JWTs now, Keycloak later) without
changing any consuming code.

The active backend is resolved via AUTH_BACKEND_CLASS in Django settings.
"""

from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from django.conf import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenPair:
    """Access + refresh token pair returned after login / register / refresh."""

    access_token: str
    refresh_token: str


# ---------------------------------------------------------------------------
# Abstract backend
# ---------------------------------------------------------------------------


class AuthBackend(ABC):
    """
    Abstract authentication backend.

    Implementations must provide methods for user creation, credential
    validation, and JWT token management. To swap the entire auth system
    (e.g. to Keycloak), implement a new subclass and update the
    AUTH_BACKEND_CLASS setting.
    """

    @abstractmethod
    def authenticate(self, email: str, password: str):
        """
        Validate credentials and return the User instance.

        Raises AuthenticationError on failure.
        """

    @abstractmethod
    def create_user(self, *, email: str, password: str, **kwargs):
        """
        Create a new user and return the User instance.

        Raises ConflictError if email already exists.
        """

    @abstractmethod
    def generate_tokens(self, user) -> TokenPair:
        """Generate an access/refresh token pair for the given user."""

    @abstractmethod
    def validate_access_token(self, token: str):
        """
        Validate an access token and return the associated User.

        Raises AuthenticationError if the token is invalid or expired.
        """

    @abstractmethod
    def refresh_tokens(self, refresh_token: str) -> TokenPair:
        """
        Validate a refresh token and return a new token pair.

        Raises AuthenticationError if the refresh token is invalid or expired.
        """


# ---------------------------------------------------------------------------
# Django JWT implementation
# ---------------------------------------------------------------------------


class DjangoJWTBackend(AuthBackend):
    """
    JWT auth backend backed by Django's User model and PyJWT.

    Tokens are self-signed using DJANGO_SECRET_KEY. When migrating to
    Keycloak, replace this class with a KeycloakBackend that validates
    Keycloak-issued JWTs instead of self-issuing them.
    """

    def __init__(self) -> None:
        self._secret = settings.SECRET_KEY
        self._algorithm = "HS256"
        self._access_lifetime = timedelta(
            minutes=getattr(settings, "JWT_ACCESS_TOKEN_LIFETIME_MINUTES", 30),
        )
        self._refresh_lifetime = timedelta(
            days=getattr(settings, "JWT_REFRESH_TOKEN_LIFETIME_DAYS", 7),
        )

    # -- Authenticate -------------------------------------------------

    def authenticate(self, email: str, password: str):
        from apps.accounts.models import User
        from common.exceptions import AuthenticationError

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            raise AuthenticationError("Invalid email or password")

        if not user.check_password(password):
            raise AuthenticationError("Invalid email or password")

        if not user.is_active:
            raise AuthenticationError("Account is disabled")

        return user

    # -- Create user --------------------------------------------------

    def create_user(self, *, email: str, password: str, **kwargs):
        from apps.accounts.models import User
        from common.exceptions import ConflictError

        if User.objects.filter(email=email).exists():
            raise ConflictError(f"A user with email '{email}' already exists")

        user = User.objects.create_user(
            username=email,
            email=email,
            password=password,
            **kwargs,
        )
        return user

    # -- Token generation ---------------------------------------------

    def generate_tokens(self, user) -> TokenPair:
        now = datetime.now(timezone.utc)

        access_payload = {
            "sub": str(user.id),
            "email": user.email,
            "type": "access",
            "iat": now,
            "exp": now + self._access_lifetime,
        }

        refresh_payload = {
            "sub": str(user.id),
            "type": "refresh",
            "iat": now,
            "exp": now + self._refresh_lifetime,
        }

        access_token = jwt.encode(access_payload, self._secret, algorithm=self._algorithm)
        refresh_token = jwt.encode(refresh_payload, self._secret, algorithm=self._algorithm)

        return TokenPair(access_token=access_token, refresh_token=refresh_token)

    # -- Token validation ---------------------------------------------

    def validate_access_token(self, token: str):
        from apps.accounts.models import User
        from common.exceptions import AuthenticationError

        try:
            payload = jwt.decode(token, self._secret, algorithms=[self._algorithm])
        except jwt.ExpiredSignatureError:
            raise AuthenticationError("Access token has expired")
        except jwt.InvalidTokenError:
            raise AuthenticationError("Invalid access token")

        if payload.get("type") != "access":
            raise AuthenticationError("Invalid token type")

        user_id = payload.get("sub")
        if not user_id:
            raise AuthenticationError("Invalid token payload")

        try:
            user = User.objects.get(id=int(user_id))
        except (User.DoesNotExist, ValueError):
            raise AuthenticationError("User not found")

        if not user.is_active:
            raise AuthenticationError("Account is disabled")

        return user

    # -- Token refresh ------------------------------------------------

    def refresh_tokens(self, refresh_token: str) -> TokenPair:
        from apps.accounts.models import User
        from common.exceptions import AuthenticationError

        try:
            payload = jwt.decode(refresh_token, self._secret, algorithms=[self._algorithm])
        except jwt.ExpiredSignatureError:
            raise AuthenticationError("Refresh token has expired")
        except jwt.InvalidTokenError:
            raise AuthenticationError("Invalid refresh token")

        if payload.get("type") != "refresh":
            raise AuthenticationError("Invalid token type")

        user_id = payload.get("sub")
        if not user_id:
            raise AuthenticationError("Invalid token payload")

        try:
            user = User.objects.get(id=int(user_id))
        except (User.DoesNotExist, ValueError):
            raise AuthenticationError("User not found")

        if not user.is_active:
            raise AuthenticationError("Account is disabled")

        return self.generate_tokens(user)


# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------

_backend_instance: AuthBackend | None = None


def get_auth_backend() -> AuthBackend:
    """
    Return the configured AuthBackend singleton.

    Reads AUTH_BACKEND_CLASS from settings (default: DjangoJWTBackend).
    """
    global _backend_instance
    if _backend_instance is not None:
        return _backend_instance

    backend_path = getattr(
        settings,
        "AUTH_BACKEND_CLASS",
        "apps.accounts.auth_backends.DjangoJWTBackend",
    )

    module_path, class_name = backend_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    backend_cls = getattr(module, class_name)

    _backend_instance = backend_cls()
    return _backend_instance
