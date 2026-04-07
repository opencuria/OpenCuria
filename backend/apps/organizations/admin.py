from __future__ import annotations

from django.contrib import admin  # noqa: F401
from .models import *  # noqa: F401

# Register organization models here once implemented.

admin.site.register(Organization)
admin.site.register(Membership)