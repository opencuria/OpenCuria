"""Durable, secret-free ownership of provider OAuth token refreshes."""

from django.db import models


class ProviderRefreshLease(models.Model):
    """One account-scoped lease shared by native and external-engine workers."""

    account = models.OneToOneField(
        "harness.ProviderConnection",
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="refresh_lease",
    )
    owner = models.UUIDField(null=True, blank=True)
    generation = models.PositiveBigIntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "harness_provider_refresh_lease"
