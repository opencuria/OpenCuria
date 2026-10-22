"""Organization lifecycle events published to the apps built on organizations.

Organizations is a foundational context and never imports its consumers.
Apps that keep per-organization defaults (for example credential-service
activations) connect a receiver from their ``AppConfig.ready()``, so
dependencies point one way.
"""

from __future__ import annotations

from django.dispatch import Signal

from .models import Organization

organization_created = Signal()
"""Sent with ``organization`` once it and its admin membership exist.

Receivers run synchronously in the creating call and may use the ORM. A
failing receiver propagates and fails the creation, like any other step.
"""


def publish_organization_created(organization: Organization) -> None:
    """Let subscribed apps seed their defaults for *organization*."""
    organization_created.send(sender=Organization, organization=organization)
