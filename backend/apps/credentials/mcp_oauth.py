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
import uuid
from datetime import timedelta
from urllib.parse import quote_plus, urlencode, urlparse

import httpcore
import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from common.utils import decrypt_value, encrypt_value

from .models import Credential
from .repositories import McpOAuthRepository

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
    if not isinstance(url, str) or not url or any(ord(char) < 0x21 for char in url):
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
    from apps.credentials.services import CredentialServiceSvc

    try:
        normalized_server_url = CredentialServiceSvc._validate_oauth_server_url(
            server_url
        )
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
    existing = McpOAuthRepository.registration(server_url, callback, issuer)
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
    return McpOAuthRepository.save_registration(
        server_url=server_url,
        callback_url=callback,
        issuer=issuer,
        client_id=client_id,
        encrypted_client_secret=encrypt_value(secret) if secret else "",
        authorization_endpoint=metadata["authorization_endpoint"],
        token_endpoint=metadata["token_endpoint"],
        registration_endpoint=endpoint,
        token_endpoint_auth_method=method,
        scopes_supported=metadata.get("scopes_supported", []),
    )


def validate_browser_request_origin(*, request_scheme: str, request_host: str) -> None:
    """Require the browser API request to share configured callback origin."""
    callback = urlparse(callback_url())
    host = urlparse(f"//{request_host}")
    if (
        request_scheme != callback.scheme
        or (host.hostname or "").lower() != (callback.hostname or "").lower()
    ):
        raise OAuthError(
            "OAuth browser API requests must use the configured callback "
            "hostname and scheme"
        )


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
        or parsed.query != "settings=credentials"
        or parsed.fragment
        or parsed.path != "/"
    ):
        raise OAuthError(
            "MCP_OAUTH_FRONTEND_RETURN_URL must be a fixed safe "
            "'/?settings=credentials' URL"
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


def state_cookie_name(state: str) -> str:
    return "mcp_oauth_" + hashlib.sha256(state.encode()).hexdigest()[:24]


def start_flow(
    *,
    user,
    organization,
    service,
    organization_credential: bool,
    credential=None,
    credential_name: str = "",
) -> tuple[str, str, str]:
    """Discover provider metadata and persist a pending service-bound state."""
    from apps.credentials.services import CredentialServiceSvc

    if service.credential_type != "mcp_oauth":
        raise OAuthError("Credential service does not support OAuth")
    try:
        endpoint = CredentialServiceSvc._validate_oauth_server_url(
            service.oauth_server_url
        )
    except ValueError:
        raise OAuthError("OAuth service endpoint is invalid") from None
    if endpoint != service.oauth_server_url:
        raise OAuthError("OAuth service endpoint is not normalized")
    callback = _validate_browser_flow_origins()
    protected, metadata, resource = _discover(endpoint)
    registration = _registration(endpoint, callback, protected, metadata)
    if (
        not isinstance(registration.authorization_endpoint, str)
        or not isinstance(registration.token_endpoint, str)
        or not _safe_registration_url(registration.authorization_endpoint)
        or not _safe_registration_url(registration.token_endpoint)
    ):
        raise OAuthError("OAuth registration endpoint must use valid HTTPS")
    if not _registration_metadata_safe(
        registration,
        expected_url=endpoint,
        expected_issuer=metadata["issuer"],
        expected_callback=callback,
    ):
        raise OAuthError("OAuth registration metadata is invalid")
    try:
        _validated_https_url(registration.authorization_endpoint)
        _validated_https_url(registration.token_endpoint)
        if registration.registration_endpoint:
            _validated_https_url(registration.registration_endpoint)
    except OAuthError:
        raise OAuthError("OAuth registration endpoint must use valid HTTPS") from None
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
    McpOAuthRepository.create_state(
        state_hash=hashlib.sha256(state.encode()).hexdigest(),
        browser_binding_hash=hashlib.sha256(binding.encode()).hexdigest(),
        encrypted_code_verifier=encrypt_value(verifier),
        user=user,
        organization=organization,
        service=service,
        credential=credential,
        credential_name=(credential_name or "").strip()[:255],
        credential_server_url_snapshot=(
            credential.oauth_server_url if credential else ""
        ),
        reconnect_existing=credential is not None,
        registration_fingerprint=_registration_fingerprint(registration),
        server_url=endpoint,
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
    authorization_url = (
        registration.authorization_endpoint
        + ("&" if "?" in registration.authorization_endpoint else "?")
        + urlencode(params)
    )
    return authorization_url, binding, state_cookie_name(state)


def _consume_callback_state(raw_state: str, binding: str):
    """Consume state once and accept only the supplied matching cookie hash."""
    if (
        not isinstance(raw_state, str)
        or not raw_state
        or not isinstance(binding, str)
        or not binding
    ):
        raise OAuthError("OAuth state is invalid or expired")
    row, matches = McpOAuthRepository.consume_state_and_match_binding(
        state_hash=hashlib.sha256(raw_state.encode()).hexdigest(),
        binding_hash=hashlib.sha256(binding.encode()).hexdigest(),
        now=timezone.now(),
    )
    if row is None or not matches:
        raise OAuthError("OAuth state is invalid or expired")
    return row


def _token_auth(registration, secret: str) -> tuple[tuple[str, str] | None, str]:
    if registration.token_endpoint_auth_method == "none":
        return None, ""
    if registration.token_endpoint_auth_method == "client_secret_basic" and secret:
        return (registration.client_id, secret), ""
    if registration.token_endpoint_auth_method == "client_secret_post" and secret:
        return None, secret
    raise OAuthError("OAuth client credentials are unavailable")


def _registration_metadata_safe(
    registration,
    *,
    expected_url: str,
    expected_issuer: str | None = None,
    expected_callback: str | None = None,
) -> bool:
    """Validate persisted provider endpoints and token authentication metadata."""
    try:
        method = registration.token_endpoint_auth_method
        secret = registration.encrypted_client_secret
        return bool(
            registration.server_url == expected_url
            and (
                expected_callback is None
                or registration.callback_url == expected_callback
            )
            and isinstance(registration.issuer, str)
            and (expected_issuer is None or registration.issuer == expected_issuer)
            and _safe_registration_url(registration.issuer)
            and _safe_registration_url(registration.authorization_endpoint)
            and _safe_registration_url(registration.token_endpoint)
            and (
                not registration.registration_endpoint
                or _safe_registration_url(registration.registration_endpoint)
            )
            and isinstance(registration.client_id, str)
            and registration.client_id
            and method in {"none", "client_secret_basic", "client_secret_post"}
            and isinstance(secret, str)
            and (
                (method == "none" and not secret) or (method != "none" and bool(secret))
            )
            and isinstance(registration.scopes_supported, list)
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _safe_registration_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        port = parsed.port
        return bool(
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.netloc
            and not parsed.username
            and not parsed.password
            and "@" not in parsed.netloc
            and not parsed.query
            and not parsed.fragment
            and (port is None or 1 <= port <= 65535)
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _registration_fingerprint(registration) -> str:
    """Hash security-sensitive registration metadata for callback integrity."""
    values = [
        registration.server_url,
        registration.issuer,
        registration.client_id,
        registration.authorization_endpoint,
        registration.token_endpoint,
        registration.registration_endpoint,
        registration.token_endpoint_auth_method,
        registration.encrypted_client_secret,
    ]
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


_TOKEN_IDENTITY_FIELDS = (
    "id_token",
    "account_id",
    "user_id",
    "workspace_id",
    "workspace_name",
    "email_domain",
)


def _validated_token_scope(token: dict, fallback: str = "") -> str:
    """Return a bounded scope from token response or the prior grant."""
    if "scope" not in token:
        return fallback
    if not isinstance(token["scope"], str):
        raise OAuthError("OAuth token scope is invalid")
    return _validated_scope(token["scope"])


def _validated_token_fields(
    token: dict,
    *,
    default_refresh: str = "",
    default_scope: str = "",
) -> tuple[str, str, int | None, str]:
    """Validate common bearer-token fields used by callback and refresh."""
    if not isinstance(token, dict) or token.get("error"):
        raise OAuthError("OAuth token response is invalid")
    access = token.get("access_token")
    refresh = token.get("refresh_token", default_refresh)
    if (
        not isinstance(access, str)
        or not access
        or not isinstance(refresh, str)
        or str(token.get("token_type", "Bearer")).lower() != "bearer"
    ):
        raise OAuthError("OAuth token response is invalid")
    try:
        lifetime = (
            int(token["expires_in"]) if token.get("expires_in") is not None else None
        )
    except (TypeError, ValueError):
        raise OAuthError("OAuth token lifetime is invalid") from None
    return access, refresh, lifetime, _validated_token_scope(token, default_scope)


def _token_expiry(lifetime: int | None) -> str:
    """Return a normalized expiry timestamp or an empty unknown expiry."""
    return (
        (timezone.now() + timedelta(seconds=max(1, lifetime))).isoformat()
        if lifetime is not None
        else ""
    )


def _store_authorization_grant(state, token: dict) -> uuid.UUID:
    """Validate a callback token and atomically store its service-bound grant."""
    access, refresh, lifetime, scope = _validated_token_fields(
        token, default_scope=state.scope
    )
    payload = {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_at": _token_expiry(lifetime),
        "scope": scope or state.scope,
        "service_id": str(state.service_id),
        "server_url": state.server_url,
        "resource": state.resource,
        "registration_id": str(state.registration_id),
        "identity": {
            key: token[key]
            for key in _TOKEN_IDENTITY_FIELDS
            if key in token and isinstance(token[key], str)
        },
    }
    try:
        return McpOAuthRepository.persist_authorization_grant(
            state=state, encrypted_value=encrypt_value(json.dumps(payload))
        )
    except ValueError as exc:
        from common.exceptions import AuthenticationError

        raise AuthenticationError(str(exc)) from None


def _mark_reconnect(credential_id: uuid.UUID) -> None:
    McpOAuthRepository.update_oauth(
        credential_id, oauth_status="reconnect_required", encrypted_value=""
    )


def oauth_credential_status(credential: Credential) -> dict:
    """Return validated nonsecret OAuth metadata for API and readiness consumers."""
    from .repositories import McpOAuthRepository

    current = getattr(credential, "oauth_status", "") or "disconnected"
    result = {
        "connected": False,
        "status": current,
        "reconnect_required": current == "reconnect_required",
        "expires_at": None,
    }
    if (
        current != "connected"
        or not isinstance(credential.encrypted_value, str)
        or not credential.encrypted_value
    ):
        return result
    grant = McpOAuthRepository.grant_metadata(credential)
    if grant is None:
        result.update(status="reconnect_required", reconnect_required=True)
        return result
    expiry = grant.pop("_expires_at", None)
    result.update(
        connected=True, status="connected", reconnect_required=False, expires_at=expiry
    )
    return result


def access_token_for_request(
    credential_id: uuid.UUID, service_id: uuid.UUID, server_url: str
) -> str | None:
    """Resolve a validated bearer token and refresh atomically when needed."""
    from .repositories import McpOAuthRepository

    with transaction.atomic():
        credential = McpOAuthRepository.get_credential(credential_id, lock=True)
        if credential is None or (
            credential.service_id != service_id
            or credential.oauth_status != "connected"
            or credential.oauth_server_url != server_url
            or credential.service.oauth_server_url != server_url
        ):
            return None
        grant = McpOAuthRepository.grant_metadata(credential, expected_url=server_url)
        if grant is None:
            McpOAuthRepository.update_locked_grant(
                credential, oauth_status="reconnect_required", encrypted_value=""
            )
            return None
        expiry = grant.pop("_expires_at", None)
        access = grant["access_token"]
        if expiry is None or expiry > timezone.now() + timedelta(seconds=60):
            return access
        refresh = grant["refresh_token"]
        if not refresh:
            McpOAuthRepository.update_locked_grant(
                credential, oauth_status="reconnect_required", encrypted_value=""
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
        if isinstance(token, dict) and token.get("error") == "invalid_grant":
            McpOAuthRepository.update_locked_grant(
                credential, oauth_status="reconnect_required", encrypted_value=""
            )
            return None
        try:
            new_access, new_refresh, lifetime, scope = _validated_token_fields(
                token,
                default_refresh=refresh,
                default_scope=str(grant.get("scope", "")),
            )
        except OAuthError:
            return None
        identity = dict(grant.get("identity", {}))
        identity.pop("_expires_at", None)
        identity.update(
            {
                key: token[key]
                for key in _TOKEN_IDENTITY_FIELDS
                if isinstance(token, dict)
                and key in token
                and isinstance(token[key], str)
            }
        )
        updated = {key: value for key, value in grant.items() if key != "_expires_at"}
        updated.update(
            {
                "access_token": new_access,
                "refresh_token": new_refresh,
                "token_type": "Bearer",
                "expires_at": _token_expiry(lifetime),
                "scope": scope,
                "identity": identity,
            }
        )
        McpOAuthRepository.update_locked_grant(
            credential, encrypted_value=encrypt_value(json.dumps(updated))
        )
        return new_access


def disconnect_credential(credential_id: uuid.UUID) -> bool:
    return bool(
        McpOAuthRepository.update_oauth(
            credential_id, oauth_status="disconnected", encrypted_value=""
        )
    )


class McpOAuthHTTPAuth(httpx.Auth):
    requires_response_body = True

    def __init__(self, credential_id, service_id, server_url):
        self.credential_id, self.service_id, self.server_url = (
            credential_id,
            service_id,
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
            )(self.credential_id, self.service_id, self.server_url)
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
