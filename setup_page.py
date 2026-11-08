"""The setup page: connects the server to Substack without editing .env or deploying again.

Protected by MCP_SECRET, the same password as the OAuth login; after logging in, a signed cookie keeps the
session for 30 minutes. The user pastes the substack.sid cookie from a private browser window; the page
explains step by step how to find it. (Logging in by email from the server doesn't work: Substack accepts
the request but silently sends no email without a captcha.)

Checking and storing the cookie is done by a callback from substack_mcp.py; this module only handles the
page and its protection. The callback reports problems as ValueError with a message for the user.
"""

import html
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from oauth_server import PasswordGate, Signer, html_page, read_form

PATH = "/setup"
COOKIE = "setup_session"
SESSION_TTL = 1800

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
                 connect_cookie: Callable[[str], Awaitable[Profile]]):
        self.signer = Signer(secret, b"substack-mcp setup v1")
        self.gate = gate
        self.status = status
        self.connect_cookie = connect_cookie

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
        if action != "set_cookie":
            return await self._main(error="Unknown action.")
        try:
            profile = await self.connect_cookie(form.get("cookie", ""))
        except ValueError as e:
            return await self._main(error=str(e))
        return await self._main(message=f"Connected to Substack as {_who(profile)}. You can close this page.")

    async def _main(self, message: str | None = None, error: str | None = None) -> Response:
        e = html.escape
        s = await self.status()
        if s.get("profile"):
            state = f'<p class="ok">Connected to Substack as <strong>{e(_who(s["profile"]))}</strong>.</p>'
            intro = "<h2>Connect another account or renew the login</h2>"
        else:
            state = f'<p class="error">Not connected to Substack{": " + e(s["error"]) if s.get("error") else "."}</p>'
            intro = "<h2>Connect Substack</h2>"
        if s.get("updated"):
            state += f'<p class="muted">Session saved {e(s["updated"])} ({e(s.get("source") or "?")}).</p>'
        notice = (f'<p class="ok">{e(message)}</p>' if message else "") + (f'<p class="error">{e(error)}</p>' if error else "")
        return html_page(f"""{state}{notice}
{intro}
<p>The server reads Substack with your login cookie <code>substack.sid</code>. You copy it once from your
browser; after that, the server keeps the login alive on its own. This needs a computer: phones have no
developer tools. Everything else, including ChatGPT and Claude on your phone, works without it.</p>

<ol class="steps">
<li><strong>Open a private window</strong>: Ctrl+Shift+N (Mac: Cmd+Shift+N) in Chrome and Edge,
Ctrl+Shift+P (Mac: Cmd+Shift+P) in Firefox. A separate session means that logging out in your normal browser
doesn't affect the server.</li>
<li>Go to <strong>substack.com</strong> and <strong>sign in</strong>, as usual (email link, password or Google).</li>
<li><strong>Open the developer tools</strong>: press F12 (Mac: Cmd+Option+I).</li>
<li>Open the tab <strong>Application</strong> (Chrome, Edge; it may hide behind <strong>»</strong>) or
<strong>Storage</strong> (Firefox). On the left, under <strong>Cookies</strong>, click
<strong>https://substack.com</strong>.</li>
<li>Find the row <strong>substack.sid</strong>, <strong>double-click its value</strong>, select it all
(Ctrl+A, Mac: Cmd+A) and copy it (Ctrl+C, Mac: Cmd+C). It is long and starts with <code>s%3A</code> or
<code>s:</code>.</li>
<li><strong>Paste it below</strong> and save.</li>
<li><strong>Close the private window without signing out.</strong> Closing is fine; signing out would end
exactly this session.</li>
</ol>
<p class="muted">Safari: first turn on Settings → Advanced → "Show features for web developers", then
Develop → Show Web Inspector → Storage → Cookies.</p>
<p class="muted">Treat the value like a password: it gives full access to your Substack account. Paste it only
here. The server only reads with it and never sends it anywhere but Substack.</p>

<form method="post">
<label for="cookie">Value of substack.sid</label>
<input id="cookie" name="cookie" type="password" autocomplete="off" required>
<div class="buttons"><button type="submit" name="action" value="set_cookie">Save and check</button></div>
</form>
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
