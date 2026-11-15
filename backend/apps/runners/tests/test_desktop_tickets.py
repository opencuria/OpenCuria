"""Single-use desktop viewer tickets and the pinned KasmVNC client manifest."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from django.core import signing
from django.core.cache import cache

from apps.runners import desktop_tickets, kasmvnc_client
from apps.runners.desktop_tickets import issue_desktop_ticket, redeem_desktop_ticket


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def test_ticket_redeems_once_for_its_workspace():
    workspace_id = str(uuid.uuid4())
    ticket = issue_desktop_ticket(workspace_id, 7)

    assert redeem_desktop_ticket(ticket, str(uuid.uuid4())) is None
    assert redeem_desktop_ticket(ticket, workspace_id) == 7
    assert redeem_desktop_ticket(ticket, workspace_id) is None


def test_ticket_rejects_tampering_expiry_and_foreign_signatures(monkeypatch):
    workspace_id = str(uuid.uuid4())
    ticket = issue_desktop_ticket(workspace_id, 7)
    forged = signing.dumps({"w": workspace_id, "u": 1, "n": "x"}, salt="other")

    assert redeem_desktop_ticket(ticket[:-2] + "xx", workspace_id) is None
    assert redeem_desktop_ticket(forged, workspace_id) is None
    assert redeem_desktop_ticket("", workspace_id) is None

    monkeypatch.setattr(desktop_tickets, "DESKTOP_TICKET_MAX_AGE_SECONDS", -1)
    assert redeem_desktop_ticket(ticket, workspace_id) is None


def test_ticket_carries_no_access_token_material():
    ticket = issue_desktop_ticket(uuid.uuid4(), 7)
    assert len(ticket) < 200
    assert "." not in ticket.split(":")[0]  # not a JWT


def test_manifest_lists_only_typed_runtime_files():
    files = kasmvnc_client.client_files()

    assert files["vnc.html"] == files["index.html"]
    assert {"dist/main.bundle.js", "package.json"} <= files.keys()
    for path, digest in files.items():
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        assert kasmvnc_client.lookup_client_file(f"/{path}") is not None, path
    for path in ("/", "/index.php", "/po/de.po", "/vnc.html?x", "/../vnc.html"):
        assert kasmvnc_client.lookup_client_file(path) is None


def test_manifest_matches_the_kasmvnc_release_installed_in_desktop_images():
    """Regenerate the manifest with scripts/kasmvnc_client_manifest.py on upgrade."""
    recipe = (
        Path(kasmvnc_client.__file__).parent / "scripts" / "qemu_desktop_session.sh"
    ).read_text()
    versions = set(re.findall(r"KasmVNC/releases/download/v([\d.]+)/", recipe))

    assert versions == {kasmvnc_client.client_version()}
