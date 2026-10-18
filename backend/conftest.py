"""Use production SQLite locking semantics in concurrent backend tests."""

import pytest
from django.conf import settings
from django.db import connection
from django.test.utils import override_settings


@pytest.fixture(scope="session")
def django_db_modify_db_settings(
    django_db_modify_db_settings_parallel_suffix,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Avoid shared-cache in-memory locks that bypass SQLite's busy timeout."""
    # Test identities never leave the disposable test database. Authentication
    # contracts do not depend on expensive production password stretching.
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    if connection.vendor == "sqlite":
        directory = tmp_path_factory.mktemp("backend-sqlite")
        connection.settings_dict["TEST"]["NAME"] = str(directory / "db.sqlite3")


@pytest.fixture(scope="session", autouse=True)
def fast_test_password_hashing():
    """Avoid production PBKDF work for thousands of disposable fixture users.

    This override is scoped to pytest only. Keep PBKDF2 as a fallback for any
    pre-hashed fixture; production and live application settings are untouched.
    """
    with override_settings(
        PASSWORD_HASHERS=[
            "django.contrib.auth.hashers.MD5PasswordHasher",
            "django.contrib.auth.hashers.PBKDF2PasswordHasher",
        ]
    ):
        yield
