"""Seed isolated account E2E identities and serve a loopback-only LLM fixture."""

# Django model imports must follow setup() in this standalone fixture.
# ruff: noqa: E402

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import django

database = Path(os.environ.get("SQLITE_PATH", "")).resolve()
if not str(database).startswith(
    str(Path(__file__).resolve().parents[1] / "test-results" / "provider-accounts")
):
    raise RuntimeError("Provider account E2E requires its disposable database")
output = database.parent
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
django.setup()
from django.contrib.auth import get_user_model

from apps.harness.services import ProviderConfigService
from apps.organizations.models import Membership, Organization
from apps.runners.models import Runner, Workspace


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = json.dumps(
            {
                "data": [
                    {
                        "id": "local/demo",
                        "name": "Local demo",
                        "context_length": 32000,
                        "supported_parameters": ["tools"],
                    }
                ]
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        payload = json.loads(
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
        )
        with open(output / "requests.jsonl", "a") as f:
            f.write(
                json.dumps(
                    {
                        "authorization": self.headers.get("Authorization"),
                        "body": payload,
                    }
                )
                + "\n"
            )
        prompt = " ".join(
            str(x.get("content", ""))
            for x in payload.get("messages", [])
            if x.get("role") == "user"
        )
        streaming = bool(payload.get("stream", True))
        self.send_response(200)
        self.send_header(
            "Content-Type", "text/event-stream" if streaming else "application/json"
        )
        self.end_headers()
        self.wfile.flush()
        if "wait" in prompt.lower():
            threading.Event().wait(15)
        if streaming:
            body = (
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "content": "Local provider response verified."
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 2,
                            "completion_tokens": 3,
                            "total_tokens": 5,
                        },
                    }
                )
                + "\n\ndata: [DONE]\n\n"
            )
        else:
            body = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Local provider response verified.",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 3,
                        "total_tokens": 5,
                    },
                }
            )
        try:
            self.wfile.write(body.encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


org, _ = Organization.objects.get_or_create(
    slug="provider-account-e2e", defaults={"name": "Provider Account E2E"}
)
for role in ("admin", "member", "other"):
    user, _ = get_user_model().objects.get_or_create(
        email=f"provider-{role}@example.test", defaults={"username": f"provider-{role}"}
    )
    user.set_password("Provider-E2E-2026!")
    user.save()
    Membership.objects.get_or_create(
        user=user,
        organization=org,
        defaults={"role": "admin" if role == "admin" else "member"},
    )
member = get_user_model().objects.get(email="provider-member@example.test")
runner, _ = Runner.objects.get_or_create(
    name="Provider test runner",
    organization=org,
    defaults={"api_token_hash": uuid.uuid4().hex, "status": "online"},
)
workspace, _ = Workspace.objects.get_or_create(
    name="Provider account test",
    runner=runner,
    defaults={"created_by": member, "status": "running"},
)
ProviderConfigService().save_config(
    organization_id=org.id, default_model="openai-compatible/local/demo", small_model=""
)
(output / "state.json").write_text(
    json.dumps({"org": str(org.id), "workspace": str(workspace.id)})
)
ThreadingHTTPServer(("127.0.0.1", 8099), Handler).serve_forever()
