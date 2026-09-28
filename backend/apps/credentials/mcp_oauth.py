"""Server-side OAuth authorization-code + PKCE for HTTP MCP services."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import secrets
import socket
import ssl
import time
from datetime import timedelta
from urllib.parse import quote_plus, urlencode, urlparse

import httpcore
import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.credentials.enums import CredentialType
from common.utils import decrypt_value, encrypt_value

from .models import Credential, McpOAuthAuthorizationState, McpOAuthClientRegistration

STATE_TTL = timedelta(minutes=10)
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_RESPONSE = 256 * 1024


class OAuthError(ValueError):
    """Sanitized OAuth protocol/configuration error."""


class McpOAuthUnauthorizedError(RuntimeError):
    """The bound OAuth credential is unavailable or rejected."""


def _public_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve once, reject non-global answers, and pin requests to checked IPs."""
    try:
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise OAuthError("OAuth endpoint host could not be resolved") from None
    addresses = []
    for result in results:
        try:
            address = ipaddress.ip_address(result[4][0])
        except ValueError:
            raise OAuthError("OAuth endpoint DNS response is invalid") from None
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global:
            raise OAuthError("OAuth endpoints cannot target private or local addresses")
        addresses.append(str(address))
    if not addresses:
        raise OAuthError("OAuth endpoint host could not be resolved")
    return tuple(dict.fromkeys(addresses))


def _validated_https_url(
    url: str, *, oauth_server_url: bool = False
) -> tuple[str, tuple[str, ...]]:
    if not isinstance(url, str) or not url or any(
        ord(char) < 0x21 for char in url
    ):
        raise OAuthError("OAuth endpoint URL is invalid")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or "@" in parsed.netloc
        or parsed.fragment
        or (oauth_server_url and (parsed.query or "?" in url or "#" in url))
    ):
        raise OAuthError("OAuth endpoints must be valid HTTPS URLs")
    try:
        port = parsed.port or 443
    except ValueError:
        raise OAuthError("OAuth endpoint URL is invalid") from None
    if not 1 <= port <= 65535:
        raise OAuthError("OAuth endpoint URL is invalid")
    addresses = _public_addresses(parsed.hostname, port)
    return url, addresses


class _PinnedBackend(httpcore.NetworkBackend):
    """httpcore backend avoiding a second, untrusted DNS lookup."""

    def __init__(self, host: str, addresses: tuple[str, ...]):
        self.host = host.lower()
        self.addresses = addresses

    def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        if host.lower() != self.host:
            raise httpcore.ConnectError("OAuth request escaped its pinned host")
        last_error = None
        for address in self.addresses:
            try:
                sock = socket.create_connection(
                    (address, port),
                    timeout,
                    source_address=(local_address, 0) if local_address else None,
                )
                for option in socket_options or ():
                    sock.setsockopt(*option)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                from httpcore._backends.sync import SyncStream

                return SyncStream(sock)
            except OSError as exc:
                last_error = exc
        raise httpcore.ConnectError("OAuth endpoint connection failed") from last_error

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("OAuth endpoints cannot use local sockets")

    def sleep(self, seconds):
        time.sleep(seconds)


def _request(method, url, *, data=None, json_body=None, headers=None):
    """Bounded pinned HTTP; no redirects/proxies and sanitized transport errors."""
    _, addresses = _validated_https_url(url)
    parsed = urlparse(url)
    transport = httpx.HTTPTransport(trust_env=False)
    # httpx lacks a public custom-network-backend hook; replace its core pool
    # explicitly so DNS is not looked up again after address validation.
    transport._pool = httpcore.ConnectionPool(
        network_backend=_PinnedBackend(parsed.hostname, addresses),
        max_connections=1,
        max_keepalive_connections=0,
        ssl_context=ssl.create_default_context(),
    )
    try:
        with httpx.Client(
            transport=transport,
            timeout=HTTP_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream(
                method,
                url,
                data=data,
                json=json_body,
                headers={"Accept": "application/json", **(headers or {})},
            ) as response:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise OAuthError("OAuth endpoint response is too large")
                body = None
                if raw:
                    try:
                        body = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        body = None
                return response.status_code, dict(response.headers), body
    except OAuthError:
        raise
    except Exception:
        raise OAuthError("OAuth endpoint request failed") from None


def _get_json(url: str) -> dict:
    status, _, payload = _request("GET", url)
    if status >= 400 or not isinstance(payload, dict):
        raise OAuthError("OAuth metadata request failed")
    return payload


def _post_registration(url: str, payload: dict) -> dict:
    status, _, payload = _request(
        "POST", url, json_body=payload, headers={"Content-Type": "application/json"}
    )
    if status >= 400 or not isinstance(payload, dict):
        raise OAuthError("OAuth client registration failed")
    return payload


def _request_basic_token(url, data, auth):
    # RFC 6749 section 2.3.1 applies application/x-www-form-urlencoded
    # encoding to each credential separately before joining with a colon.
    pair = f"{quote_plus(auth[0], safe='')}:{quote_plus(auth[1], safe='')}"
    encoded = base64.b64encode(pair.encode()).decode("ascii")
    return _request(
        "POST",
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {encoded}",
        },
    )


def _post_token(url, form, *, auth=None, post_client_secret=""):
    data = dict(form)
    if post_client_secret:
        data["client_secret"] = post_client_secret
    if auth is not None:
        status, _, result = _request_basic_token(url, data, auth)
    else:
        status, _, result = _request(
            "POST",
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if status >= 400:
        if isinstance(result, dict) and result.get("error") == "invalid_grant":
            return result
        raise OAuthError("OAuth token request failed")
    if not isinstance(result, dict):
        raise OAuthError("OAuth token response is invalid")
    return result


def _issuer_metadata(issuer: str) -> dict:
    _validated_https_url(issuer)
    parsed = urlparse(issuer)
    path = parsed.path.rstrip("/")
    origin = f"https://{parsed.netloc}"
    candidates = []
    if path:
        # RFC 8414 well-known insertion: place the well-known component
        # before the issuer path (not after it). Keep the historical suffix
        # variants as compatibility fallbacks, still requiring issuer match.
        candidates.extend(
            [
                f"{origin}/.well-known/oauth-authorization-server{path}",
                f"{origin}/.well-known/openid-configuration{path}",
                f"{origin}{path}/.well-known/oauth-authorization-server",
                f"{issuer.rstrip('/')}/.well-known/openid-configuration",
                f"{origin}{path}/.well-known/openid-configuration",
            ]
        )
    candidates.extend(
        [
            f"{origin}/.well-known/oauth-authorization-server",
            f"{origin}/.well-known/openid-configuration",
        ]
    )
    for candidate in dict.fromkeys(candidates):
        try:
            payload = _get_json(candidate)
        except OAuthError:
            continue
        if payload.get("issuer") == issuer:
            return payload
    raise OAuthError("OAuth authorization server metadata is unavailable")


def _resource_matches(server_url: str, resource: str) -> bool:
    try:
        server, candidate = urlparse(server_url), urlparse(resource)
        if (
            candidate.scheme != "https"
            or not candidate.hostname
            or candidate.username
            or candidate.password
            or candidate.query
            or candidate.fragment
        ):
            return False
        server_port, resource_port = server.port or 443, candidate.port or 443
    except ValueError:
        return False
    if (
        server.scheme != "https"
        or server.hostname.lower() != candidate.hostname.lower()
        or server_port != resource_port
    ):
        return False
    server_path = (server.path or "/").rstrip("/") or "/"
    resource_path = (candidate.path or "/").rstrip("/") or "/"
    return server_path == resource_path or server_path.startswith(
        resource_path.rstrip("/") + "/"
    )


def _validated_scope(value: str) -> str:
    """Return a bounded OAuth scope string (RFC 6749 scope-token chars)."""
    if len(value) > 2048:
        raise OAuthError("OAuth scope is invalid")
    parts = value.split()
    if any(not re.fullmatch(r"[\x21\x23-\x5B\x5D-\x7E]+", part) for part in parts):
        raise OAuthError("OAuth scope is invalid")
    return " ".join(parts)


def _challenge_scope_from_header(challenge: str) -> str:
    bearer = re.search(r"(?i)Bearer\s+", challenge)
    if not bearer:
        return ""
    rest = challenge[bearer.end() :]
    next_scheme = re.search(r"(?i),\s*(?:Basic|Digest|Bearer)\s+", rest)
    params = rest[: next_scheme.start()] if next_scheme else rest
    match = re.search(r'(?i)\bscope\s*=\s*(?:"([^"]*)"|([^,\s]+))', params)
    value = (match.group(1) or match.group(2) or "").strip() if match else ""
    try:
        return _validated_scope(value)
    except OAuthError:
        return ""


def _validate_resource_metadata_endpoint(server_url: str, endpoint: str) -> None:
    _validated_https_url(server_url, oauth_server_url=True)
    server, candidate = urlparse(server_url), urlparse(endpoint)
    try:
        server_port, candidate_port = server.port or 443, candidate.port or 443
    except ValueError:
        raise OAuthError("Protected resource metadata URL is invalid") from None
    if (
        candidate.scheme != "https"
        or candidate.hostname != server.hostname
        or candidate_port != server_port
        or candidate.username
        or candidate.password
        or candidate.fragment
        or candidate.query
    ):
        raise OAuthError("Protected resource metadata URL escaped its MCP origin")


def _discover(server_url: str):
    if not isinstance(server_url, str) or not server_url:
        raise OAuthError("OAuth MCP server URL is invalid")
    try:
        from apps.plugins.services import validate_oauth_server_url

        normalized_server_url = validate_oauth_server_url(server_url)
    except ValueError:
        raise OAuthError("OAuth MCP server URL must be a valid HTTPS URL") from None
    if normalized_server_url != server_url:
        raise OAuthError("OAuth MCP server URL must be normalized")
    _validated_https_url(server_url, oauth_server_url=True)
    parsed = urlparse(server_url)
    origin = f"https://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    scope, metadata_url = "", ""
    try:
        status, headers, _ = _request("GET", server_url)
    except OAuthError:
        status, headers = 0, {}
    if status == 401:
        challenge = headers.get("www-authenticate", "")
        scope = _challenge_scope_from_header(challenge)
        match = re.search(
            r'(?i)resource_metadata\s*=\s*(?:"([^"\s]+)"|([^,\s]+))', challenge
        )
        metadata_url = (match.group(1) or match.group(2)) if match else ""
    candidates = [metadata_url] if metadata_url else []
    if path:
        # RFC 9728 defines path-inserted protected-resource well-known URLs.
        candidates.extend(
            [
                f"{origin}/.well-known/oauth-protected-resource{path}",
                f"{origin}{path}/.well-known/oauth-protected-resource",
            ]
        )
    candidates.append(f"{origin}/.well-known/oauth-protected-resource")
    protected = None
    for endpoint in dict.fromkeys(candidates):
        if metadata_url and endpoint == metadata_url:
            _validate_resource_metadata_endpoint(server_url, endpoint)
        try:
            document = _get_json(endpoint)
        except OAuthError:
            continue
        if isinstance(document.get("resource"), str) and _resource_matches(
            server_url, document["resource"]
        ):
            protected = document
            break
    if protected is None:
        raise OAuthError("MCP protected resource metadata is unavailable or mismatched")
    auth_servers = protected.get("authorization_servers")
    if (
        not isinstance(auth_servers, list)
        or not auth_servers
        or not isinstance(auth_servers[0], str)
    ):
        raise OAuthError("MCP metadata does not identify an authorization server")
    # An Authorization Server Identifier is an HTTPS issuer URL and the
    # PRM issuer must be accepted as-is; no discovery fallback is allowed.
    issuer = auth_servers[0]
    _validated_https_url(issuer)
    metadata = _issuer_metadata(issuer)
    for key in ("authorization_endpoint", "token_endpoint"):
        if not isinstance(metadata.get(key), str):
            raise OAuthError("OAuth server metadata is missing required endpoints")
        _validated_https_url(metadata[key])
    if metadata.get("issuer") != issuer:
        raise OAuthError("OAuth issuer does not match protected resource metadata")
    methods = metadata.get("code_challenge_methods_supported")
    if not isinstance(methods, list) or "S256" not in methods:
        raise OAuthError("OAuth server must support PKCE S256")
    protected = dict(protected)
    protected["_challenge_scope"] = scope
    return protected, metadata, protected["resource"]


def _registration(server_url, callback, protected, metadata):
    issuer = metadata["issuer"]
    existing = McpOAuthClientRegistration.objects.filter(
        server_url=server_url, callback_url=callback, issuer=issuer
    ).first()
    if existing:
        return existing
    endpoint = metadata.get("registration_endpoint")
    if not isinstance(endpoint, str):
        raise OAuthError("OAuth server does not support dynamic client registration")
    supported = metadata.get("token_endpoint_auth_methods_supported") or [
        "none",
        "client_secret_basic",
        "client_secret_post",
    ]
    if not isinstance(supported, list):
        raise OAuthError("OAuth token authentication metadata is invalid")
    method = next(
        (
            value
            for value in ("none", "client_secret_basic", "client_secret_post")
            if value in supported
        ),
        None,
    )
    if method is None:
        raise OAuthError("OAuth server has no supported client authentication method")
    _validated_https_url(endpoint)
    result = _post_registration(
        endpoint,
        {
            "client_name": "OpenCuria",
            "redirect_uris": [callback],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": method,
        },
    )
    client_id = result.get("client_id")
    returned_method = result.get("token_endpoint_auth_method", method)
    secret = result.get("client_secret", "")
    if not isinstance(client_id, str) or not client_id or returned_method != method:
        raise OAuthError("OAuth client registration response is invalid")
    if (method == "none" and secret) or (
        method != "none" and (not isinstance(secret, str) or not secret)
    ):
        raise OAuthError("OAuth client registration credentials are invalid")
    registration, _ = McpOAuthClientRegistration.objects.get_or_create(
        server_url=server_url,
        callback_url=callback,
        issuer=issuer,
        defaults={
            "client_id": client_id,
            "encrypted_client_secret": encrypt_value(secret) if secret else "",
            "authorization_endpoint": metadata["authorization_endpoint"],
            "token_endpoint": metadata["token_endpoint"],
            "registration_endpoint": endpoint,
            "token_endpoint_auth_method": method,
            "scopes_supported": metadata.get("scopes_supported", []),
        },
    )
    return registration


def callback_url():
    value = getattr(settings, "MCP_OAUTH_CALLBACK_URL", "")
    parsed = urlparse(value)
    loopback = parsed.hostname in {"localhost", "127.0.0.1"}
    if (
        not parsed.hostname
        or not parsed.netloc
        or (
            parsed.scheme != "https"
            and not (settings.DEBUG and loopback and parsed.scheme == "http")
        )
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/api/v1/mcp-oauth/callback/"
    ):
        raise OAuthError("MCP_OAUTH_CALLBACK_URL must be a fixed valid callback")
    return value


def frontend_return_url():
    value = getattr(settings, "MCP_OAUTH_FRONTEND_RETURN_URL", "")
    parsed = urlparse(value)
    loopback = parsed.hostname in {"localhost", "127.0.0.1"}
    if (
        not parsed.hostname
        or not parsed.netloc
        or (
            parsed.scheme != "https"
            and not (settings.DEBUG and loopback and parsed.scheme == "http")
        )
        or parsed.username
        or parsed.password
        or parsed.query != "settings=plugins"
        or parsed.fragment
        or parsed.path != "/"
    ):
        raise OAuthError(
            "MCP_OAUTH_FRONTEND_RETURN_URL must be a fixed safe "
            "'/?settings=plugins' URL"
        )
    return value


def _validate_browser_flow_origins() -> str:
    """Require the frontend and callback to share a cookie-compatible host."""
    callback_url_value = callback_url()
    callback = urlparse(callback_url_value)
    frontend = urlparse(frontend_return_url())
    if (
        callback.scheme != frontend.scheme
        or not callback.hostname
        or callback.hostname.lower().rstrip(".")
        != (frontend.hostname or "").lower().rstrip(".")
    ):
        raise OAuthError(
            "MCP OAuth frontend and callback must use the same hostname and "
            "scheme so the browser-binding cookie is sent"
        )
    return callback_url_value


def state_cookie_name(state):
    return "mcp_oauth_" + hashlib.sha256(state.encode()).hexdigest()[:24]


def start_flow(*, user, organization, server, service, organization_credential):
    if (
        service.credential_type != CredentialType.MCP_OAUTH
        or server.transport == "stdio"
        or server.auth_type != "oauth"
    ):
        raise OAuthError("OAuth is not configured for this MCP server")
    if service.organization_id not in {None, organization.id}:
        raise OAuthError("OAuth service is not available to this organization")
    if (
        service.oauth_plugin_slug != server.plugin.slug
        or service.oauth_requirement_key != server.oauth_requirement_key
    ):
        raise OAuthError("OAuth service is not bound to this MCP plugin")
    callback = _validate_browser_flow_origins()
    protected, metadata, resource = _discover(server.url)
    registration = _registration(server.url, callback, protected, metadata)
    # Validate even persisted registrations before constructing the browser
    # redirect; cached metadata must not bypass HTTPS/public-host validation.
    _validated_https_url(registration.authorization_endpoint)
    state, binding, verifier = (
        secrets.token_urlsafe(32),
        secrets.token_urlsafe(32),
        secrets.token_urlsafe(64),
    )
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    scope = protected.get("_challenge_scope", "")
    if not scope:
        advertised = protected.get("scopes_supported") or []
        if advertised:
            if not isinstance(advertised, list) or any(
                not isinstance(item, str) for item in advertised
            ):
                raise OAuthError("MCP OAuth scopes_supported metadata is invalid")
            scope = _validated_scope(" ".join(advertised))
    McpOAuthAuthorizationState.objects.create(
        state_hash=hashlib.sha256(state.encode()).hexdigest(),
        browser_binding_hash=hashlib.sha256(binding.encode()).hexdigest(),
        encrypted_code_verifier=encrypt_value(verifier),
        user=user,
        organization=organization,
        service=service,
        server_id=server.id,
        requirement_key=server.oauth_requirement_key,
        server_url=server.url,
        resource=resource,
        issuer=metadata["issuer"],
        registration=registration,
        organization_credential=organization_credential,
        redirect_uri=callback,
        scope=scope,
        expires_at=timezone.now() + STATE_TTL,
    )
    params = {
        "response_type": "code",
        "client_id": registration.client_id,
        "redirect_uri": callback,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": resource,
    }
    if scope:
        params["scope"] = scope
    auth_url = (
        registration.authorization_endpoint
        + ("&" if "?" in registration.authorization_endpoint else "?")
        + urlencode(params)
    )
    return auth_url, binding, state_cookie_name(state)


def _consume_callback_state(raw_state, binding):
    if not raw_state or not binding:
        raise OAuthError("OAuth state is invalid or expired")
    now = timezone.now()
    with transaction.atomic():
        row = (
            McpOAuthAuthorizationState.objects.select_for_update()
            .select_related("user", "organization", "service", "registration")
            .filter(
                state_hash=hashlib.sha256(raw_state.encode()).hexdigest(),
                consumed_at__isnull=True,
                expires_at__gt=now,
            )
            .first()
        )
        if row is None:
            raise OAuthError("OAuth state is invalid or expired")
        row.consumed_at = now
        row.save(update_fields=["consumed_at"])
        matches = secrets.compare_digest(
            row.browser_binding_hash, hashlib.sha256(binding.encode()).hexdigest()
        )
    if not matches:
        raise OAuthError("OAuth state is invalid or expired")
    return row


def _token_auth(registration, secret):
    if registration.token_endpoint_auth_method == "none":
        return None, ""
    if registration.token_endpoint_auth_method == "client_secret_basic" and secret:
        return (registration.client_id, secret), ""
    if registration.token_endpoint_auth_method == "client_secret_post" and secret:
        return None, secret
    raise OAuthError("OAuth client credentials are unavailable")


def _validate_current_server(row):
    from apps.plugins.models import PluginMcpServer

    return PluginMcpServer.objects.filter(
        id=row.server_id,
        url=row.server_url,
        auth_type="oauth",
        oauth_requirement_key=row.requirement_key,
        plugin__slug=row.service.oauth_plugin_slug,
        plugin__credential_requirements__key=row.requirement_key,
        plugin__credential_requirements__credential_service_id=row.service_id,
        plugin__credential_requirements__required=True,
    ).exists()


def _store_token(row, token):
    if not _validate_current_server(row):
        raise OAuthError("MCP server changed during OAuth authorization")
    if "scope" in token:
        if not isinstance(token["scope"], str):
            raise OAuthError("OAuth token scope is invalid")
        token = {**token, "scope": _validated_scope(token["scope"])}
    if (
        not isinstance(token.get("access_token"), str)
        or not token["access_token"]
        or str(token.get("token_type", "Bearer")).lower() != "bearer"
    ):
        raise OAuthError("OAuth token response is invalid")
    if "refresh_token" in token and not isinstance(token["refresh_token"], str):
        raise OAuthError("OAuth token response is invalid")
    try:
        lifetime = (
            int(token["expires_in"]) if token.get("expires_in") is not None else None
        )
    except (TypeError, ValueError):
        raise OAuthError("OAuth token lifetime is invalid") from None
    data = {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token", ""),
        "token_type": "Bearer",
        "expires_at": (timezone.now() + timedelta(seconds=max(1, lifetime))).isoformat()
        if lifetime is not None
        else "",
        "scope": token.get("scope", row.scope),
        "resource": row.resource,
        "server_url": row.server_url,
        "server_id": str(row.server_id),
        "registration_id": str(row.registration_id),
        "identity": {
            key: token[key]
            for key in (
                "id_token",
                "account_id",
                "user_id",
                "workspace_id",
                "workspace_name",
                "email_domain",
            )
            if key in token
        },
    }
    with transaction.atomic():
        owner = (
            {"organization_id": row.organization_id}
            if row.organization_credential
            else {"user_id": row.user_id}
        )
        credential, _ = Credential.objects.get_or_create(
            service=row.service,
            oauth_server_id=row.server_id,
            **owner,
            defaults={
                "user": None if row.organization_credential else row.user,
                "organization": row.organization
                if row.organization_credential
                else None,
                "name": f"{row.service.name} OAuth",
                "created_by": row.user,
                "oauth_server_url": row.server_url,
                "oauth_resource": row.resource,
                "oauth_registration": row.registration,
                "oauth_status": "connected",
                "encrypted_value": encrypt_value(json.dumps(data)),
            },
        )
        # The owner/server uniqueness constraint plus Django's race-safe
        # get_or_create protects parallel independent OAuth flows. Lock the
        # canonical row while replacing rotating tokens.
        credential = Credential.objects.select_for_update().get(pk=credential.pk)
        credential.oauth_server_url = row.server_url
        credential.oauth_resource = row.resource
        credential.oauth_registration = row.registration
        credential.oauth_status = "connected"
        credential.encrypted_value = encrypt_value(json.dumps(data))
        credential.save(
            update_fields=[
                "oauth_server_url",
                "oauth_resource",
                "oauth_registration",
                "oauth_status",
                "encrypted_value",
                "updated_at",
            ]
        )


def complete_flow(*, raw_state, binding, code="", error=""):
    row = _consume_callback_state(raw_state, binding)
    if error:
        raise OAuthError("Authorization was denied")
    if not code or len(code) > 8192 or not _validate_current_server(row):
        raise OAuthError("OAuth callback is invalid or MCP server changed")
    if (
        row.registration.issuer != row.issuer
        or row.registration.server_url != row.server_url
    ):
        raise OAuthError("OAuth issuer or MCP server changed during authorization")
    registration = row.registration
    secret = (
        decrypt_value(registration.encrypted_client_secret)
        if registration.encrypted_client_secret
        else ""
    )
    auth, post_secret = _token_auth(registration, secret)
    token = _post_token(
        registration.token_endpoint,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": row.redirect_uri,
            "client_id": registration.client_id,
            "code_verifier": decrypt_value(row.encrypted_code_verifier),
            "resource": row.resource,
        },
        auth=auth,
        post_client_secret=post_secret,
    )
    if token.get("error"):
        raise OAuthError("OAuth token exchange failed")
    _store_token(row, token)
    return True


def _mark_reconnect(credential_id):
    Credential.objects.filter(
        pk=credential_id, service__credential_type=CredentialType.MCP_OAUTH
    ).update(oauth_status="reconnect_required", encrypted_value="")


def access_token_for_request(credential_id, server_id, server_url):
    with transaction.atomic():
        credential = (
            Credential.objects.select_for_update()
            .select_related("service", "oauth_registration")
            .filter(
                pk=credential_id,
                service__credential_type=CredentialType.MCP_OAUTH,
                oauth_status="connected",
                oauth_server_id=server_id,
                oauth_server_url=server_url,
            )
            .first()
        )
        if credential is None:
            return None
        try:
            data = json.loads(decrypt_value(credential.encrypted_value))
        except Exception:
            credential.oauth_status = "reconnect_required"
            credential.encrypted_value = ""
            credential.save(
                update_fields=["oauth_status", "encrypted_value", "updated_at"]
            )
            return None
        if (
            data.get("server_id") != str(server_id)
            or data.get("server_url") != server_url
            or data.get("resource") != credential.oauth_resource
            or data.get("registration_id") != str(credential.oauth_registration_id)
        ):
            credential.oauth_status = "reconnect_required"
            credential.encrypted_value = ""
            credential.save(
                update_fields=["oauth_status", "encrypted_value", "updated_at"]
            )
            return None
        if not _runtime_server_is_current(credential, server_id, server_url):
            credential.oauth_status = "reconnect_required"
            credential.encrypted_value = ""
            credential.save(
                update_fields=["oauth_status", "encrypted_value", "updated_at"]
            )
            return None
        expiry_text = data.get("expires_at") or ""
        try:
            expiry = (
                timezone.datetime.fromisoformat(expiry_text) if expiry_text else None
            )
        except ValueError:
            credential.oauth_status = "reconnect_required"
            credential.encrypted_value = ""
            credential.save(
                update_fields=["oauth_status", "encrypted_value", "updated_at"]
            )
            return None
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.get_current_timezone())
        if expiry is not None and expiry <= timezone.now() + timedelta(seconds=60):
            refresh = data.get("refresh_token")
            if not refresh:
                credential.oauth_status = "reconnect_required"
                credential.encrypted_value = ""
                credential.save(
                    update_fields=["oauth_status", "encrypted_value", "updated_at"]
                )
                return None
            registration = credential.oauth_registration
            secret = (
                decrypt_value(registration.encrypted_client_secret)
                if registration.encrypted_client_secret
                else ""
            )
            auth, post_secret = _token_auth(registration, secret)
            try:
                token = _post_token(
                    registration.token_endpoint,
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": refresh,
                        "client_id": registration.client_id,
                        "resource": credential.oauth_resource,
                    },
                    auth=auth,
                    post_client_secret=post_secret,
                )
            except OAuthError:
                return None
            if token.get("error") == "invalid_grant":
                credential.oauth_status = "reconnect_required"
                credential.encrypted_value = ""
                credential.save(
                    update_fields=["oauth_status", "encrypted_value", "updated_at"]
                )
                return None
            if token.get("error"):
                return None
            access = token.get("access_token")
            rotated_refresh = token.get("refresh_token", refresh)
            if (
                not isinstance(access, str)
                or not access
                or str(token.get("token_type", "Bearer")).lower() != "bearer"
                or not isinstance(rotated_refresh, str)
            ):
                return None
            try:
                lifetime = (
                    int(token["expires_in"])
                    if token.get("expires_in") is not None
                    else None
                )
            except (TypeError, ValueError):
                return None
            if "scope" in token:
                if not isinstance(token["scope"], str):
                    return None
                try:
                    token_scope = _validated_scope(token["scope"])
                except OAuthError:
                    return None
            else:
                token_scope = data.get("scope", "")
            identity = dict(data.get("identity") or {})
            identity.update(
                {
                    k: token[k]
                    for k in (
                        "id_token",
                        "account_id",
                        "user_id",
                        "workspace_id",
                        "workspace_name",
                        "email_domain",
                    )
                    if k in token
                }
            )
            data.update(
                {
                    "access_token": access,
                    "refresh_token": rotated_refresh,
                    "token_type": "Bearer",
                    "expires_at": (
                        timezone.now() + timedelta(seconds=max(1, lifetime))
                    ).isoformat()
                    if lifetime is not None
                    else "",
                    "scope": token_scope,
                    "identity": identity,
                }
            )
            credential.encrypted_value = encrypt_value(json.dumps(data))
            credential.save(update_fields=["encrypted_value", "updated_at"])
        access = data.get("access_token")
        return access if isinstance(access, str) and access else None


def _runtime_server_is_current(credential, server_id, server_url):
    from apps.plugins.models import PluginMcpServer

    return PluginMcpServer.objects.filter(
        id=server_id,
        url=server_url,
        auth_type="oauth",
        oauth_requirement_key=credential.service.oauth_requirement_key,
        plugin__slug=credential.service.oauth_plugin_slug,
        plugin__credential_requirements__key=credential.service.oauth_requirement_key,
        plugin__credential_requirements__credential_service_id=credential.service_id,
        plugin__credential_requirements__required=True,
    ).exists()


def disconnect_credential(credential_id, *, server_id, server_url):
    return bool(
        Credential.objects.filter(
            pk=credential_id,
            service__credential_type=CredentialType.MCP_OAUTH,
            oauth_server_id=server_id,
            oauth_server_url=server_url,
        ).update(oauth_status="disconnected", encrypted_value="")
    )


class McpOAuthHTTPAuth(httpx.Auth):
    requires_response_body = True

    def __init__(self, credential_id, server_id, server_url):
        self.credential_id, self.server_id, self.server_url = (
            credential_id,
            server_id,
            server_url,
        )
        parsed = urlparse(server_url)
        self.origin = (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower(),
            parsed.port or 443,
        )

    async def async_auth_flow(self, request):
        url = request.url
        if (
            url.scheme.lower(),
            (url.host or "").lower(),
            url.port or 443,
        ) != self.origin:
            raise McpOAuthUnauthorizedError(
                "MCP OAuth request escaped its pinned origin"
            )
        try:
            token = await sync_to_async(
                access_token_for_request, thread_sensitive=True
            )(self.credential_id, self.server_id, self.server_url)
        except Exception:
            raise McpOAuthUnauthorizedError(
                "MCP OAuth credential lookup failed"
            ) from None
        if not token:
            raise McpOAuthUnauthorizedError("MCP OAuth credential requires reconnect")
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            try:
                await sync_to_async(_mark_reconnect, thread_sensitive=True)(
                    self.credential_id
                )
            finally:
                await response.aclose()
            raise McpOAuthUnauthorizedError(
                "MCP OAuth authorization was rejected; reconnect required"
            )
