"""OAuth for the HTTP mode, for clients that cannot send a custom header (ChatGPT).

The server is its own authorization server, built from the MCP SDK's handlers: discovery metadata,
dynamic client registration, PKCE and the token endpoint come from the SDK, this module supplies the
provider behind them and a small login page. Logging in means entering MCP_SECRET on that page.

- Access and refresh tokens are signed with a key derived from MCP_SECRET. They need no storage, survive
  restarts and cold starts, and rotating MCP_SECRET revokes all of them at once.
- MCP_SECRET itself stays valid as a static bearer token, so existing "Authorization: Bearer <token>"
  connectors keep working.
- Anyone can register a client, so registrations are only kept in memory. A client is written to disk
  only after a successful login, which means strangers cannot fill the file.
- Authorization codes live in memory for 5 minutes. The server runs as a single instance, and the code is
  exchanged seconds after the login.
"""

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

log = logging.getLogger("substack_mcp.oauth")

SCOPE = "substack"
LOGIN_PATH = "/oauth/login"
ACCESS_TTL = 3600  # clients refresh on their own
REFRESH_TTL = 90 * 86400  # renewed with every refresh, so it only runs out after 90 days without use
CODE_TTL = 300
LOGIN_TTL = 600  # time to enter the password
MAX_CLIENTS = 20  # stored clients; beyond that, the oldest has to log in again
MAX_PENDING = 50  # registrations without a login yet
MAX_FAILURES = 20  # failed logins per hour before the login page locks
MAX_FORM_BYTES = 8192
STATIC_CLIENT = "static-token"


def _b64(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _unb64(data: bytes) -> bytes:
    return base64.urlsafe_b64decode(data + b"=" * (-len(data) % 4))


def strip_bearer(value: str) -> str:
    """The token from "Bearer <token>", "<token>" or a doubled "Bearer Bearer <token>"; "" otherwise."""
    *scheme, token = value.split() or [""]
    return token if all(p.lower() == "bearer" for p in scheme) else ""


class Provider:
    """OAuthAuthorizationServerProvider for a single user whose password is MCP_SECRET."""

    def __init__(self, secret: str, public_url: str, clients_file: Path):
        self.secret = secret.encode()
        self.public_url = public_url.rstrip("/")
        self.key = hmac.new(self.secret, b"substack-mcp oauth v1", hashlib.sha256).digest()
        self.clients_file = clients_file
        self.clients = self._load_clients()
        self.pending: OrderedDict[str, OAuthClientInformationFull] = OrderedDict()
        self.codes: dict[str, AuthorizationCode] = {}
        self.failures: list[float] = []

    def auth_settings(self) -> AuthSettings:
        return AuthSettings(
            issuer_url=self.public_url,
            resource_server_url=f"{self.public_url}/mcp",
            # There is only one resource, and the tokens are signed by this server
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
        )

    # --- Signed values: tokens and the login link -------------------------------------------------

    def _sign(self, kind: str, payload: dict, ttl: int) -> str:
        body = _b64(json.dumps({**payload, "k": kind, "e": int(time.time()) + ttl},
                               separators=(",", ":")).encode())
        return (body + b"." + _b64(hmac.new(self.key, body, hashlib.sha256).digest())).decode()

    def _unsign(self, kind: str, value: str) -> dict | None:
        """The payload if the signature is valid, `kind` matches and it has not expired."""
        try:
            body, sig = value.encode().split(b".")
            if not hmac.compare_digest(_unb64(sig), hmac.new(self.key, body, hashlib.sha256).digest()):
                return None
            data = json.loads(_unb64(body))
        except (ValueError, UnicodeError):
            return None
        return data if data.get("k") == kind and data.get("e", 0) > time.time() else None

    # --- Clients ----------------------------------------------------------------------------------

    def _load_clients(self) -> dict[str, OAuthClientInformationFull]:
        try:
            raw = json.loads(self.clients_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("%s unreadable (%s), starting without stored clients", self.clients_file, e)
            return {}
        clients = {}
        for item in raw if isinstance(raw, list) else []:
            try:
                client = OAuthClientInformationFull.model_validate(item)
            except ValueError:
                continue
            clients[client.client_id] = client
        return clients

    def _save_clients(self):
        data = [c.model_dump(mode="json", exclude_none=True) for c in self.clients.values()]
        tmp = self.clients_file.with_name(self.clients_file.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.clients_file)  # atomic

    def _approve(self, client: OAuthClientInformationFull):
        """Stores a client after a successful login."""
        self.pending.pop(client.client_id, None)
        if client.client_id in self.clients:
            return
        self.clients[client.client_id] = client
        while len(self.clients) > MAX_CLIENTS:
            oldest = min(self.clients.values(), key=lambda c: c.client_id_issued_at or 0)
            del self.clients[oldest.client_id]
        self._save_clients()
        log.info("OAuth client stored: %s", client.client_name or client.client_id)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id) or self.pending.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.pending[client_info.client_id] = client_info
        while len(self.pending) > MAX_PENDING:
            self.pending.popitem(last=False)

    # --- Authorization: hand over to the login page -----------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        # The request travels signed in the link, so the login page needs no state of its own
        link = self._sign("login", {"c": client.client_id, "p": params.model_dump(mode="json")}, LOGIN_TTL)
        return f"{self.public_url}{LOGIN_PATH}?request={link}"

    async def login(self, request: Request) -> Response:
        data = self._unsign("login", request.query_params.get("request", ""))
        client = data and await self.get_client(data["c"])
        if not client:
            return _page(error="This login link has expired. Start connecting again in the app.", status=400)
        params = AuthorizationParams.model_validate(data["p"])
        app_name = client.client_name or "An app"
        target = urlparse(str(params.redirect_uri)).hostname or str(params.redirect_uri)
        if request.method != "POST":
            return _page(app_name, target)

        form = await _read_form(request)
        if form is None:
            return _page(error="Request too large.", status=413)
        if form.get("action") == "deny":
            return RedirectResponse(construct_redirect_uri(
                str(params.redirect_uri), error="access_denied", state=params.state), status_code=303)

        now = time.time()
        self.failures = [t for t in self.failures if now - t < 3600]
        if len(self.failures) >= MAX_FAILURES:
            log.warning("OAuth login locked: %d failed attempts in the last hour", len(self.failures))
            return _page(error="Too many failed attempts. Try again in an hour.", status=429)
        if not hmac.compare_digest(strip_bearer(form.get("password", "")).encode(), self.secret):
            self.failures.append(now)
            log.warning("OAuth login with wrong password for %s", app_name)
            await asyncio.sleep(1)
            return _page(app_name, target, error="Wrong password.", status=401)

        self._approve(client)
        self.codes = {k: v for k, v in self.codes.items() if v.expires_at > now}
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=now + CODE_TTL, client_id=client.client_id,
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource)
        log.info("OAuth login for %s", app_name)
        return RedirectResponse(construct_redirect_uri(
            str(params.redirect_uri), code=code, state=params.state), status_code=303)

    # --- Tokens -----------------------------------------------------------------------------------

    def _tokens(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        claims = {"c": client_id, "s": scopes, "r": resource}
        return OAuthToken(
            access_token=self._sign("at", claims, ACCESS_TTL),
            expires_in=ACCESS_TTL,
            scope=" ".join(scopes) or None,
            refresh_token=self._sign("rt", claims, REFRESH_TTL),
        )

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        code = self.codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        if self.codes.pop(authorization_code.code, None) is None:  # single use
            raise TokenError("invalid_grant", "authorization code already used")
        return self._tokens(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        data = self._unsign("rt", refresh_token)
        if not data or data["c"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=data["c"], scopes=data["s"],
                            expires_at=data["e"], resource=data.get("r"))

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        return self._tokens(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        token = strip_bearer(token)  # the SDK only removes the first "Bearer "
        if hmac.compare_digest(token.encode(), self.secret):  # constant time, no timing leak
            return AccessToken(token=token, client_id=STATIC_CLIENT, scopes=[])
        data = self._unsign("at", token)
        if not data:
            log.info("Token rejected: invalid or expired (length %d)", len(token))
            return None
        if data["c"] not in self.clients:
            log.info("Token rejected: client no longer stored")
            return None
        return AccessToken(token=token, client_id=data["c"], scopes=data["s"], expires_at=data["e"],
                           resource=data.get("r"))

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Not offered (revocation is disabled): signed tokens can only be revoked all at once, by rotating MCP_SECRET."""


async def _read_form(request: Request) -> dict[str, str] | None:
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            return None
    return {k: v[0] for k, v in parse_qs(body.decode(errors="replace")).items()}


def _page(app_name: str | None = None, target: str | None = None, error: str | None = None,
          status: int = 200) -> HTMLResponse:
    """The login page; without `app_name` only the error message."""
    e = html.escape
    if app_name:
        content = f"""<p><strong>{e(app_name)}</strong> wants to read your Substack subscriptions and RSS feeds
and change your feed list. After the login, you are sent back to <strong>{e(target or "")}</strong>.</p>
<form method="post">
<input type="hidden" name="username" value="substack-mcp" autocomplete="username">
<label for="password">Password (the MCP_SECRET token)</label>
<input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
{f'<p class="error">{e(error)}</p>' if error else ''}
<div class="buttons"><button type="submit" name="action" value="allow">Allow</button>
<button type="submit" name="action" value="deny" class="secondary" formnovalidate>Deny</button></div>
</form>"""
    else:
        content = f'<p class="error">{e(error or "")}</p>'
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Substack Reader login</title>
<style>
:root {{ --bg: #f6f5f2; --card: #fff; --text: #1f1e1c; --muted: #6b6862; --accent: #ff6719; --border: #dedbd4; --error: #b3261e; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #171615; --card: #22211f; --text: #ecebe8; --muted: #a29f98; --border: #3a3835; --error: #f2b8b5; }} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 16px; background: var(--bg);
  color: var(--text); font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ width: 100%; max-width: 420px; background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 24px; }}
h1 {{ font-size: 1.25rem; margin: 0 0 12px; }}
label {{ display: block; font-size: .9rem; color: var(--muted); margin: 16px 0 6px; }}
input[type=password] {{ width: 100%; padding: 10px 12px; font: inherit; color: inherit; background: var(--bg);
  border: 1px solid var(--border); border-radius: 8px; }}
.buttons {{ display: flex; gap: 8px; margin-top: 16px; }}
button {{ flex: 1; padding: 10px; font: inherit; font-weight: 600; border-radius: 8px; border: 1px solid var(--accent);
  background: var(--accent); color: #fff; cursor: pointer; }}
button.secondary {{ background: transparent; color: var(--text); border-color: var(--border); }}
.error {{ color: var(--error); }}
</style></head>
<body><main><h1>Substack Reader</h1>{content}</main></body></html>"""
    return HTMLResponse(page, status_code=status, headers={
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    })
