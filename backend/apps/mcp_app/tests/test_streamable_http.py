from __future__ import annotations

from mcp.types import DEFAULT_NEGOTIATED_VERSION
from starlette.testclient import TestClient

from apps.mcp_app.server import build_mcp_app


class _DummyAPIKey:
    user = None

    def has_permission(self, _permission) -> bool:
        return True


def _auth_headers(**extra_headers: str) -> dict[str, str]:
    headers = {
        "Authorization": "Bearer test-token",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    headers.update(extra_headers)
    return headers


def _initialize_payload() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": "init-1",
        "method": "initialize",
        "params": {
            "protocolVersion": DEFAULT_NEGOTIATED_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0.0"},
        },
    }


def test_streamable_http_initialize_uses_primary_mcp_endpoint(monkeypatch):
    monkeypatch.setattr(
        "apps.mcp_app.auth.authenticate_api_key",
        lambda token: _DummyAPIKey() if token == "test-token" else None,
    )

    with TestClient(build_mcp_app()) as client:
        response = client.post(
            "/mcp",
            headers=_auth_headers(),
            json=_initialize_payload(),
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.headers["mcp-session-id"]
        assert response.json()["result"]["protocolVersion"] == DEFAULT_NEGOTIATED_VERSION

        close_response = client.delete(
            "/mcp",
            headers=_auth_headers(
                **{
                    "Mcp-Session-Id": response.headers["mcp-session-id"],
                    "Mcp-Protocol-Version": DEFAULT_NEGOTIATED_VERSION,
                }
            ),
        )
        assert close_response.status_code == 200


def test_streamable_http_rejects_sessionless_non_initialize_post(monkeypatch):
    monkeypatch.setattr(
        "apps.mcp_app.auth.authenticate_api_key",
        lambda token: _DummyAPIKey() if token == "test-token" else None,
    )

    with TestClient(build_mcp_app()) as client:
        response = client.post(
            "/mcp",
            headers=_auth_headers(),
            json={"jsonrpc": "2.0", "id": "1", "method": "tools/list", "params": {}},
        )

    assert response.status_code == 400
    assert "Missing MCP session" in response.json()["error"]


def test_streamable_http_get_without_session_returns_405(monkeypatch):
    monkeypatch.setattr(
        "apps.mcp_app.auth.authenticate_api_key",
        lambda token: _DummyAPIKey() if token == "test-token" else None,
    )

    with TestClient(build_mcp_app()) as client:
        response = client.get(
            "/mcp",
            headers={"Authorization": "Bearer test-token", "Accept": "text/event-stream"},
        )

    assert response.status_code == 405
