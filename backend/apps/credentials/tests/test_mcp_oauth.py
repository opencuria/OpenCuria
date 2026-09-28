"""OAuth state, callback, token rotation, and authorization regression tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
import threading
import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.auth_backends import DjangoJWTBackend
from apps.credentials.enums import CredentialType
from apps.credentials.mcp_oauth import (
    McpOAuthHTTPAuth,
    OAuthError,
    _discover,
    _post_registration,
    start_flow,
)
from apps.credentials.models import (
    Credential,
    CredentialService,
    McpOAuthAuthorizationState,
    McpOAuthClientRegistration,
)
from apps.credentials.services import CredentialSvc
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.plugins.models import Plugin, PluginCredentialRequirement, PluginMcpServer
from apps.plugins.services import PluginService
from apps.runners.models import Runner, Workspace
from apps.runners.services import RunnerService
from common.exceptions import AuthenticationError, ConflictError, NotFoundError
from common.utils import decrypt_value, encrypt_value, hash_token


@pytest.fixture
def context(db, settings):
    settings.MCP_OAUTH_CALLBACK_URL = "http://127.0.0.1:8000/api/v1/mcp-oauth/callback/"
    settings.MCP_OAUTH_FRONTEND_RETURN_URL = "http://127.0.0.1:8080/?settings=plugins"
    user = get_user_model().objects.create_user(
        email=f"oauth-{uuid.uuid4().hex[:8]}@test.local", password="secret"
    )
    org = Organization.objects.create(
        name="OAuth Org", slug=f"oauth-{uuid.uuid4().hex[:8]}"
    )
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    plugin_slug = f"oauth-{uuid.uuid4().hex[:8]}"
    plugin = Plugin.objects.create(
        name="OAuth plugin", slug=plugin_slug, organization=org
    )
    service = CredentialService.objects.create(
        name="OAuth",
        slug=f"{plugin_slug}-oauth",
        organization=org,
        credential_type=CredentialType.MCP_OAUTH,
        plugin_owned=True,
        oauth_plugin_slug=plugin_slug,
        oauth_requirement_key="mcp",
    )
    server = PluginMcpServer.objects.create(
        plugin=plugin,
        name="MCP",
        slug="mcp",
        transport="streamable_http",
        url="https://mcp.example/mcp",
        auth_type="oauth",
        oauth_requirement_key="mcp",
    )
    PluginCredentialRequirement.objects.create(
        plugin=plugin, key="mcp", credential_service=service, required=True
    )
    runner = Runner.objects.create(
        name="runner",
        api_token_hash=hash_token(uuid.uuid4().hex),
        organization=org,
    )
    workspace = Workspace.objects.create(runner=runner, name="ws", created_by=user)
    return user, org, service, plugin, server, workspace


def _registration(server, **kwargs):
    defaults = {
        "client_id": "client",
        "authorization_endpoint": "https://auth.example/tenant/authorize",
        "token_endpoint": "https://auth.example/tenant/token",
        **kwargs,
    }
    registration, _ = McpOAuthClientRegistration.objects.get_or_create(
        server_url=server.url,
        callback_url="http://127.0.0.1:8000/api/v1/mcp-oauth/callback/",
        issuer="https://auth.example/tenant",
        defaults=defaults,
    )
    return registration


def _connect_url(plugin, server):
    return f"/api/v1/mcp-oauth/{plugin.id}/mcp-servers/{server.id}/oauth/connect/"


def _status_url(plugin, server):
    return f"/api/v1/mcp-oauth/{plugin.id}/mcp-servers/{server.id}/oauth/status/"


def _disconnect_url(plugin, server, service_id, org=False):
    return (
        f"/api/v1/mcp-oauth/{plugin.id}/mcp-servers/{server.id}/oauth/disconnect/"
        f"?service_id={service_id}&organization_credential={str(org).lower()}"
    )


def _jwt_client(client, user):
    token = DjangoJWTBackend().generate_tokens(user).access_token
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    return token


@pytest.mark.django_db
def test_oauth_service_crud_is_prohibited_and_resolution_filters_tokens(context):
    user, org, service, _, _, workspace = context
    with pytest.raises(ValueError, match="OAuth connect"):
        CredentialSvc().create_personal_credential(
            service_id=service.id,
            name="manual",
            value="plaintext",
            user=user,
            org_id=org.id,
        )
    registration = _registration(context[4])
    token_data = {
        "access_token": "secret",
        "refresh_token": "refresh",
        "token_type": "Bearer",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "resource": "https://mcp.example/mcp",
        "server_url": context[4].url,
        "server_id": str(context[4].id),
        "registration_id": str(registration.id),
    }
    credential = Credential.objects.create(
        user=user,
        service=service,
        name="OAuth",
        encrypted_value=encrypt_value(json.dumps(token_data)),
        created_by=user,
        oauth_server_id=context[4].id,
        oauth_server_url=context[4].url,
        oauth_resource=token_data["resource"],
        oauth_registration=registration,
        oauth_status="connected",
    )
    ordinary_service = CredentialService.objects.create(
        name="Ordinary token",
        slug=f"ordinary-{uuid.uuid4().hex[:8]}",
        credential_type=CredentialType.ENV,
        env_var_name="ORDINARY_TOKEN",
    )
    ordinary_credential = CredentialSvc().create_personal_credential(
        service_id=ordinary_service.id,
        name="Ordinary token",
        value="ordinary-secret",
        user=user,
        org_id=org.id,
    )
    resolved = CredentialSvc().resolve_credentials(
        [credential.id, ordinary_credential.id], org_id=org.id, user=user
    )
    assert set(resolved.credentials) == {credential, ordinary_credential}
    assert resolved.oauth_credentials == [credential.id]
    assert resolved.env_vars == {"ORDINARY_TOKEN": "ordinary-secret"}
    assert resolved.files == []
    assert resolved.ssh_keys == []
    workspace.credentials.add(credential, ordinary_credential)
    workspace_resolved = CredentialSvc().resolve_workspace_credentials(workspace)
    assert workspace_resolved.oauth_credentials == [credential.id]
    assert workspace_resolved.env_vars == {"ORDINARY_TOKEN": "ordinary-secret"}
    assert workspace_resolved.files == []
    assert workspace_resolved.ssh_keys == []

    credential.oauth_server_url = "https://attacker.example/mcp"
    credential.save(update_fields=["oauth_server_url"])
    with pytest.raises(ConflictError, match="MCP OAuth credential"):
        CredentialSvc().resolve_credentials([credential.id], org_id=org.id, user=user)


@pytest.mark.django_db
def test_dcr_is_json_and_discovery_requires_prm_matching_resource_and_s256(
    monkeypatch,
):
    from apps.credentials import mcp_oauth

    monkeypatch.setattr(
        mcp_oauth,
        "_validated_https_url",
        lambda url, **kwargs: (url, ("93.184.216.34",)),
    )
    seen = {}

    def fake_request(method, url, **kwargs):
        seen.update(method=method, url=url, **kwargs)
        return 201, {}, {"client_id": "registered"}

    monkeypatch.setattr(mcp_oauth, "_request", fake_request)
    assert _post_registration(
        "https://auth.example/register", {"redirect_uris": ["https://back/cb"]}
    ) == {"client_id": "registered"}
    assert seen["method"] == "POST"
    assert seen["json_body"] == {"redirect_uris": ["https://back/cb"]}

    prm = {
        "resource": "https://mcp.example",
        "authorization_servers": ["https://auth.example/tenant"],
        "scopes_supported": ["mcp:read"],
    }
    metadata = {
        "issuer": "https://auth.example/tenant",
        "authorization_endpoint": "https://auth.example/tenant/authorize",
        "token_endpoint": "https://auth.example/tenant/token",
        "code_challenge_methods_supported": ["S256"],
    }
    monkeypatch.setattr(
        mcp_oauth,
        "_request",
        lambda method, url, **kwargs: (
            401,
            {
                "www-authenticate": 'Bearer realm="mcp", scope="mcp:read mcp:write"',
            },
            None,
        ),
    )
    monkeypatch.setattr(
        mcp_oauth,
        "_get_json",
        lambda url: prm if "protected-resource" in url else metadata,
    )
    discovered = _discover("https://mcp.example/mcp")
    assert discovered[2] == "https://mcp.example"
    assert discovered[0]["_challenge_scope"] == "mcp:read mcp:write"
    prm["resource"] = "https://evil.example/mcp"
    with pytest.raises(OAuthError, match="unavailable or mismatched"):
        _discover("https://mcp.example/mcp")
    prm["resource"] = "https://mcp.example"
    metadata["code_challenge_methods_supported"] = []
    with pytest.raises(OAuthError, match="PKCE S256"):
        _discover("https://mcp.example/mcp")


@pytest.mark.django_db
def test_pinned_request_uses_checked_ip_and_disables_redirects(monkeypatch, tmp_path):
    """Exercise HTTPTransport + the real pinned backend over local TLS."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    from apps.credentials import mcp_oauth

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mcp.test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(dt_timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(dt_timezone.utc) + timedelta(minutes=5))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("mcp.test")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / "server.pem"
    key_file = tmp_path / "server-key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "https://127.0.0.1/collect")
            self.end_headers()

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(str(cert_file), str(key_file))
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_create_default_context = ssl.create_default_context

    def trusted_default_context(*, cafile=None, capath=None, cadata=None):
        return original_create_default_context(
            cafile=str(cert_file), capath=capath, cadata=cadata
        )

    monkeypatch.setattr(
        mcp_oauth.ssl, "create_default_context", trusted_default_context
    )
    monkeypatch.setattr(
        mcp_oauth, "_public_addresses", lambda host, port: ("127.0.0.1",)
    )
    try:
        status, headers, _ = mcp_oauth._request(
            "GET", f"https://mcp.test:{server.server_port}/mcp"
        )
        assert status == 302
        assert headers["location"] == "https://127.0.0.1/collect"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.django_db
def test_ssrf_rejects_private_dns_and_callback_urls_are_fixed(monkeypatch, settings):
    from apps.credentials import mcp_oauth

    settings.MCP_OAUTH_CALLBACK_URL = "http://127.0.0.1:8000/api/v1/mcp-oauth/callback/"

    monkeypatch.setattr(
        mcp_oauth.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(OAuthError, match="private or local"):
        mcp_oauth._validated_https_url("https://evil.example/token")
    for url in (
        "http://mcp.example/mcp",
        "https://user:pass@mcp.example/mcp",
        "https://mcp.example/mcp?token=secret",
        "https://mcp.example/mcp#fragment",
    ):
        with pytest.raises(OAuthError):
            mcp_oauth._discover(url)
    settings.MCP_OAUTH_CALLBACK_URL = "https://attacker.example/redirect"
    with pytest.raises(OAuthError, match="fixed valid callback"):
        mcp_oauth.callback_url()
    settings.MCP_OAUTH_CALLBACK_URL = "https://app.example/api/v1/mcp-oauth/callback/"
    settings.MCP_OAUTH_FRONTEND_RETURN_URL = "https://app.example/?settings=plugins"
    assert mcp_oauth.frontend_return_url() == "https://app.example/?settings=plugins"
    settings.MCP_OAUTH_FRONTEND_RETURN_URL = "https://attacker.example/path"
    with pytest.raises(OAuthError, match="fixed safe"):
        mcp_oauth.frontend_return_url()
    settings.MCP_OAUTH_FRONTEND_RETURN_URL = "https://app.example/?settings=credentials"
    with pytest.raises(OAuthError, match="settings=plugins"):
        mcp_oauth.frontend_return_url()

    settings.MCP_OAUTH_CALLBACK_URL = "https://api.example/api/v1/mcp-oauth/callback/"
    settings.MCP_OAUTH_FRONTEND_RETURN_URL = "https://app.example/?settings=plugins"
    with pytest.raises(OAuthError, match="same hostname and scheme"):
        mcp_oauth._validate_browser_flow_origins()


@pytest.mark.django_db
def test_start_flow_persists_hashed_state_verifier_and_scoped_cookie(
    context, monkeypatch
):
    user, org, service, _, server, _ = context
    settings.MCP_OAUTH_CALLBACK_URL = "https://web.example/api/v1/mcp-oauth/callback/"
    settings.MCP_OAUTH_FRONTEND_RETURN_URL = "https://web.example/?settings=plugins"
    registration = _registration(server)
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._discover",
        lambda url: (
            {"_challenge_scope": "mcp:read mcp:write"},
            {"issuer": registration.issuer},
            "https://mcp.example",
        ),
    )
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._registration", lambda *args: registration
    )
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._validated_https_url",
        lambda url, **kwargs: (url, ("93.184.216.34",)),
    )
    auth_url, binding, cookie_name = start_flow(
        user=user,
        organization=org,
        server=server,
        service=service,
        organization_credential=False,
    )
    params = parse_qs(urlparse(auth_url).query)
    row = McpOAuthAuthorizationState.objects.get()
    assert params["code_challenge_method"] == ["S256"]
    assert params["scope"] == ["mcp:read mcp:write"]
    assert params["resource"] == [row.resource]
    assert row.state_hash == hashlib.sha256(params["state"][0].encode()).hexdigest()
    assert decrypt_value(row.encrypted_code_verifier)
    assert cookie_name.startswith("mcp_oauth_")
    assert binding not in row.browser_binding_hash


@pytest.mark.django_db
def test_start_flow_revalidates_cached_authorization_endpoint(context, monkeypatch):
    from apps.credentials import mcp_oauth

    user, org, service, _, server, _ = context
    settings.MCP_OAUTH_CALLBACK_URL = "https://web.example/api/v1/mcp-oauth/callback/"
    settings.MCP_OAUTH_FRONTEND_RETURN_URL = "https://web.example/?settings=plugins"
    registration = _registration(server)
    registration.authorization_endpoint = "http://127.0.0.1/authorize"
    registration.save(update_fields=["authorization_endpoint"])
    monkeypatch.setattr(
        mcp_oauth,
        "_discover",
        lambda url: (
            {"_challenge_scope": ""},
            {"issuer": registration.issuer},
            "https://mcp.example",
        ),
    )
    monkeypatch.setattr(mcp_oauth, "_registration", lambda *args: registration)

    with pytest.raises(OAuthError, match="valid HTTPS"):
        start_flow(
            user=user,
            organization=org,
            server=server,
            service=service,
            organization_credential=False,
        )
    assert not McpOAuthAuthorizationState.objects.exists()


@pytest.mark.django_db
def test_callback_consumes_mismatch_and_failure_and_stores_bound_token(
    context, monkeypatch, settings
):
    user, org, service, _, server, _ = context
    settings.MCP_OAUTH_CALLBACK_URL = "https://web.example/api/v1/mcp-oauth/callback/"
    settings.MCP_OAUTH_FRONTEND_RETURN_URL = "https://web.example/?settings=plugins"
    registration = _registration(server)
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._discover",
        lambda url: (
            {"scopes_supported": []},
            {"issuer": registration.issuer},
            "https://mcp.example",
        ),
    )
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._registration", lambda *args: registration
    )
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._validated_https_url",
        lambda url, **kwargs: (url, ("93.184.216.34",)),
    )
    auth_url, binding, cookie_name = start_flow(
        user=user,
        organization=org,
        server=server,
        service=service,
        organization_credential=False,
    )
    state = parse_qs(urlparse(auth_url).query)["state"][0]
    with pytest.raises(OAuthError):
        from apps.credentials.mcp_oauth import complete_flow

        complete_flow(raw_state=state, binding="wrong", code="code")
    with pytest.raises(OAuthError):
        complete_flow(raw_state=state, binding=binding, code="code")

    auth_url, binding, cookie_name = start_flow(
        user=user,
        organization=org,
        server=server,
        service=service,
        organization_credential=False,
    )
    state = parse_qs(urlparse(auth_url).query)["state"][0]
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._post_token",
        lambda *args, **kwargs: {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "expires_in": 3600,
            "id_token": "notion-id-token",
            "account_id": "notion-account",
            "user_id": "notion-user",
            "workspace_id": "notion-workspace",
            "workspace_name": "My Notion",
            "email_domain": "example.org",
        },
    )
    assert complete_flow(raw_state=state, binding=binding, code="code")
    cred = Credential.objects.get(user=user, service=service)
    assert cred.oauth_server_id == server.id
    assert cred.oauth_resource == "https://mcp.example"
    stored = json.loads(decrypt_value(cred.encrypted_value))
    assert stored["identity"] == {
        "id_token": "notion-id-token",
        "account_id": "notion-account",
        "user_id": "notion-user",
        "workspace_id": "notion-workspace",
        "workspace_name": "My Notion",
        "email_domain": "example.org",
    }
    assert "access-secret" not in cred.encrypted_value
    assert parse_qs(urlparse(settings.MCP_OAUTH_FRONTEND_RETURN_URL).query) == {
        "settings": ["plugins"]
    }
    with pytest.raises(OAuthError):
        complete_flow(raw_state=state, binding=binding, code="code")


@pytest.mark.django_db
def test_request_auth_refreshes_each_call_rotates_and_invalid_grant_reconnects(
    context, monkeypatch
):
    user, org, service, _, server, _ = context
    registration = _registration(server)
    data = {
        "access_token": "old",
        "refresh_token": "r0",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "resource": "https://mcp.example/mcp",
        "server_url": server.url,
        "server_id": str(server.id),
        "registration_id": str(registration.id),
        "identity": {"workspace_id": "w1"},
    }
    credential = Credential.objects.create(
        user=user,
        service=service,
        name="OAuth",
        encrypted_value=encrypt_value(json.dumps(data)),
        created_by=user,
        oauth_server_id=server.id,
        oauth_server_url=server.url,
        oauth_resource="https://mcp.example/mcp",
        oauth_status="connected",
        oauth_registration=registration,
    )
    refreshes = []

    def refresh(*args, **kwargs):
        refreshes.append(args)
        return {
            "access_token": "new",
            "refresh_token": "r1",
            "expires_in": 3600,
            "email_domain": "new.example",
        }

    from apps.credentials.mcp_oauth import access_token_for_request

    monkeypatch.setattr("apps.credentials.mcp_oauth._post_token", refresh)
    assert access_token_for_request(credential.id, server.id, server.url) == "old"
    assert not refreshes
    stored = json.loads(
        decrypt_value(Credential.objects.get(pk=credential.pk).encrypted_value)
    )
    assert stored["refresh_token"] == "r0"
    assert stored["identity"]["workspace_id"] == "w1"

    data["expires_at"] = "2000-01-01T00:00:00+00:00"
    credential.encrypted_value = encrypt_value(json.dumps(data))
    credential.save(update_fields=["encrypted_value"])
    assert access_token_for_request(credential.id, server.id, server.url) == "new"
    assert len(refreshes) == 1
    stored = json.loads(
        decrypt_value(Credential.objects.get(pk=credential.pk).encrypted_value)
    )
    assert stored["refresh_token"] == "r1"
    assert stored["identity"]["workspace_id"] == "w1"
    assert stored["identity"]["email_domain"] == "new.example"
    assert access_token_for_request(credential.id, server.id, server.url) == "new"
    assert len(refreshes) == 1
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._post_token",
        lambda *args, **kwargs: {"error": "invalid_grant"},
    )
    stored["expires_at"] = "2000-01-01T00:00:00+00:00"
    credential.encrypted_value = encrypt_value(json.dumps(stored))
    credential.save(update_fields=["encrypted_value"])
    assert access_token_for_request(credential.id, server.id, server.url) is None
    credential.refresh_from_db()
    assert credential.oauth_status == "reconnect_required"
    assert credential.encrypted_value == ""
    assert credential.encrypted_value == ""


@pytest.mark.django_db
def test_auth_handler_resolves_each_request_and_never_leaks_on_redirect(
    context, monkeypatch
):
    user, org, service, _, server, _ = context

    async def resolve(credential_id, server_id, url):
        return f"token-{credential_id}"

    monkeypatch.setattr(
        "apps.credentials.mcp_oauth.sync_to_async",
        lambda func, thread_sensitive=True: (
            lambda *args: asyncio.sleep(0, result=func(*args))
        ),
    )
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth.access_token_for_request",
        lambda *args: f"token-{args[0]}",
    )
    auth = McpOAuthHTTPAuth(uuid.uuid4(), server.id, server.url)

    async def run_flow():
        request = httpx.Request("POST", server.url)
        flow = auth.async_auth_flow(request)
        prepared = await flow.__anext__()
        assert prepared.headers["Authorization"].startswith("Bearer token-")
        response = httpx.Response(
            307,
            request=prepared,
            headers={"Location": "https://attacker.example/collect"},
        )
        with pytest.raises(StopAsyncIteration):
            await flow.asend(response)
        escaped = httpx.Request("POST", "https://attacker.example/collect")
        with pytest.raises(Exception):
            await auth.async_auth_flow(escaped).__anext__()

    import asyncio

    asyncio.run(run_flow())


@pytest.mark.django_db
def test_api_requires_real_jwt_and_status_disconnect_permissions(
    context, monkeypatch, settings
):
    user, org, service, plugin, server, workspace = context
    settings.DEBUG = True
    settings.MCP_OAUTH_CALLBACK_URL = "http://127.0.0.1:8000/api/v1/mcp-oauth/callback/"
    settings.MCP_OAUTH_FRONTEND_RETURN_URL = "http://127.0.0.1:8080/?settings=plugins"
    client = Client()
    response = client.get(
        _status_url(plugin, server), HTTP_X_ORGANIZATION_ID=str(org.id)
    )
    assert response.status_code == 401
    access_token = _jwt_client(client, user)
    response = client.get(
        _status_url(plugin, server), HTTP_X_ORGANIZATION_ID=str(org.id)
    )
    assert response.status_code == 200
    assert response.json() == {
        "personal": {
            "connected": False,
            "credential_id": None,
            "expires_at": None,
            "reconnect_required": False,
        },
        "organization": {
            "connected": False,
            "credential_id": None,
            "expires_at": None,
            "reconnect_required": False,
        },
    }
    registration = _registration(server)
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._discover",
        lambda url: (
            {"_challenge_scope": ""},
            {"issuer": registration.issuer},
            "https://mcp.example",
        ),
    )
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._registration",
        lambda *args: registration,
    )
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._validated_https_url",
        lambda url, **kwargs: (url, ("93.184.216.34",)),
    )
    response = client.post(
        _connect_url(plugin, server),
        data=json.dumps({"service_id": str(service.id)}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
        HTTP_X_ORGANIZATION_ID=str(org.id),
        HTTP_HOST="127.0.0.1:8000",
    )
    assert response.status_code == 200, response.content
    assert "access_token" not in response.content.decode()
    cookies = [name for name in response.cookies if name.startswith("mcp_oauth_")]
    assert len(cookies) == 1
    cookie_name = cookies[0]
    assert response.cookies[cookie_name]["httponly"]
    assert response.cookies[cookie_name]["samesite"] == "Lax"
    assert response.cookies[cookie_name]["path"] == "/api/v1/mcp-oauth/callback/"

    auth_query = parse_qs(urlparse(response.json()["authorization_url"]).query)
    callback = client.get(
        f"/api/v1/mcp-oauth/callback/?code=one-time-provider-code&state={auth_query['state'][0]}",
        HTTP_COOKIE=f"{cookie_name}={response.cookies[cookie_name].value}",
        HTTP_HOST="127.0.0.1:8000",
    )
    assert callback.status_code == 302
    callback_target = callback["Location"]
    assert callback_target == (
        "http://127.0.0.1:8080/?settings=plugins&mcp_oauth=error"
    )
    assert "one-time-provider-code" not in callback_target
    assert "code=" not in callback_target
    assert "state=" not in callback_target
    assert not Credential.objects.filter(user=user, service=service).exists()
    assert callback.cookies[cookie_name].value == ""
    assert callback.cookies[cookie_name]["max-age"] == 0
    org_user = get_user_model().objects.create_user(
        email="member@test.local", password="secret"
    )
    Membership.objects.create(
        user=org_user, organization=org, role=MembershipRole.MEMBER
    )
    other_client = Client()
    member_token = _jwt_client(other_client, org_user)
    response = other_client.post(
        _connect_url(plugin, server),
        data=json.dumps(
            {"service_id": str(service.id), "organization_credential": True}
        ),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {member_token}",
        HTTP_X_ORGANIZATION_ID=str(org.id),
    )
    assert response.status_code == 403
    cross_org = Organization.objects.create(
        name="Other", slug=f"cross-{uuid.uuid4().hex[:8]}"
    )
    response = other_client.get(
        _status_url(plugin, server),
        HTTP_AUTHORIZATION=f"Bearer {member_token}",
        HTTP_X_ORGANIZATION_ID=str(cross_org.id),
    )
    assert response.status_code == 404

    registration = _registration(server)
    shared_token = {
        "access_token": "shared-secret",
        "refresh_token": "shared-refresh",
        "token_type": "Bearer",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "resource": "https://mcp.example/mcp",
        "server_url": server.url,
        "server_id": str(server.id),
        "registration_id": str(registration.id),
    }
    organization_credential = Credential.objects.create(
        organization=org,
        service=service,
        name="Shared OAuth",
        encrypted_value=encrypt_value(json.dumps(shared_token)),
        created_by=user,
        oauth_server_id=server.id,
        oauth_server_url=server.url,
        oauth_resource=shared_token["resource"],
        oauth_registration=registration,
        oauth_status="connected",
    )
    member_status = other_client.get(
        _status_url(plugin, server),
        HTTP_AUTHORIZATION=f"Bearer {member_token}",
        HTTP_X_ORGANIZATION_ID=str(org.id),
    )
    assert member_status.status_code == 200
    assert member_status.json()["organization"]["credential_id"] == str(
        organization_credential.id
    )
    admin_status = client.get(
        _status_url(plugin, server),
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
        HTTP_X_ORGANIZATION_ID=str(org.id),
    )
    assert admin_status.json()["organization"]["credential_id"] == str(
        organization_credential.id
    )


@pytest.mark.django_db
def test_generic_delete_requires_oauth_disconnect_and_checks_ownership_first(context):
    user, org, service, _, server, workspace = context
    registration = _registration(server)
    credential = Credential.objects.create(
        user=user,
        service=service,
        name="OAuth",
        encrypted_value="",
        created_by=user,
        oauth_server_id=server.id,
        oauth_server_url=server.url,
        oauth_resource="https://mcp.example/mcp",
        oauth_registration=registration,
        oauth_status="disconnected",
    )
    workspace.credentials.add(credential)
    svc = CredentialSvc()

    with pytest.raises(ConflictError) as error:
        svc.delete_credential(
            credential.id, org_id=org.id, user=user, is_admin=True
        )
    assert error.value.code == "oauth_flow_required"
    assert Credential.objects.filter(pk=credential.id).exists()

    client = Client()
    access_token = _jwt_client(client, user)
    response = client.delete(
        f"/api/v1/credentials/{credential.id}/",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
        HTTP_X_ORGANIZATION_ID=str(org.id),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "oauth_flow_required"
    assert Credential.objects.filter(pk=credential.id).exists()

    other_user = get_user_model().objects.create_user(
        email=f"other-{uuid.uuid4().hex[:8]}@test.local", password="secret"
    )
    with pytest.raises(NotFoundError):
        svc.delete_credential(
            credential.id, org_id=org.id, user=other_user, is_admin=True
        )

    org_credential = Credential.objects.create(
        organization=org,
        service=service,
        name="Shared OAuth",
        encrypted_value="",
        created_by=user,
        oauth_server_id=server.id,
        oauth_server_url=server.url,
        oauth_resource="https://mcp.example/mcp",
        oauth_registration=registration,
        oauth_status="disconnected",
    )
    with pytest.raises(AuthenticationError):
        svc.delete_credential(
            org_credential.id, org_id=org.id, user=other_user, is_admin=False
        )
    assert Credential.objects.filter(pk=org_credential.id).exists()


@pytest.mark.django_db(transaction=True)
def test_disconnect_preserves_attachment_and_allows_workspace_detach(context):
    user, org, service, plugin, server, workspace = context
    PluginService().set_org_activation(plugin.id, org_id=org.id, user=user, active=True)
    registration = _registration(server)
    token_data = {
        "access_token": "access",
        "refresh_token": "refresh",
        "token_type": "Bearer",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "resource": "https://mcp.example/mcp",
        "server_url": server.url,
        "server_id": str(server.id),
        "registration_id": str(registration.id),
    }
    credential = Credential.objects.create(
        user=user,
        service=service,
        name="OAuth",
        encrypted_value=encrypt_value(json.dumps(token_data)),
        created_by=user,
        oauth_server_id=server.id,
        oauth_server_url=server.url,
        oauth_resource="https://mcp.example/mcp",
        oauth_status="connected",
        oauth_registration=registration,
    )
    workspace.credentials.add(credential)
    PluginService().set_workspace_plugins(
        workspace=workspace, org_id=org.id, user=user, plugin_ids=[plugin.id]
    )
    from apps.credentials.mcp_oauth import disconnect_credential

    assert disconnect_credential(
        credential.id, server_id=server.id, server_url=server.url
    )
    credential.refresh_from_db()
    assert credential.encrypted_value == ""
    assert credential.oauth_status == "disconnected"
    assert workspace.credentials.filter(pk=credential.pk).exists()
    gaps = PluginService().validate_workspace_credential_removal(
        workspace=workspace,
        org_id=org.id,
        remaining_service_ids=set(),
        remaining_credential_ids=set(),
    )
    assert gaps == []
    asyncio.run(
        RunnerService().update_workspace(
            workspace.id,
            credentials=[],
            resolved_credentials=CredentialSvc().resolve_credentials(
                [], org_id=org.id, user=user
            ),
        )
    )
    assert not workspace.credentials.filter(pk=credential.pk).exists()
    readiness = PluginService().list_workspace_plugins(
        workspace=workspace, org_id=org.id
    )[0]
    assert readiness["ready"] is False
