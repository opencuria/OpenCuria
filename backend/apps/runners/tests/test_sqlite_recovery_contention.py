"""File-backed SQLite recovery transactions wait for a competing writer."""

import sqlite3
from copy import deepcopy
from threading import Event, Thread

import pytest
from django.db import connection
from django.db.backends.sqlite3.base import DatabaseWrapper
from django.test.utils import CaptureQueriesContext

from apps.runners.operations import OperationRepository


@pytest.mark.django_db
def test_idle_delivery_poll_does_not_take_writer_lock(runner):
    with CaptureQueriesContext(connection) as queries:
        assert OperationRepository.candidates() == []
    assert all(
        query["sql"].lstrip().upper().startswith("SELECT")
        for query in queries.captured_queries
    )


def test_atomic_read_then_write_waits_for_competing_writer(tmp_path, django_db_blocker):
    if connection.vendor != "sqlite":
        pytest.skip("SQLite-specific transaction behavior")
    config = deepcopy(connection.settings_dict)
    config["NAME"] = str(tmp_path / "contention.sqlite3")
    writer = sqlite3.connect(config["NAME"], isolation_level=None)
    writer.execute("CREATE TABLE counter (value INTEGER NOT NULL)")
    writer.execute("INSERT INTO counter VALUES (0)")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE counter SET value = value + 1")
    attempting = Event()
    entered = Event()
    finished = Event()
    errors = []
    statements = []

    def recover() -> None:
        db = DatabaseWrapper(config, alias="sqlite-contention")
        try:
            db.ensure_connection()
            db.connection.set_trace_callback(statements.append)
            attempting.set()
            # This is the same BEGIN used by Django's outer atomic blocks.
            db._start_transaction_under_autocommit()
            entered.set()
            with db.cursor() as cursor:
                cursor.execute("SELECT value FROM counter")
                assert cursor.fetchone()[0] == 1
                cursor.execute("UPDATE counter SET value = value + 1")
            db.commit()
        except Exception as exc:
            errors.append(exc)
        finally:
            db.close()
            finished.set()

    with django_db_blocker.unblock():
        thread = Thread(target=recover)
        thread.start()
        try:
            assert attempting.wait(5)
            # DEFERRED would enter immediately, read the old value, and fail
            # its write upgrade. IMMEDIATE waits before its first read.
            assert not entered.wait(0.05)
            assert not finished.is_set()
            writer.commit()
            assert finished.wait(5)
        finally:
            writer.rollback()
            writer.close()
            thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    assert "BEGIN IMMEDIATE" in statements
    with sqlite3.connect(config["NAME"]) as verification:
        assert verification.execute("SELECT value FROM counter").fetchone()[0] == 2
