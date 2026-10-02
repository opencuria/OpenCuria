"""Test-only OAuth protocol adapter, excluded from production imports."""

from urllib.parse import urlparse

from config.asgi import application as production_application
from apps.credentials import mcp_oauth as oauth_fixture_module
from apps.runners import sio_server as runner_socket_server
from apps.runners.services import RunnerService as FixtureRunnerService


def oauth_protocol_fixture(
    method, url, *, data=None, json_body=None, headers=None
):
    """Answer only fixed fixture provider/resource protocol requests."""
    parsed = urlparse(url)
    if parsed.netloc == "mcp.notion.com" and parsed.path == "/mcp" and method == "GET":
        return 401, {
            "www-authenticate": (
                'Bearer resource_metadata="https://mcp.notion.com/'
                '.well-known/oauth-protected-resource/mcp", scope="mcp"'
            )
        }, None
    if (
        parsed.netloc == "mcp.notion.com"
        and parsed.path == "/.well-known/oauth-protected-resource/mcp"
        and method == "GET"
    ):
        return 200, {}, {
            "resource": "https://mcp.notion.com/mcp",
            "authorization_servers": ["https://issuer.focused-e2e.invalid"],
        }
    if (
        parsed.netloc == "issuer.focused-e2e.invalid"
        and parsed.path == "/.well-known/oauth-authorization-server"
        and method == "GET"
    ):
        return 200, {}, {
            "issuer": "https://issuer.focused-e2e.invalid",
            "authorization_endpoint": "https://issuer.focused-e2e.invalid/authorize",
            "token_endpoint": "https://issuer.focused-e2e.invalid/token",
            "registration_endpoint": "https://issuer.focused-e2e.invalid/register",
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["mcp"],
        }
    if (
        parsed.netloc == "issuer.focused-e2e.invalid"
        and parsed.path == "/register"
        and method == "POST"
    ):
        return 201, {}, {
            "client_id": "focused-e2e-client",
            "token_endpoint_auth_method": "none",
        }
    if (
        parsed.netloc == "issuer.focused-e2e.invalid"
        and parsed.path == "/token"
        and method == "POST"
    ):
        return 200, {}, {
            "access_token": "test-only-provider-access-token",
            "refresh_token": "test-only-provider-refresh-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "mcp",
        }
    raise RuntimeError("Focused OAuth fixture refused an unexpected external endpoint")


def configure_fixtures():
    """Install network and transport seams only after Django initialization."""
    mcp_oauth = oauth_fixture_module
    mcp_oauth._request = oauth_protocol_fixture
    fixture_hosts = {"mcp.notion.com", "issuer.focused-e2e.invalid"}

    def fixture_public_addresses(host: str, port: int) -> tuple[str, ...]:
        """Pin the two fixture hosts and fail closed on every other host."""
        if host not in fixture_hosts:
            raise RuntimeError("Focused OAuth fixture refused an unexpected host")
        return ("8.8.8.8",)

    async def accept_fixture_runner_task(self, runner, event, data):
        """Accept test-only task delivery without starting runner processes."""
        return None

    def get_fixture_runner_service():
        """Return the runner facade with test-only transport replaced."""
        service = FixtureRunnerService()
        service._emit_to_runner = accept_fixture_runner_task.__get__(
            service, FixtureRunnerService
        )
        return service

    mcp_oauth._public_addresses = fixture_public_addresses
    runner_socket_server.get_runner_service = get_fixture_runner_service


application = production_application
configure_fixtures()
