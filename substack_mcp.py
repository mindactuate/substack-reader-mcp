"""
Substack Reader MCP Server
--------------------------
Gives Claude read access to your Substack subscriptions.

Setup:
    pip install -r requirements.txt
    export SUBSTACK_SID="<value of the substack.sid cookie>"  # for the subscription list & paid posts
    export SUBSTACK_USER="<user_id>-<name>"                    # only without a cookie: public subscription list, e.g. "12345-jane-doe"
    python substack_mcp.py                                     # stdio (Claude Desktop / Claude Code)
    MCP_TRANSPORT=streamable-http MCP_SECRET=<random> python substack_mcp.py   # remote (claude.ai custom connector)

The substack.sid cookie is a rolling session: every request extends it by 90 days. So it only expires if the
server is not used for 90 days or if you log out of the browser session it came from. If it is invalid, the
tools say so explicitly.

Instead of export, the variables can also be placed in a .env next to the script (KEY=VALUE per line).

Fallback without a cookie: put a subs.txt next to the script, one publication URL per line,
e.g. https://www.astralcodexten.com

Note: the endpoints used are unofficial and may change.
"""

import asyncio
import datetime as dt
import hmac
import json
import logging
import os
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import httpx
from markdownify import markdownify as md
from mcp.server.mcpserver import MCPServer
from pydantic import Field


def _load_dotenv(path: Path):
    """Reads KEY=VALUE lines from .env; environment variables that are already set take precedence."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(Path(__file__).with_name(".env"))

# Tolerates typical copy-paste mistakes: whitespace, quotes, "substack.sid=" prefix
SID = (os.environ.get("SUBSTACK_SID") or "").strip().strip("'\"").removeprefix("substack.sid=").strip() or None
# Profile identifier from the profile URL, format "<user_id>-<name>", e.g. "12345-jane-doe"
USER = (os.environ.get("SUBSTACK_USER") or "").strip().strip("'\"").strip("/") or None
SUBS_FILE = Path(__file__).with_name("subs.txt")
CACHE_FILE = Path(os.environ.get("SUBSTACK_CACHE_FILE") or Path(__file__).with_name("cache.json"))
# Request statistics as JSONL, one line per event; SUBSTACK_STATS_FILE=off disables them
_stats_env = os.environ.get("SUBSTACK_STATS_FILE")
STATS_FILE = None if _stats_env == "off" else Path(_stats_env or Path(__file__).with_name("stats.jsonl"))
CONCURRENCY = 4
CACHE_TTL = 15 * 60  # seconds, for the subscription list and archives (change constantly)
POST_CACHE_TTL = 7 * 24 * 3600  # seconds, for full texts (published posts rarely change)
RECS_CACHE_TTL = 24 * 3600  # seconds, for recommendations (change rarely)
SUBS_KEY = "subs:v2"  # "v2": entries now include the publication ID
MIN_INTERVAL = 0.5   # seconds between two request starts (global, max. 2 requests/s)
MAX_RETRIES = 4
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_BACKOFF = 60     # seconds
ARCHIVE_FIELDS = ("title", "subtitle", "description", "truncated_body_text", "post_date",
                  "canonical_url", "audience", "type", "wordcount", "podcast_duration", "postTags",
                  "section_name", "reaction_count", "comment_count", "restacks")
MAX_BATCH = 15       # max. URLs per get_posts call
MAX_SUGGESTIONS = 30  # max. suggestions per discover_publications call

PROMPTS_DIR = Path(os.environ.get("SUBSTACK_PROMPTS_DIR") or Path(__file__).with_name("prompts"))


def _prompt_text(name: str, optional: bool = False) -> str:
    """Reads a text block from prompts/<name>.md so it can be adjusted without code changes.
    HTML comments are stripped so the files can contain explanations and examples."""
    path = PROMPTS_DIR / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        if optional:
            return ""
        raise RuntimeError(f"Prompt file missing or unreadable: {path}") from e
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()


def _interests_block() -> str:
    text = _prompt_text("interests", optional=True)
    return f"\n\nMy personal focus areas (for selection and ordering):\n{text}" if text else ""


# The instructions deliberately contain only neutral usage hints. Criteria and answer format live
# exclusively in the news_briefing prompt: claude.ai delivers server instructions like a tool result,
# and Claude rightly treats directives in there ("always use this format") as third-party content and
# ignores them. A prompt, on the other hand, is chosen by the user, so its text counts as the user's request.
mcp = MCPServer(
    "substack-reader",
    instructions=(
        "Reads the user's Substack subscriptions. Proven workflow for news questions: get_recent_posts "
        "for the overview, then get_posts for the most promising candidates, and only then select. "
        "Roundup issues (weekly reviews, roundups) often contain several news items and can be read in "
        "full with get_post(max_chars=100000); full texts are cached for 7 days, so fetching again costs "
        "no requests. Every post has a `url` field that can be used to link to it. "
        "A prepared briefing request is available as the prompt 'news_briefing'."
    ),
)
_inflight: dict[str, asyncio.Future] = {}


def _load_cache() -> dict[str, tuple[float, object]]:
    try:
        raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}  # missing or broken: start empty
    now = time.time()
    return {k: (ts, v) for k, (ts, v) in raw.items() if now - ts < _ttl(k)}


def _ttl(key: str) -> float:
    if key.startswith("post:"):
        return POST_CACHE_TTL
    if key.startswith("recs:"):
        return RECS_CACHE_TTL
    return CACHE_TTL


def _save_cache():
    now = time.time()
    live = {k: e for k, e in _cache.items() if now - e[0] < _ttl(k)}
    tmp = CACHE_FILE.with_name(CACHE_FILE.name + ".tmp")
    try:
        tmp.write_text(json.dumps(live, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE_FILE)  # atomic, so an interruption never leaves a half-written file
    except OSError:
        pass  # the cache is only an optimization, don't pass write errors on to the client


_cache: dict[str, tuple[float, object]] = _load_cache()


class _ThrottledTransport(httpx.AsyncHTTPTransport):
    """Throttles all requests process-wide and retries them with backoff on 429/5xx."""

    _lock = asyncio.Lock()
    _next_slot = 0.0

    @classmethod
    async def _wait_for_slot(cls):
        # Reserves the next free time slot under the lock; the waiting happens outside of it
        async with cls._lock:
            now = time.monotonic()
            slot = max(now, cls._next_slot)
            cls._next_slot = slot + MIN_INTERVAL
        await asyncio.sleep(slot - now)

    @classmethod
    def _pause_all(cls, seconds: float):
        # On 429 the wait applies to all following requests, not just this one
        cls._next_slot = max(cls._next_slot, time.monotonic() + seconds)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Set the login cookie here per request instead of on the client: httpx sends client cookies to every
        # host, including foreign URLs in get_post (e.g. via prompt injection from a post) and redirect targets.
        if SID and _cookie_allowed(request.url.host):
            existing = request.headers.get("cookie")
            request.headers["cookie"] = f"{existing}; substack.sid={SID}" if existing else f"substack.sid={SID}"
        for attempt in range(MAX_RETRIES + 1):
            waited = time.monotonic()
            await self._wait_for_slot()
            start = time.monotonic()
            stat = {"kind": "http", "host": request.url.host, "path": request.url.path,
                    "attempt": attempt, "wait_ms": round((start - waited) * 1000)}
            try:
                response = await super().handle_async_request(request)
            except httpx.TransportError as e:
                _log_stat(**stat, error=type(e).__name__, ms=round((time.monotonic() - start) * 1000))
                if attempt == MAX_RETRIES:
                    raise
                await asyncio.sleep(_backoff(attempt))
                continue

            # Duration until the response headers; the client reads the body afterwards
            _log_stat(**stat, status=response.status_code, ms=round((time.monotonic() - start) * 1000))
            if response.status_code not in RETRY_STATUS or attempt == MAX_RETRIES:
                return response
            await response.aclose()
            delay = _retry_after(response) or _backoff(attempt)
            if response.status_code == 429:
                self._pause_all(delay)
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")


def _log_stat(**event):
    if STATS_FILE is None:
        return
    line = json.dumps({"ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"), **event},
                      ensure_ascii=False)
    try:
        with STATS_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # statistics are just a by-product, don't pass write errors on to the client


def _backoff(attempt: int) -> float:
    return min(MAX_BACKOFF, 2 ** attempt + random.uniform(0, 1))


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return min(MAX_BACKOFF, float(response.headers["Retry-After"]))
    except (KeyError, ValueError):
        return None


def _cookie_allowed(host: str) -> bool:
    """Login cookie only for Substack itself and the custom domains of subscribed publications."""
    host = host.lower()
    if host == "substack.com" or host.endswith(".substack.com"):
        return True
    subs = _cache.get(SUBS_KEY)  # domains rarely change, so an expired entry is fine too
    return bool(subs) and host in {urlparse(s["base_url"]).hostname for s in subs[1]}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (substack-reader-mcp)"},
        timeout=20,
        follow_redirects=True,
        transport=_ThrottledTransport(),
    )


def _cached(key: str):
    hit = _cache.get(key)
    return hit[1] if hit and time.time() - hit[0] < _ttl(key) else None


def _store(key: str, value):
    _cache[key] = (time.time(), value)
    return value


async def _once(key: str, fetch):
    """Cache with in-flight deduplication: parallel calls with the same key share one request.
    Errors are not cached."""
    kind = key.split(":", 1)[0]  # subs / archive / post / recs
    if (hit := _cached(key)) is not None:
        _log_stat(kind="cache", type=kind, result="hit", key=key)
        return hit
    task = _inflight.get(key)
    if task is None:
        _log_stat(kind="cache", type=kind, result="miss", key=key)
        task = asyncio.ensure_future(fetch())
        _inflight[key] = task
        task.add_done_callback(lambda _: _inflight.pop(key, None))
    else:
        _log_stat(kind="cache", type=kind, result="shared", key=key)
    # shield: if one caller is cancelled, the request keeps running for the others
    value = _store(key, await asyncio.shield(task))
    # Only write once nothing is running anymore: one write per get_recent_posts instead of one per publication
    if all(t.done() for t in _inflight.values()):
        _save_cache()
    return value


async def _subscriptions(c: httpx.AsyncClient) -> list[dict]:
    return await _once(SUBS_KEY, lambda: _fetch_subscriptions(c))


def _pub_url(p: dict) -> str:
    """Base URL of a publication from the fields of the Substack API."""
    return f"https://{p['custom_domain']}" if p.get("custom_domain") else f"https://{p.get('subdomain')}.substack.com"


def _error_text(e: Exception) -> str:
    """Short error description, understandable for Claude, for entries with an `error` field."""
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    return str(e) if isinstance(e, ValueError) else type(e).__name__


async def _fetch_subscriptions(c: httpx.AsyncClient) -> list[dict]:
    subs: list[dict] = []
    if SID or USER:
        # /api/v1/subscriptions now only returns an empty list; the profile contains the subscriptions including
        # the publication. With a cookie, use the logged-in profile: it responds with 401 when the session has
        # expired. The public profile returns the subscriptions even without a valid login, so an expired cookie
        # would go unnoticed there and paid posts would silently come back truncated.
        url = ("https://substack.com/api/v1/user/profile/self" if SID
               else f"https://substack.com/api/v1/user/{USER}/public_profile/self")
        r = await c.get(url)
        if SID and r.status_code in (401, 403):
            raise RuntimeError(
                "Substack login expired or invalid. Set a new value of the substack.sid cookie as SUBSTACK_SID "
                "and restart the server."
            )
        if r.is_error:
            raise RuntimeError(f"Fetching the Substack profile failed ({r.status_code}). Response: {r.text[:300]}")
        for s in r.json().get("subscriptions", []):
            p = s.get("publication")
            if not p:
                continue
            subs.append({"name": p.get("name"), "base_url": _pub_url(p), "id": p.get("id"),
                         "membership": s.get("membership_state")})

    if not subs and SUBS_FILE.exists():
        for line in SUBS_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                subs.append({"name": urlparse(line).netloc, "base_url": line.rstrip("/")})

    return subs


async def _archive(c, sem, base_url: str, limit: int) -> list[dict]:
    """The latest `limit` posts of a publication, reduced to ARCHIVE_FIELDS and cached.
    Errors (HTTP, unexpected format) are raised, not swallowed."""
    async def fetch():
        async with sem:
            # Since Nov 2026 Substack paginates with a cursor: `offset` leads to HTTP 400,
            # and the response is {"posts": [...], "nextCursor": ...} instead of a bare list
            r = await c.get(f"{base_url}/api/v1/archive", params={"sort": "new", "limit": limit})
            r.raise_for_status()
            data = r.json()
        items = data.get("posts") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ValueError(f"Unexpected archive format: {str(data)[:200]}")
        # Keep only the fields needed, which keeps the cache file small
        return [{k: p.get(k) for k in ARCHIVE_FIELDS}
                | {"postTags": [t.get("name") for t in p.get("postTags") or []]}
                for p in items]

    # Cache the raw data and filter by date afterwards: other `days` values use the same cache.
    # "v2" in the key so that old cache entries without the newer fields are not used
    return await _once(f"archive:v2:{base_url}:{limit}", fetch)


async def _recent_for_pub(c, sem, pub: dict, since: dt.datetime, limit: int) -> list[dict]:
    archive = await _archive(c, sem, pub["base_url"], limit)
    posts = []
    for p in archive:
        date_str = p.get("post_date")
        if not date_str:
            continue
        date = dt.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if date < since:
            continue
        post = {
            "publication": pub["name"],
            "title": p.get("title"),
            "subtitle": p.get("subtitle"),
            "excerpt": _excerpt(p.get("description"), p.get("truncated_body_text"), p.get("subtitle")),
            "date": date.date().isoformat(),
            "url": p.get("canonical_url"),
            "type": p.get("type"),
            "words": p.get("wordcount"),
            "audio_min": round(p["podcast_duration"] / 60) if p.get("podcast_duration") else None,
            "tags": p.get("postTags"),
            "section": p.get("section_name"),
            "reactions": p.get("reaction_count"),
            "comments": p.get("comment_count"),
            "restacks": p.get("restacks"),
            "paywalled": p.get("audience") not in (None, "everyone"),
        }
        # Omitting empty fields saves tokens
        posts.append({k: v for k, v in post.items() if v not in (None, "", [])})
    return posts


def _excerpt(description: str | None, body_start: str | None, subtitle: str | None) -> str:
    """The author's teaser plus the actual start of the text; duplicate text only once."""
    d, b = (description or "").strip(), (body_start or "").strip()
    if d == (subtitle or "").strip():
        d = ""  # the teaser is often identical to the subtitle, which is already returned separately
    if not d or d in b:
        return b
    if not b or b in d:
        return d
    return f"{d} | {b}"


@mcp.tool()
async def list_subscriptions() -> list[dict]:
    """Lists all Substack publications the user is subscribed to."""
    async with _client() as c:
        return await _subscriptions(c)


@mcp.tool()
async def get_recent_posts(days: int = 14, keywords: list[str] | None = None,
                           max_per_publication: int = 20) -> list[dict]:
    """Fetches posts from all subscriptions of the last `days` calendar days plus today, newest first.

    Per post: title, subtitle, `excerpt` (teaser + start of the text), date, `url` and signals for
    classification: `type` (newsletter/podcast/thread; "podcast" is often also set on articles with
    an audio version), `words`, `audio_min`, `tags`, `section`, `reactions`, `comments`, `restacks`,
    `paywalled`. Empty fields are omitted.

    `keywords` is an optional, rough pre-filter (case-insensitive) on title, subtitle, excerpt and
    tags. For semantic relevance, better fetch without keywords and judge yourself. Fetch the full
    texts of several candidates at once with get_posts.

    If a publication could not be loaded, an entry with `publication` and `error` is appended at the
    end. Its posts are then missing from the result; tell the user.
    """
    # Whole calendar days in local time: days=7 on Nov 5 means from Oct 29 00:00, regardless of the time of day
    today = dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    since = today - dt.timedelta(days=days)
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(pub: dict) -> tuple[list[dict], dict | None]:
        # Report errors visibly instead of silently returning an empty list: otherwise a broken
        # API looks exactly like a week without new posts
        try:
            return await _recent_for_pub(c, sem, pub, since, max_per_publication), None
        except (httpx.HTTPError, ValueError) as e:
            return [], {"publication": pub["name"], "error": _error_text(e)}

    async with _client() as c:
        subs = await _subscriptions(c)
        results = await asyncio.gather(*(one(s) for s in subs))

    posts = [p for chunk, _ in results for p in chunk]
    failed = [err for _, err in results if err]
    if keywords:
        kws = [k.lower() for k in keywords]
        posts = [p for p in posts
                 if any(k in " ".join([p.get("title", ""), p.get("subtitle", ""), p.get("excerpt", ""),
                                       *p.get("tags", [])]).lower() for k in kws)]
    return sorted(posts, key=lambda p: p["date"], reverse=True) + failed


@mcp.tool()
async def get_post(url: str, max_chars: int = 30000) -> dict:
    """Returns the full text of a Substack post as Markdown. Paid posts only with a valid subscription cookie."""
    async def fetch():
        parsed = urlparse(url)
        slug = parsed.path.rstrip("/").split("/p/")[-1]
        base = f"{parsed.scheme}://{parsed.netloc}"
        async with _client() as c:
            r = await c.get(f"{base}/api/v1/posts/{slug}")
            r.raise_for_status()
            p = r.json()
        return {
            "title": p.get("title"),
            "subtitle": p.get("subtitle"),
            "author": ", ".join(b.get("name", "") for b in p.get("publishedBylines", [])),
            "date": p.get("post_date"),
            "url": p.get("canonical_url") or url,
            "body": md(p.get("body_html") or "", heading_style="ATX").strip(),
        }

    # Cache the full text untruncated so a later call with a larger max_chars doesn't get the short text
    post = await _once(f"post:{url}", fetch)
    body = post["body"]
    return {k: v for k, v in post.items() if k != "body"} | {
        "truncated": len(body) > max_chars,
        "content": body[:max_chars] or "(No full text available, possibly paywalled)",
    }


@mcp.tool()
async def get_posts(urls: list[str], max_chars: int = 6000) -> list[dict]:
    """Returns the full texts of several posts (max. 15) as Markdown, each truncated to `max_chars`.

    Meant for the second stage of a selection: read candidates from get_recent_posts specifically
    before deciding. Failed URLs appear with an `error` field instead of aborting the call.
    """
    async def one(url: str) -> dict:
        try:
            return await get_post(url, max_chars)
        except httpx.HTTPError as e:
            return {"url": url, "error": _error_text(e)}

    if len(urls) > MAX_BATCH:
        raise ValueError(f"At most {MAX_BATCH} URLs per call, got: {len(urls)}")
    return await asyncio.gather(*(one(u) for u in urls))


async def _recommendations(c, sem, pub: dict) -> list[dict]:
    """Publications that `pub` recommends on its page, reduced to the fields needed."""
    async def fetch():
        async with sem:
            r = await c.get(f"{pub['base_url']}/api/v1/recommendations/from/{pub['id']}")
            r.raise_for_status()
            data = r.json()
        if not isinstance(data, list):
            raise ValueError(f"Unexpected recommendations format: {str(data)[:200]}")
        # Deliberately don't keep subscribe_auth_token and the like
        return [{"id": rp.get("id"), "name": rp.get("name"), "url": _pub_url(rp),
                 "author": rp.get("author_name"), "about": rp.get("hero_text"),
                 "language": rp.get("language"), "paid_option": rp.get("payments_state") == "enabled",
                 "blurb": rec.get("description")}
                for rec in data if (rp := rec.get("recommendedPublication"))]

    return await _once(f"recs:{pub['id']}", fetch)


@mcp.tool()
async def discover_publications(limit: int = 15, recent_posts: int = 3) -> list[dict]:
    """Suggests new publications: the recommendations of all subscribed publications, merged and
    sorted by how many subscriptions recommend the same publication. Already subscribed ones are omitted.

    Per suggestion: `name`, `url`, `author`, `about` (self-description), `recommended_by` (which
    subscriptions recommend it), `blurbs` (reasons given by the recommenders, if any), `paid_option`
    (offers a paid subscription) and `recent_posts` (latest titles with date and URL) to judge topics
    and publishing cadence. Empty fields are omitted.

    Many suggestions have only one recommendation; then choose by fit with the user's existing
    subscriptions and interests, not by order. Subscribing is only possible on Substack itself:
    give the user the `url` as a link. Failed fetches appear at the end with `error`.
    """
    if not 1 <= limit <= MAX_SUGGESTIONS:
        raise ValueError(f"`limit` must be between 1 and {MAX_SUGGESTIONS}, got: {limit}")
    recent_posts = max(0, min(recent_posts, 10))
    sem = asyncio.Semaphore(CONCURRENCY)

    async def recs_of(pub: dict) -> tuple[list[dict], dict | None]:
        try:
            return await _recommendations(c, sem, pub), None
        except (httpx.HTTPError, ValueError) as e:
            return [], {"publication": pub["name"], "error": _error_text(e)}

    async def add_posts(cand: dict):
        try:
            archive = await _archive(c, sem, cand["url"], recent_posts)
        except (httpx.HTTPError, ValueError) as e:
            cand["recent_posts_error"] = _error_text(e)
            return
        cand["recent_posts"] = [
            {k: v for k, v in {"title": p.get("title"), "subtitle": p.get("subtitle"),
                               "date": (p.get("post_date") or "")[:10], "url": p.get("canonical_url"),
                               "paywalled": p.get("audience") not in (None, "everyone")}.items()
             if v not in (None, "", False)}
            for p in archive[:recent_posts]
        ]

    async with _client() as c:
        subs = await _subscriptions(c)
        with_id = [s for s in subs if s.get("id")]
        if not with_id:
            raise RuntimeError("Recommendations need the subscription list from the Substack profile "
                               "(SUBSTACK_SID or SUBSTACK_USER); subs.txt is not enough for this.")
        own_ids = {s["id"] for s in with_id}
        own_urls = {s["base_url"] for s in subs}
        results = await asyncio.gather(*(recs_of(s) for s in with_id))

        candidates: dict[int, dict] = {}
        for pub, (recs, _) in zip(with_id, results):
            for r in recs:
                if r["id"] in own_ids or r["url"] in own_urls:
                    continue
                cand = candidates.setdefault(r["id"], {k: v for k, v in r.items() if k not in ("id", "blurb")}
                                             | {"recommended_by": [], "blurbs": []})
                if pub["name"] not in cand["recommended_by"]:
                    cand["recommended_by"].append(pub["name"])
                    if r["blurb"]:
                        cand["blurbs"].append(f"{pub['name']}: {r['blurb']}")

        top = sorted(candidates.values(),
                     key=lambda x: (-len(x["recommended_by"]), -len(x["blurbs"]), (x["name"] or "").lower()))[:limit]
        if recent_posts:
            await asyncio.gather(*(add_posts(x) for x in top))

    failed = [err for _, err in results if err]
    return [{k: v for k, v in x.items() if v not in (None, "", [], False)} for x in top] + failed


def _int_arg(value: str | int | None, default: int, low: int, high: int) -> int:
    """Lenient number parsing for prompt arguments: empty → default, "14 days" → 14, clamped to [low, high]."""
    match = re.search(r"\d+", str(value or ""))
    return max(low, min(int(match.group()), high)) if match else default


# MCP prompt arguments are always strings, and clients like the Claude mobile app send "" for fields left
# empty. With `int` parameters, validation then fails and the app only shows an unknown network error.
# Hence plain strings that are parsed leniently, plus descriptions for the app's input form.
@mcp.prompt()
def news_briefing(
    days: Annotated[str, Field(description="Period in calendar days, default 7")] = "",
    focus: Annotated[str, Field(description="Optional topical focus, e.g. 'AI regulation'")] = "",
    max_items: Annotated[str, Field(description="Max. items in the main part, default 10")] = "",
    language: Annotated[str, Field(description="Language of the briefing, e.g. 'German'; "
                                               "empty = your language")] = "",
) -> str:
    """News briefing from my Substack subscriptions: only real new events, no essays or old topics."""
    days, max_items = _int_arg(days, 7, 1, 90), _int_arg(max_items, 10, 1, 30)
    focus, language = (focus or "").strip(), (language or "").strip()
    focus_line = f"Topical focus: {focus}. Other topics only if they are truly significant.\n" if focus else ""
    # The prompt itself is in English; without a hint, Claude would conclude that the answer should be
    # in English too. So set the language explicitly or explicitly leave it open.
    language_line = (f"Write the briefing in {language}." if language else
                     "Write the briefing in my language (see the language rule below), not automatically "
                     "in English just because this request is phrased in English.")
    return f"""\
Create a briefing of the most important real news from my Substack subscriptions of the last {days} days.
{language_line}
{focus_line}
{_prompt_text("news_criteria")}{_interests_block()}

Procedure:
1. Call get_recent_posts(days={days}). Entries with `error` are publications that could not be
   loaded: mention them under the notes.
2. Pre-select based on title, subtitle, excerpt and tags. Helpful signals:
   - An excerpt that opens with a concrete event, date or number suggests news; one that opens
     with a thesis, question or anecdote suggests an essay or analysis.
   - Tags and sections (`tags`, `section`) often show whether a newsletter itself classifies a
     post as news, briefing or update, or as essay, opinion or interview.
   - type "podcast" with a large audio_min is usually an interview; "podcast" with short audio
     is often just an article with a read-aloud version.
   - Roundup issues (weekly review, briefing, roundup, links of the week, numbered issues) often
     contain several news items at once: always treat them as candidates.
3. Read the up to 15 most promising candidates with get_posts. Only decide after that.
   Full texts are cached for 7 days, so re-reading costs nothing: always fetch again instead of
   relying on something read earlier.
   Split roundup issues into individual items and read them in full for that: get_post with
   max_chars=100000 instead of relying on the introduction and table of contents. If a text is
   still truncated or only available as a paywall preview, mention that under the notes.
4. Merge the same event from several publications into one item and name all sources.
   Coverage by several publications suggests relevance.
5. What counts is the date of the event, not that of the post. Leave out events before the
   period; include them only if the post itself adds substantial new information, and then
   give both dates.

At most {max_items} items in the main part, the rest under "In brief".

{_prompt_text("briefing_format")}"""


def print_stats(hours: float | None = None):
    """Summarizes stats.jsonl, optionally only for the last `hours` hours."""
    if STATS_FILE is None or not STATS_FILE.exists():
        print("No statistics available.")
        return
    since = dt.datetime.now().astimezone() - dt.timedelta(hours=hours) if hours else None
    events = []
    for line in STATS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if since is None or dt.datetime.fromisoformat(e["ts"]) >= since:
            events.append(e)

    http = [e for e in events if e.get("kind") == "http"]
    cache = [e for e in events if e.get("kind") == "cache"]
    print(f"Period: {'last ' + str(hours) + ' h' if hours else 'all'} "
          f"({events[0]['ts'] if events else '-'} to {events[-1]['ts'] if events else '-'})\n")

    print(f"HTTP requests: {len(http)}")
    if http:
        statuses = Counter(str(e.get("status") or e.get("error")) for e in http)
        ms = sorted(e["ms"] for e in http)
        print(f"  Status:        {', '.join(f'{k}: {v}' for k, v in statuses.most_common())}")
        print(f"  Retries:       {sum(1 for e in http if e['attempt'] > 0)}, "
              f"throttled (429): {statuses.get('429', 0)}")
        print(f"  Duration:      median {ms[len(ms) // 2]} ms, p95 {ms[int(len(ms) * 0.95)]} ms, max {ms[-1]} ms")
        print(f"  Throttling:    {sum(e['wait_ms'] for e in http) / 1000:.1f} s waiting in total")
        hosts = Counter(e["host"] for e in http)
        print(f"  Top hosts:     {', '.join(f'{h} ({n})' for h, n in hosts.most_common(5))}")

    print(f"\nCache lookups: {len(cache)}")
    for typ in sorted({e["type"] for e in cache}):
        results = Counter(e["result"] for e in cache if e["type"] == typ)
        total = sum(results.values())
        saved = results["hit"] + results["shared"]
        print(f"  {typ:8} {total:4}  hits {results['hit']}, shared {results['shared']}, "
              f"fetched {results['miss']}  → {saved / total:.0%} without a request")


log = logging.getLogger("substack_mcp")


class _CloudLoggingFormatter(logging.Formatter):
    """One JSON object per log record, including the traceback.

    Cloud Run turns every output line into its own log entry. The SDK's Rich handler draws multi-line
    tracebacks in boxes, which ends up as dozens of fragmented entries. JSON lines are parsed by Cloud
    Logging as one structured entry with the right severity, and Error Reporting picks up the stack trace."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        return json.dumps({"severity": record.levelname, "message": message, "logger": record.name},
                          ensure_ascii=False)


def _use_cloud_logging():
    """Replaces the Rich handler that MCPServer installs with JSON lines on stderr."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_CloudLoggingFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


class _BearerAuth:
    """ASGI middleware: only lets HTTP requests through with "Authorization: Bearer <token>".

    Deliberately without a WWW-Authenticate header: it would tempt clients like claude.ai into an OAuth flow
    that doesn't exist here."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode()

    def _check(self, header: bytes) -> str | None:
        """None if valid; otherwise a reason for the log that reveals nothing about the token.

        Lenient about the spelling: "Bearer <token>", "bearer <token>", just "<token>" and even a
        doubled "Bearer Bearer <token>" (the claude.ai dialog invites this) are accepted."""
        parts = header.split()
        if not parts:
            return "Authorization header missing"
        *scheme, candidate = parts
        if any(p.lower() != b"bearer" for p in scheme):
            return f"unexpected scheme {b' '.join(scheme)[:20]!r}"
        if not hmac.compare_digest(candidate, self.token):  # constant time, no timing leak
            return (f"wrong token (length {len(candidate)}, expected {len(self.token)}, "
                    f"{len(scheme)}x 'Bearer' in front)")
        return None

    async def __call__(self, scope, receive, send):
        # Let /.well-known/ through: there is nothing there (404), which tells claude.ai that there is no OAuth
        if scope["type"] == "http" and not scope["path"].startswith("/.well-known/"):
            reason = self._check(dict(scope["headers"]).get(b"authorization", b""))
            if reason:
                ua = dict(scope["headers"]).get(b"user-agent", b"").decode(errors="replace")[:40]
                log.warning("401 for %s %s (%s): %s", scope["method"], scope["path"], ua, reason)
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


if __name__ == "__main__":
    if sys.argv[1:2] == ["stats"]:
        print_stats(float(sys.argv[2]) if len(sys.argv) > 2 else None)
    else:
        transport = os.environ.get("MCP_TRANSPORT", "stdio")
        if transport == "streamable-http":
            # Remote mode (claude.ai custom connector, also in the mobile app).
            # Every request needs "Authorization: Bearer <MCP_SECRET>"; claude.ai stores the header encrypted.
            import uvicorn
            from mcp.server.transport_security import TransportSecuritySettings
            secret = (os.environ.get("MCP_SECRET") or "").strip()
            if not secret:
                sys.exit("MCP_SECRET missing: without a token, your Substack login would be open to anyone with the URL.")
            hosts = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
            host = os.environ.get("MCP_HOST", "127.0.0.1")
            app = _BearerAuth(mcp.streamable_http_app(
                streamable_http_path="/mcp",
                stateless_http=True,
                transport_security=TransportSecuritySettings(
                    enable_dns_rebinding_protection=bool(hosts),
                    allowed_hosts=hosts,
                ),
                host=host,
            ), secret)
            on_cloud_run = bool(os.environ.get("K_SERVICE"))  # set by Cloud Run
            if on_cloud_run:
                _use_cloud_logging()
            # Own uvicorn instead of mcp.run: access log off, Cloud Run logs requests itself anyway.
            # On Cloud Run, log_config=None makes uvicorn log through the root handler, i.e. as JSON too.
            uvicorn.run(app, host=host, port=int(os.environ.get("PORT") or os.environ.get("MCP_PORT") or 8000),
                        access_log=False, log_level="info",
                        **({"log_config": None} if on_cloud_run else {}))
        else:
            mcp.run(transport=transport)
