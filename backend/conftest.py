"""Use production SQLite locking semantics in concurrent backend tests."""

import pytest
from django.db import connection


@pytest.fixture(scope="session")
def django_db_modify_db_settings(
    django_db_modify_db_settings_parallel_suffix,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Avoid shared-cache in-memory locks that bypass SQLite's busy timeout."""
    if connection.vendor == "sqlite":
        directory = tmp_path_factory.mktemp("backend-sqlite")
        connection.settings_dict["TEST"]["NAME"] = str(directory / "db.sqlite3")
