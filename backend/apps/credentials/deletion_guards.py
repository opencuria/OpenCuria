"""Deletion guards contributed by the apps built on credentials.

Credentials is a foundational context and never imports its consumers. An
app whose runtime depends on attached credentials (for example workspace
plugins) connects a receiver from its ``AppConfig.ready()`` and reports what
a deletion would break, so dependencies point one way.
"""

from __future__ import annotations

import uuid

from django.dispatch import Signal

from .models import Credential

credential_deletion_requested = Signal()
"""Sent with ``credential`` and ``org_id`` before a credential is deleted.

Receivers return a list of blocking gaps, each a dict with at least
``workspace_id``, ``plugin_id`` and ``key`` (metadata only, never secret
data). Any gap vetoes the deletion. Receiver errors propagate, so a failing
guard blocks the deletion instead of silently allowing it.
"""


def collect_deletion_blockers(
    credential: Credential, *, org_id: uuid.UUID
) -> list[dict]:
    """Return every gap the subscribed guards report for *credential*."""
    responses = credential_deletion_requested.send(
        sender=Credential, credential=credential, org_id=org_id
    )
    return [gap for _receiver, gaps in responses for gap in gaps]
