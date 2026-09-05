from __future__ import annotations

from django.contrib import admin

from .models import Skill


@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "user", "organization", "created_by", "created_at"]
    list_filter = ["organization", "created_at"]
    search_fields = ["name", "user__email", "organization__name", "created_by__email"]
    readonly_fields = ["id", "created_at", "updated_at"]


