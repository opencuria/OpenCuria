"""Django admin configuration for the credentials app."""

from __future__ import annotations

from django.contrib import admin

from .models import Credential, CredentialService, OrgCredentialServiceActivation


@admin.register(CredentialService)
class CredentialServiceAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "credential_type", "env_var_name", "created_at"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ("name",)}
    fields = [
        "name",
        "slug",
        "description",
        "credential_type",
        "env_var_name",
        "label",
    ]


@admin.register(Credential)
class CredentialAdmin(admin.ModelAdmin):
    list_display = ["name", "organization", "service", "created_by", "created_at"]
    list_filter = ["service", "organization"]
    readonly_fields = [
        "id",
        "encrypted_value",
        "public_key",
        "created_at",
        "updated_at",
    ]


@admin.register(OrgCredentialServiceActivation)
class OrgCredentialServiceActivationAdmin(admin.ModelAdmin):
    list_display = ["id", "organization", "credential_service", "created_at"]
    list_filter = ["organization", "credential_service"]
    search_fields = ["organization__name", "credential_service__name", "credential_service__slug"]
    readonly_fields = ["id", "created_at"]
