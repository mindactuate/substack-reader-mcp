"""The setup page: connects the server to Substack without editing .env or deploying again.

Protected by MCP_SECRET, the same password as the OAuth login; after logging in, a signed cookie keeps the
session for 30 minutes. Two ways to connect:

1. Log in by email (preferred): Substack sends a login email. The user copies the link from it without
   opening it, pastes it here, and the server follows it and keeps the session cookie it receives. That
   session belongs to the server alone, so logging out in the browser no longer breaks anything. Only
   works if Substack does not ask for a captcha.
2. Paste the substack.sid cookie (fallback).

The Substack-specific work is done by the callbacks from substack_mcp.py; this module only handles the
page and its protection. The callbacks report problems as ValueError with a message for the user.
"""

import html
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from oauth_server import PasswordGate, Signer, html_page, read_form

PATH = "/setup"
COOKIE = "setup_session"
SESSION_TTL = 1800
EMAIL_INTERVAL = 60  # at most one login email per minute

Profile = dict  # {"name", "handle"} of the connected Substack account


def _https(request: Request) -> bool:
    # Cloud Run terminates TLS and forwards plain HTTP, but says so in X-Forwarded-Proto
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def _who(profile: Profile) -> str:
    name, handle = profile.get("name"), profile.get("handle")
    return f"{name} (@{handle})" if name and handle else name or (f"@{handle}" if handle else "your account")


class SetupPage:
    def __init__(self, secret: str, gate: PasswordGate, *,
                 status: Callable[[], Awaitable[dict]],
                 connect_cookie: Callable[[str], Awaitable[Profile]],
                 send_login_email: Callable[[str], Awaitable[None]],
                 redeem_login_link: Callable[[str], Awaitable[Profile]]):
        self.signer = Signer(secret, b"substack-mcp setup v1")
        self.gate = gate
        self.status = status
        self.connect_cookie = connect_cookie
        self.send_login_email = send_login_email
        self.redeem_login_link = redeem_login_link
        self.last_email = 0.0

    def _authed(self, request: Request) -> bool:
        return self.signer.unsign("setup", request.cookies.get(COOKIE, "")) is not None

    async def handle(self, request: Request) -> Response:
        if request.method != "POST":
            return await self._main() if self._authed(request) else _login()

        # The session cookie is SameSite=Strict; checking Origin on top costs nothing
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return html_page('<p class="error">Request from another site rejected.</p>', 403)
        form = await read_form(request)
        if form is None:
            return html_page('<p class="error">Request too large.</p>', 413)
        action = form.get("action", "")

        if action == "login":
            if self.gate.locked():
                return _login("Too many failed attempts. Try again in an hour.", 429)
            if not await self.gate.check(form.get("password", ""), "setup page"):
                return _login("Wrong password.", 401)
            response = RedirectResponse(PATH, status_code=303)
            response.set_cookie(COOKIE, self.signer.sign("setup", {}, SESSION_TTL), max_age=SESSION_TTL,
                                path=PATH, httponly=True, samesite="strict", secure=_https(request))
            return response
        if not self._authed(request):
            return _login("Your session has expired. Please log in again.")
        if action == "logout":
            response = RedirectResponse(PATH, status_code=303)
            response.delete_cookie(COOKIE, path=PATH)
            return response

        message = error = None
        try:
            if action == "send_link":
                email = form.get("email", "").strip()
                if time.time() - self.last_email < EMAIL_INTERVAL:
                    raise ValueError("Please wait a minute before requesting another email.")
                self.last_email = time.time()
                await self.send_login_email(email)
                message = (f"Substack has sent a login email to {email}. Copy the link from it (don't open it) "
                           "and paste it below.")
            elif action == "use_link":
                message = f"Connected to Substack as {_who(await self.redeem_login_link(form.get('link', '')))}."
            elif action == "set_cookie":
                message = f"Connected to Substack as {_who(await self.connect_cookie(form.get('cookie', '')))}."
            else:
                raise ValueError("Unknown action.")
        except ValueError as e:
            error = str(e)
        return await self._main(message, error, cookie_open=action == "set_cookie")

    async def _main(self, message: str | None = None, error: str | None = None,
                    cookie_open: bool = False) -> Response:
        e = html.escape
        s = await self.status()
        if s.get("profile"):
            state = f'<p class="ok">Connected to Substack as <strong>{e(_who(s["profile"]))}</strong>.</p>'
        else:
            state = f'<p class="error">Not connected to Substack{": " + e(s["error"]) if s.get("error") else "."}</p>'
        if s.get("updated"):
            state += f'<p class="muted">Session saved {e(s["updated"])} ({e(s.get("source") or "?")}).</p>'
        notice = (f'<p class="ok">{e(message)}</p>' if message else "") + (f'<p class="error">{e(error)}</p>' if error else "")
        return html_page(f"""{state}{notice}
<h2>Log in with Substack</h2>
<p class="muted">Substack sends a login email to your account. The server needs the link in it unused, so
don't open it: copy it instead.</p>
<form method="post">
<label for="email">Email address of your Substack account</label>
<input id="email" name="email" type="email" autocomplete="email" required>
<div class="buttons"><button type="submit" name="action" value="send_link">Send login email</button></div>
</form>
<form method="post">
<label for="link">Login link from the email</label>
<ol><li>In the email, long-press (phone) or right-click (computer) the sign-in button or link.</li>
<li>Choose "Copy link" and paste it here.</li></ol>
<input id="link" name="link" type="url" required>
<div class="buttons"><button type="submit" name="action" value="use_link">Connect</button></div>
</form>
<details{" open" if cookie_open else ""}><summary>Paste the cookie instead</summary>
<ol><li>Open a private browser window and log in to substack.com.</li>
<li>Open the developer tools (F12): <strong>Application</strong> (Chrome, Edge) or <strong>Storage</strong>
(Firefox) → Cookies → https://substack.com.</li>
<li>Copy the value of <code>substack.sid</code> and paste it here.</li>
<li>Close the private window without logging out: logging out ends the session.</li></ol>
<form method="post">
<label for="cookie">Value of substack.sid</label>
<input id="cookie" name="cookie" type="password" autocomplete="off" required>
<div class="buttons"><button type="submit" name="action" value="set_cookie">Save cookie</button></div>
</form></details>
<form method="post"><div class="buttons">
<button type="submit" name="action" value="logout" class="secondary">Log out of this page</button></div></form>""")


def _login(error: str | None = None, status: int = 200) -> Response:
    e = html.escape
    return html_page(f"""<p>Connect the server to your Substack account.</p>
<form method="post">
<input type="hidden" name="username" value="substack-rss-reader" autocomplete="username">
<label for="password">Password (the MCP_SECRET token)</label>
<input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
{f'<p class="error">{e(error)}</p>' if error else ''}
<div class="buttons"><button type="submit" name="action" value="login">Log in</button></div>
</form>""", status)
