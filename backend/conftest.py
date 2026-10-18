"""Use production SQLite locking semantics in concurrent backend tests."""

import pytest
from django.conf import settings
from django.db import connection


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
