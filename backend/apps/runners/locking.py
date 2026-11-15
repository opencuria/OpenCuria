"""Lifecycle serialization boundary shared by PostgreSQL and SQLite."""

import uuid

from django.db import connection
from django.db.models import F

from .models import Runner


def lock_runner(runner_id: uuid.UUID | str) -> Runner:
    """Lock runner first; SQLite needs a conditional write before graph reads.

    PostgreSQL uses row locks; SQLite has no SELECT FOR UPDATE and acquires its
    reserved writer lock with this no-op write inside the caller's transaction.
    """
    if connection.vendor == "sqlite":
        Runner.objects.filter(pk=runner_id).update(status=F("status"))
    return Runner.objects.select_for_update().get(pk=runner_id)
