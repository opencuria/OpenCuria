from __future__ import annotations

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import APIKey, User

admin.site.register(User, UserAdmin)


@admin.register(APIKey)
class APIKeyAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "user", "key_prefix", "is_active", "created_at", "expires_at"]
    list_filter = ["is_active", "created_at", "expires_at"]
    search_fields = ["name", "key_prefix", "user__email"]
    readonly_fields = ["id", "key_hash", "key_prefix", "created_at", "last_used_at"]
