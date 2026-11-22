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


#: Public address returned for names a test does not list.
FAKE_DNS_PUBLIC_ADDRESS = "93.184.216.34"


@pytest.fixture
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Resolve hosts offline for egress checks; tests edit the returned table.

    IP literals resolve as the system resolver would (``AI_NUMERICHOST``),
    listed names return their answers (``[]`` = NXDOMAIN) and every other
    name returns one public address.
    """
    import socket

    real_getaddrinfo = socket.getaddrinfo
    table: dict[str, list[str]] = {"localhost": ["127.0.0.1", "::1"]}

    def getaddrinfo(host, port, *args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            return real_getaddrinfo(
                host, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST
            )
        except socket.gaierror:
            pass
        answers = table.get(str(host).lower(), [FAKE_DNS_PUBLIC_ADDRESS])
        if not answers:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [
            (
                socket.AF_INET6 if ":" in answer else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (answer, port),
            )
            for answer in answers
        ]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return table
