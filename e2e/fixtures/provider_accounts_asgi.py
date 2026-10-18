"""Local account E2E: real API/provider with no workspace exec boundary."""

import os
from importlib import import_module

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
django.setup()

harness = import_module("apps.harness.harness_service")

# Provider-account tests need no execution daemon. The production service and
# provider adapters stay real; only the unused workspace-execution boundary is absent.
harness._default_harness_service = harness.HarnessService()

application = import_module("config.asgi").application
