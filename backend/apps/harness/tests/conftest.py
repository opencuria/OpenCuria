"""Shared fixtures for the harness app tests."""

from __future__ import annotations

import os
import uuid

import pytest

from apps.organizations.models import Organization

# Allow sync ORM calls from async test functions (Django safety check).
os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"


@pytest.fixture
def organization(db) -> Organization:
    """Default organization for harness fixtures."""
    return Organization.objects.create(
        name=f"Harness Org {uuid.uuid4().hex[:6]}",
        slug=f"harness-org-{uuid.uuid4().hex[:10]}",
    )
