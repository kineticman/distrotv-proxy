#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

# -----------------------------
# Upstream endpoints
# -----------------------------
FEED_URL = "https://tv.jsrdn.com/tv_v5/getfeed.php?type=live"
EPG_URL  = "https://tv.jsrdn.com/epg/query.php"

# -----------------------------
# HTTP headers
# -----------------------------
# Android TV UA matches what the DistroTV app sends — required for EPG endpoint access
ANDROID_UA = "Dalvik/2.1.0 (Linux; U; Android 9; AFTT Build/STT9.221129.002) GTV/AFTT DistroTV/2.0.9"
BROWSER_UA  = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"

# Headers required by DistroTV's CloudFront Function edge validator
HLS_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Origin":     "https://distro.tv",
    "Referer":    "https://distro.tv/",
}

# -----------------------------
# Tunables
# -----------------------------
DEFAULT_GROUP             = "DistroTV"
DEFAULT_TTL_SECONDS       = 43200        # feed cache TTL (12h — feed changes infrequently)
HTTP_TIMEOUT_SECONDS      = 15           # per-request timeout (playback)
PROBE_TIMEOUT_SECONDS     = 10           # timeout during startup probe (fail fast, retry on demand)
FEED_RETRY_ATTEMPTS       = 3
FEED_RETRY_SLEEP_SECONDS  = 0.75
EPG_TTL_SECONDS                = 3600  # cache EPG data for 1 hour
VARIANT_TTL_SECONDS            = 300   # mediatailor / empty channels — 5 min
VARIANT_TTL_SERVERSIDE_SECONDS = 0     # serverside channels — never cache (time-anchored URLs)
CHANNEL_COOLDOWN_SECONDS       = 120   # cooldown after MAX_CONSECUTIVE_FAILURES
MAX_CONSECUTIVE_FAILURES  = 3
PROBE_CONCURRENCY         = 6            # parallel workers during startup probe (lower = less CDN pressure)
STATE_FILE                = os.environ.get("STATE_FILE", "channel_state.json")

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("distro")


# -----------------------------
# Models
# -----------------------------
@dataclass(frozen=True)
class Channel:
    tvg_id:       str
    name:         str
    upstream_url: str   # raw URL with macros
    ad_template:  str   # e.g. "mediatailor_23011", "serverside_22915", "empty_23094"
    logo:         str   # img_logo URL (may be empty)
    description:  str   = ""
    genre:        str   = ""
    language:     str   = "en"
    rating:       str   = ""

    @property
    def ad_type(self) -> str:
        """Returns 'mediatailor', 'serverside', or 'empty'."""
        if self.ad_template.startswith("mediatailor"):
            return "mediatailor"
        if self.ad_template.startswith("serverside"):
            return "serverside"
        return "empty"


# -----------------------------
# Per-channel runtime state  (persisted to JSON)
# -----------------------------
@dataclass
class ChannelState:
    included:      bool  = True
    status:        str   = "untested"   # untested | ok | error | cooldown
    last_ok_at:    float = 0.0
    last_fail_at:  float = 0.0
    failures:      int   = 0
    resolved_url:  str   = ""           # last successfully resolved media playlist URL
    resolved_at:   float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ChannelState":
        s = ChannelState()
        for k, v in d.items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s


# -----------------------------
# State store  (load / save JSON)
# -----------------------------
class StateStore:
    def __init__(self, path: str = STATE_FILE):
        self.path = path
        self._states: Dict[str, ChannelState] = {}
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                raw = json.load(f)
            for tvg_id, d in raw.items():
                self._states[tvg_id] = ChannelState.from_dict(d)
            log.info("state loaded: %d channels from %s", len(self._states), self.path)
        except Exception as e:
            log.warning("state load failed (%s): %s", self.path, e)

    async def _save(self) -> None:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(
                    {tvg_id: s.to_dict() for tvg_id, s in self._states.items()},
                    f, indent=2,
                )
            os.replace(tmp, self.path)
        except Exception as e:
            log.warning("state save failed: %s", e)

    def get(self, tvg_id: str) -> ChannelState:
        if tvg_id not in self._states:
            self._states[tvg_id] = ChannelState()
        return self._states[tvg_id]

    async def set_included(self, tvg_id: str, included: bool) -> None:
        async with self._lock:
            self.get(tvg_id).included = included
            await self._save()

    async def record_success(self, tvg_id: str, resolved_url: str) -> None:
        async with self._lock:
            s = self.get(tvg_id)
            s.status       = "ok"
            s.last_ok_at   = time.time()
            s.failures     = 0
            s.resolved_url = resolved_url
            s.resolved_at  = time.time()
            await self._save()

    async def record_failure(self, tvg_id: str) -> None:
        async with self._lock:
            s = self.get(tvg_id)
            s.failures    += 1
            s.last_fail_at = time.time()
            s.status       = "cooldown" if s.failures >= MAX_CONSECUTIVE_FAILURES else "error"
            await self._save()

    def get_cached_url(self, tvg_id: str, ad_type: str = "") -> Optional[str]:
        s = self._states.get(tvg_id)
        if not s or not s.resolved_url:
            return None
        # serverside channels use time-anchored URLs — never serve stale cache
        if ad_type == "serverside":
            return None
        if (time.time() - s.resolved_at) < VARIANT_TTL_SECONDS:
            return s.resolved_url
        return None

    def in_cooldown(self, tvg_id: str) -> bool:
        s = self._states.get(tvg_id)
        if not s or s.failures < MAX_CONSECUTIVE_FAILURES:
            return False
        return (time.time() - s.last_fail_at) < CHANNEL_COOLDOWN_SECONDS

    def last_good_url(self, tvg_id: str) -> Optional[str]:
        s = self._states.get(tvg_id)
        return s.resolved_url if s and s.resolved_url else None

    def all_states(self) -> Dict[str, ChannelState]:
        return dict(self._states)


# -----------------------------
# Feed parsing
# -----------------------------
def _iter_shows(feed: object) -> Iterable[dict]:
    if isinstance(feed, dict):
        shows = feed.get("shows")
        if isinstance(shows, dict):
            yield from (s for s in shows.values() if isinstance(s, dict))
            return
        if isinstance(shows, list):
            yield from (s for s in shows if isinstance(s, dict))
            return
        for key in ("data", "items", "results"):
            v = feed.get(key)
            if isinstance(v, list):
                yield from (s for s in v if isinstance(s, dict))
                return
        if "type" in feed and "title" in feed:
            yield feed
            return
    if isinstance(feed, list):
        yield from (s for s in feed if isinstance(s, dict))


def parse_channels_from_feed(feed: object) -> Dict[str, Channel]:
    channels: Dict[str, Channel] = {}
    for show in _iter_shows(feed):
        if show.get("type") != "live":
            continue
        name = (show.get("title") or "").strip()
        logo = (show.get("img_logo") or "").strip()
        seasons = show.get("seasons") or []
        if not seasons or not isinstance(seasons[0], dict):
            continue
        episodes = seasons[0].get("episodes") or []
        if not episodes or not isinstance(episodes[0], dict):
            continue
        ep           = episodes[0]
        tvg_id       = ep.get("id")
        content      = ep.get("content") or {}
        upstream_url = content.get("url")
        ad_template  = (ep.get("adtemplates") or "").strip()
        if not name or not tvg_id or not upstream_url:
            continue
        description = (show.get("description") or "").strip()
        genre       = (show.get("genre") or "").strip()
        language    = (show.get("language") or "en").strip()
        rating      = (show.get("rating") or "").strip()

        ch = Channel(
            tvg_id=str(tvg_id),
            name=name,
            upstream_url=str(upstream_url),
            ad_template=ad_template,
            logo=logo,
            description=description,
            genre=genre,
            language=language,
            rating=rating,
        )
        channels[ch.tvg_id] = ch
    return channels


# -----------------------------
# Feed cache
# -----------------------------
class FeedCache:
    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.ttl_seconds      = ttl_seconds
        self._lock            = asyncio.Lock()
        self._fetched_at      = 0.0
        self._channels_by_id: Dict[str, Channel] = {}

    async def get_channels(self, client: httpx.AsyncClient) -> Dict[str, Channel]:
        now = time.time()
        if (now - self._fetched_at) < self.ttl_seconds and self._channels_by_id:
            return self._channels_by_id
        async with self._lock:
            now = time.time()
            if (now - self._fetched_at) < self.ttl_seconds and self._channels_by_id:
                return self._channels_by_id
            await self._refresh_locked(client)
            return self._channels_by_id

    async def _refresh_locked(self, client: httpx.AsyncClient) -> None:
        headers_variants = [
            {"User-Agent": ANDROID_UA, "Accept": "application/json,*/*"},
            {"User-Agent": BROWSER_UA, "Referer": "https://distro.tv/", "Accept": "application/json,*/*"},
        ]
        last_err: Optional[Exception] = None
        for attempt in range(1, FEED_RETRY_ATTEMPTS + 1):
            for headers in headers_variants:
                try:
                    r = await client.get(FEED_URL, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
                    ct   = (r.headers.get("content-type") or "").lower()
                    text = r.text or ""
                    try:
                        feed_obj = r.json()
                    except Exception as je:
                        sample = text[:300].replace("\r", " ").replace("\n", " ")
                        raise RuntimeError(
                            f"feed JSON decode failed status={r.status_code} ct={ct} sample={sample!r}"
                        ) from je
                    if r.status_code >= 400:
                        raise RuntimeError(f"feed HTTP {r.status_code}")
                    channels = parse_channels_from_feed(feed_obj)
                    if channels:
                        self._channels_by_id = channels
                        self._fetched_at     = time.time()
                        log.info("feed refreshed: %d channels", len(channels))
                        return
                    sample = text[:300].replace("\r", " ").replace("\n", " ")
                    log.warning(
                        "parsed 0 channels (attempt %d/%d) status=%s ct=%s sample=%r",
                        attempt, FEED_RETRY_ATTEMPTS, r.status_code, ct, sample,
                    )
                except Exception as e:
                    last_err = e
                    log.warning("feed refresh attempt failed: %s", e)
            if attempt < FEED_RETRY_ATTEMPTS:
                await asyncio.sleep(FEED_RETRY_SLEEP_SECONDS)

        if self._channels_by_id:
            log.warning("feed refresh failed; keeping old cache (%d channels)", len(self._channels_by_id))
            self._fetched_at = time.time()
            return
        log.error("feed refresh failed and cache is empty: %s", last_err)
        self._fetched_at = time.time()


# -----------------------------
# URL sanitization
# -----------------------------
MACRO_RE = re.compile(r"__[^_].*?__")

def m3u_escape(s: str) -> str:
    return s.replace("\n", " ").replace("\r", " ").strip()

def sanitize_upstream_url(url: str) -> str:
    parts = urlsplit(url)
    q     = parse_qsl(parts.query, keep_blank_values=True)
    now_ms    = str(int(time.time() * 1000))
    device_id = str(uuid.uuid4())
    replacements = {
        "__CACHE_BUSTER__":           now_ms,
        "__DEVICE_ID__":              device_id,
        "__LIMIT_AD_TRACKING__":      "0",
        "__IS_GDPR__":                "0",
        "__IS_CCPA__":                "0",
        "__GEO_COUNTRY__":            "US",
        "__LATITUDE__":               "",
        "__LONGITUDE__":              "",
        "__GEO_DMA__":                "",
        "__GEO_TYPE__":               "",
        "__PAGEURL_ESC__":            "https%3A%2F%2Fdistro.tv%2F",
        "__STORE_URL__":              "https%3A%2F%2Fdistro.tv%2F",
        "__APP_BUNDLE__":             "distro.tv",
        "__APP_VERSION__":            "0",
        "__APP_CATEGORY__":           "",
        "__WIDTH__":                  "1920",
        "__HEIGHT__":                 "1080",
        "__DEVICE__":                 "Linux",
        "__DEVICE_ID_TYPE__":         "uuid",
        "__DEVICE_CONNECTION_TYPE__": "",
        "__DEVICE_CATEGORY__":        "desktop",
        "__env.i__":                  "web",
        "__env.u__":                  "web",
        "__PALN__":                   "",
        "__GDPR_CONSENT__":           "",
        "__ADVERTISING_ID__":         "",
        "__CLIENT_IP__":              "",
    }
    sanitized: List[Tuple[str, str]] = []
    for k, v in q:
        if v in replacements:
            v = replacements[v]
        elif MACRO_RE.search(v or ""):
            v = ""
        sanitized.append((k, v))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sanitized, doseq=True), ""))


# -----------------------------
# HLS resolution helpers
# -----------------------------
def pick_variant_from_master(master_text: str, master_url: str) -> Optional[str]:
    lines    = [ln.strip() for ln in master_text.splitlines() if ln.strip()]
    best_bw  = -1
    best_uri: Optional[str] = None
    for i, ln in enumerate(lines):
        if not ln.startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = ln.split(":", 1)[1] if ":" in ln else ""
        bw    = -1
        for part in attrs.split(","):
            if part.startswith("BANDWIDTH="):
                try:
                    bw = int(part.split("=", 1)[1])
                except Exception:
                    bw = -1
                break
        j = i + 1
        while j < len(lines) and lines[j].startswith("#"):
            j += 1
        if j >= len(lines):
            continue
        abs_uri = urljoin(master_url, lines[j])
        if bw > best_bw:
            best_bw  = bw
            best_uri = abs_uri
    return best_uri


# CDNs that use short-lived session tokens in variant URLs.
# Resolving master→variant server-side causes "session not found" errors
# because the token expires before the client fetches it.
# For these hosts we skip resolution and let the client handle it.
SESSION_CDN_HOSTS = {
    "d3s7x6kmqcnb6b.cloudfront.net",
}


async def resolve_to_media_playlist_url(
    client: httpx.AsyncClient,
    upstream_url: str,
    verify_segments: bool = False,
) -> str:
    """
    Resolve upstream_url to a playable media playlist URL.
    If verify_segments=True, fetch the media playlist and raise if it has no segments.
    Skips master→variant resolution for session-based CDNs.
    """
    from urllib.parse import urlsplit as _urlsplit
    if _urlsplit(upstream_url).netloc in SESSION_CDN_HOSTS:
        # Return master URL directly — client must select variant itself
        return upstream_url
    r = await client.get(
        upstream_url,
        headers=HLS_HEADERS,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    r.raise_for_status()
    text = r.text or ""

    if "#EXT-X-STREAM-INF" in text:
        variant = pick_variant_from_master(text, upstream_url)
        media_url = variant or upstream_url
    else:
        media_url = upstream_url
        text = ""  # haven't fetched media playlist yet

    if verify_segments:
        if not text:
            r2 = await client.get(
                media_url,
                headers=HLS_HEADERS,
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            r2.raise_for_status()
            text = r2.text or ""
        # A valid media playlist must have at least one segment URI
        has_segments = any(
            ln.strip() and not ln.strip().startswith("#")
            for ln in text.splitlines()
        )
        if not has_segments:
            raise RuntimeError(f"playlist has no segments: {media_url[:80]}")

    return media_url


# -----------------------------
# Startup probe
# -----------------------------
async def _probe_channel(ch: Channel, client: httpx.AsyncClient) -> None:
    upstream = sanitize_upstream_url(ch.upstream_url)
    try:
        media_url = await resolve_to_media_playlist_url(client, upstream, verify_segments=True)
        await state_store.record_success(ch.tvg_id, media_url)
        log.debug("probe ok  %s %s", ch.tvg_id, ch.name)
    except Exception as e:
        await state_store.record_failure(ch.tvg_id)
        log.debug("probe err %s %s: %s", ch.tvg_id, ch.name, e)


async def probe_all_channels(channels: Dict[str, Channel]) -> None:
    log.info("startup probe: testing %d channels (concurrency=%d)", len(channels), PROBE_CONCURRENCY)
    sem = asyncio.Semaphore(PROBE_CONCURRENCY)

    async def _bounded(ch: Channel) -> None:
        async with sem:
            async with httpx.AsyncClient(follow_redirects=True, timeout=PROBE_TIMEOUT_SECONDS) as client:
                await _probe_channel(ch, client)

    t0 = time.time()
    await asyncio.gather(*[_bounded(ch) for ch in channels.values()])
    ok  = sum(1 for s in state_store.all_states().values() if s.status == "ok")
    err = sum(1 for s in state_store.all_states().values() if s.status in ("error", "cooldown"))
    log.info("startup probe done in %.1fs — ok=%d err=%d", time.time() - t0, ok, err)


# -----------------------------
# FastAPI app
# -----------------------------
app         = FastAPI(title="DistroTV Resolver", version="0.7.0")
feed_cache  = FeedCache(ttl_seconds=DEFAULT_TTL_SECONDS)
state_store = StateStore(path=STATE_FILE)
templates   = Jinja2Templates(directory="templates")


def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=True, timeout=HTTP_TIMEOUT_SECONDS)


@app.on_event("startup")
async def _startup() -> None:
    async with _make_client() as client:
        try:
            channels = await feed_cache.get_channels(client)
        except Exception as e:
            log.warning("startup feed warm failed: %s", e)
            return
    asyncio.create_task(probe_all_channels(channels))
    asyncio.create_task(_epg_refresh_loop())
    asyncio.create_task(_feed_refresh_loop())


async def _epg_refresh_loop() -> None:
    """Background task: refresh EPG cache every hour."""
    await asyncio.sleep(EPG_TTL_SECONDS)
    while True:
        try:
            async with _make_client() as client:
                channels = await feed_cache.get_channels(client)
                xml = await _fetch_epg_xml(channels, client)
            async with _epg_lock:
                _epg_cache["xml"] = xml
                _epg_cache["fetched_at"] = time.time()
            log.info("EPG background refresh complete")
        except Exception as e:
            log.warning("EPG background refresh failed: %s", e)
        await asyncio.sleep(EPG_TTL_SECONDS)


async def _feed_refresh_loop() -> None:
    """Background task: refresh channel feed daily and re-probe."""
    FEED_REFRESH_INTERVAL = 86400  # 24 hours
    await asyncio.sleep(FEED_REFRESH_INTERVAL)
    while True:
        try:
            feed_cache._fetched_at = 0.0  # force cache invalidation
            async with _make_client() as client:
                channels = await feed_cache.get_channels(client)
            log.info("daily feed refresh complete: %d channels", len(channels))
            await probe_all_channels(channels)
        except Exception as e:
            log.warning("daily feed refresh failed: %s", e)
        await asyncio.sleep(FEED_REFRESH_INTERVAL)


# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------
@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------
# M3U playlist  (only included channels)
# ------------------------------------------------------------------
@app.get("/playlist.m3u")
async def playlist(request: Request) -> Response:
    async with _make_client() as client:
        channels = await feed_cache.get_channels(client)

    base = str(request.base_url).rstrip("/")
    out  = ["#EXTM3U"]
    for ch in channels.values():
        s = state_store.get(ch.tvg_id)
        if not s.included:
            continue
        logo_attr = f' tvg-logo="{ch.logo}"' if ch.logo else ""
        out.append(
            f'#EXTINF:-1 tvg-id="{m3u_escape(ch.tvg_id)}"{logo_attr}'
            f' group-title="{m3u_escape(DEFAULT_GROUP)}",{m3u_escape(ch.name)}'
        )
        out.append(f"{base}/play/{m3u_escape(ch.tvg_id)}.m3u8")

    return PlainTextResponse("\n".join(out) + "\n", media_type="audio/x-mpegurl")


# ------------------------------------------------------------------
# Per-channel play endpoint
# ------------------------------------------------------------------
@app.get("/play/{tvg_id}.m3u8")
async def play_m3u8(tvg_id: str) -> RedirectResponse:
    async with _make_client() as client:
        channels = await feed_cache.get_channels(client)
        ch = channels.get(tvg_id)
        if not ch:
            raise HTTPException(status_code=404, detail="unknown channel")

        # 1. Fresh cached variant URL — zero upstream calls
        cached = state_store.get_cached_url(tvg_id, ch.ad_type)
        if cached:
            log.debug("variant cache hit %s", tvg_id)
            return RedirectResponse(url=cached, status_code=302)

        # 2. In cooldown — serve last-good or raw upstream
        if state_store.in_cooldown(tvg_id):
            fallback = state_store.last_good_url(tvg_id) or sanitize_upstream_url(ch.upstream_url)
            log.warning("cooldown %s %s — serving last-good", tvg_id, ch.name)
            return RedirectResponse(url=fallback, status_code=302)

        # 3. Fresh resolve
        upstream = sanitize_upstream_url(ch.upstream_url)
        try:
            media_url = await resolve_to_media_playlist_url(client, upstream)
            await state_store.record_success(tvg_id, media_url)
            return RedirectResponse(url=media_url, status_code=302)
        except Exception as e:
            await state_store.record_failure(tvg_id)
            s = state_store.get(tvg_id)
            log.warning("resolve failed %s %s failures=%d: %s", tvg_id, ch.name, s.failures, e)
            fallback = state_store.last_good_url(tvg_id) or upstream
            return RedirectResponse(url=fallback, status_code=302)


# ------------------------------------------------------------------
# API — channel list (for admin UI)
# ------------------------------------------------------------------
@app.get("/api/channels")
async def api_channels() -> JSONResponse:
    async with _make_client() as client:
        channels = await feed_cache.get_channels(client)

    now  = time.time()
    rows = []
    for ch in channels.values():
        s = state_store.get(ch.tvg_id)
        rows.append({
            "tvg_id":        ch.tvg_id,
            "name":          ch.name,
            "ad_type":       ch.ad_type,
            "ad_template":   ch.ad_template,
            "logo":          ch.logo,
            "included":      s.included,
            "status":        s.status,
            "failures":      s.failures,
            "last_ok_ago":   round(now - s.last_ok_at,   0) if s.last_ok_at   else None,
            "last_fail_ago": round(now - s.last_fail_at, 0) if s.last_fail_at else None,
            "cached_fresh":  bool(s.resolved_url and ch.ad_type != "serverside" and (now - s.resolved_at) < VARIANT_TTL_SECONDS),
        })

    rows.sort(key=lambda r: r["name"].lower())
    ok       = sum(1 for r in rows if r["status"] == "ok")
    err      = sum(1 for r in rows if r["status"] in ("error", "cooldown"))
    untested = sum(1 for r in rows if r["status"] == "untested")
    included = sum(1 for r in rows if r["included"])

    return JSONResponse({
        "summary":  {"total": len(rows), "ok": ok, "error": err, "untested": untested, "included": included},
        "channels": rows,
    })


# ------------------------------------------------------------------
# API — toggle channel inclusion
# ------------------------------------------------------------------
@app.post("/api/channels/{tvg_id}/toggle")
async def api_toggle(tvg_id: str) -> JSONResponse:
    async with _make_client() as client:
        channels = await feed_cache.get_channels(client)
    if tvg_id not in channels:
        raise HTTPException(status_code=404, detail="unknown channel")
    s = state_store.get(tvg_id)
    await state_store.set_included(tvg_id, not s.included)
    return JSONResponse({"tvg_id": tvg_id, "included": state_store.get(tvg_id).included})


# ------------------------------------------------------------------
# API — re-probe a single channel on demand
# ------------------------------------------------------------------
@app.post("/api/channels/{tvg_id}/probe")
async def api_probe(tvg_id: str) -> JSONResponse:
    async with _make_client() as client:
        channels = await feed_cache.get_channels(client)
        ch = channels.get(tvg_id)
        if not ch:
            raise HTTPException(status_code=404, detail="unknown channel")
        await _probe_channel(ch, client)
    s = state_store.get(tvg_id)
    return JSONResponse({"tvg_id": tvg_id, "status": s.status, "failures": s.failures})


# ------------------------------------------------------------------
# API — force feed refresh + re-probe
# ------------------------------------------------------------------
@app.post("/api/feed/refresh")
async def api_feed_refresh() -> JSONResponse:
    feed_cache._fetched_at = 0.0  # invalidate cache
    async with _make_client() as client:
        channels = await feed_cache.get_channels(client)
    asyncio.create_task(probe_all_channels(channels))
    return JSONResponse({"channels": len(channels), "probing": True})


# ------------------------------------------------------------------
# Admin UI
# ------------------------------------------------------------------
@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("admin.html", {"request": request})


# ------------------------------------------------------------------
# ------------------------------------------------------------------
# EPG / XMLTV  — real schedule data from DistroTV EPG API
# ------------------------------------------------------------------
def _xml_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;") \
                    .replace(">", "&gt;").replace('"', "&quot;")

def _genre_to_category(genre: str) -> List[str]:
    cats: List[str] = []
    mapping = {
        "news": "News", "sports": "Sports", "movies": "Movie",
        "music": "Music", "entertainment": "Entertainment",
        "lifestyle": "Lifestyle", "travel": "Travel",
        "documentary": "Documentary", "kids": "Children",
        "comedy": "Comedy", "drama": "Drama",
    }
    lower = genre.lower()
    for key, val in mapping.items():
        if key in lower and val not in cats:
            cats.append(val)
    return cats or ["General"]


# In-memory EPG cache
_epg_cache: Dict[str, object] = {"xml": None, "fetched_at": 0.0}
_epg_lock = asyncio.Lock()


async def _fetch_epg_xml(channels: Dict[str, Channel], client: httpx.AsyncClient) -> str:
    """
    Fetch real schedule data from the DistroTV EPG API and build XMLTV.
    Channel id = tvg_id (episode ID) — matches tvg-id in /playlist.m3u exactly.
    Falls back to stub entries if the API is unavailable.
    """
    import datetime

    id_to_ch: Dict[str, Channel] = {ch.tvg_id: ch for ch in channels.values()}
    all_ids = ",".join(id_to_ch.keys())

    slots_by_id: Dict[str, list] = {}
    try:
        r = await client.get(
            f"https://tv.jsrdn.com/epg/query.php?id={all_ids}",
            headers={"User-Agent": ANDROID_UA, "Accept": "application/json,*/*"},
            timeout=30,
        )
        r.raise_for_status()
        raw_epg = (r.json().get("epg") or {})
        for ep_id, ch_epg in raw_epg.items():
            slots = ch_epg.get("slots") or []
            if slots:
                slots_by_id[ep_id] = slots
        log.info("EPG fetched: %d/%d channels have schedule data", len(slots_by_id), len(channels))
    except Exception as e:
        log.warning("EPG fetch failed, using stubs: %s", e)

    now        = datetime.datetime.now(datetime.timezone.utc)
    stub_start = (now - datetime.timedelta(hours=12)).strftime("%Y%m%d%H%M%S") + " +0000"
    stub_stop  = (now + datetime.timedelta(hours=36)).strftime("%Y%m%d%H%M%S") + " +0000"

    lines: List[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE tv SYSTEM "xmltv.dtd">',
        '<tv source-info-name="DistroTV" generator-info-name="distrotv-proxy">',
    ]

    # Channel entries
    for ch in sorted(channels.values(), key=lambda c: c.name.lower()):
        cid  = _xml_escape(ch.tvg_id)
        name = _xml_escape(ch.name)
        logo = _xml_escape(ch.logo)
        lines.append(f'  <channel id="{cid}">')
        lines.append(f'    <display-name>{name}</display-name>')
        if logo:
            lines.append(f'    <icon src="{logo}"/>')
        lines.append('  </channel>')

    # Programme entries
    for ch in sorted(channels.values(), key=lambda c: c.name.lower()):
        cid   = _xml_escape(ch.tvg_id)
        lang  = _xml_escape(ch.language or "en")
        cats  = _genre_to_category(ch.genre)
        slots = slots_by_id.get(ch.tvg_id)

        if slots:
            for slot in slots:
                try:
                    s_start = datetime.datetime.strptime(
                        slot["start"], "%Y-%m-%d %H:%M:%S"
                    ).strftime("%Y%m%d%H%M%S") + " +0000"
                    s_stop  = datetime.datetime.strptime(
                        slot["end"], "%Y-%m-%d %H:%M:%S"
                    ).strftime("%Y%m%d%H%M%S") + " +0000"
                except (KeyError, ValueError):
                    continue
                title = _xml_escape((slot.get("title") or ch.name).strip())
                desc  = _xml_escape((slot.get("description") or "").strip())
                icon  = _xml_escape(slot.get("img_thumbh") or "")
                lines.append(f'  <programme start="{s_start}" stop="{s_stop}" channel="{cid}">')
                lines.append(f'    <title lang="{lang}">{title}</title>')
                if desc:
                    lines.append(f'    <desc lang="{lang}">{desc}</desc>')
                if icon:
                    lines.append(f'    <icon src="{icon}"/>')
                for cat in cats:
                    lines.append(f'    <category lang="en">{_xml_escape(cat)}</category>')
                if ch.rating:
                    lines.append('    <rating system="VCHIP">')
                    lines.append(f'      <value>{_xml_escape(ch.rating)}</value>')
                    lines.append('    </rating>')
                lines.append('  </programme>')
        else:
            # Stub fallback
            title = _xml_escape(ch.name)
            desc  = _xml_escape(ch.description)
            lines.append(f'  <programme start="{stub_start}" stop="{stub_stop}" channel="{cid}">')
            lines.append(f'    <title lang="{lang}">{title}</title>')
            if desc:
                lines.append(f'    <desc lang="{lang}">{desc}</desc>')
            for cat in cats:
                lines.append(f'    <category lang="en">{_xml_escape(cat)}</category>')
            if ch.rating:
                lines.append('    <rating system="VCHIP">')
                lines.append(f'      <value>{_xml_escape(ch.rating)}</value>')
                lines.append('    </rating>')
            lines.append('  </programme>')

    lines.append("</tv>")
    return "\n".join(lines)


@app.get("/epg.xml")
async def epg_xml() -> Response:
    async with _epg_lock:
        now = time.time()
        if _epg_cache["xml"] and (now - _epg_cache["fetched_at"]) < EPG_TTL_SECONDS:
            log.debug("EPG cache hit")
            xml = _epg_cache["xml"]
        else:
            async with _make_client() as client:
                channels = await feed_cache.get_channels(client)
                xml = await _fetch_epg_xml(channels, client)
            _epg_cache["xml"] = xml
            _epg_cache["fetched_at"] = time.time()

    return Response(
        content=xml.encode("utf-8"),
        media_type="application/xml",
        headers={"Content-Disposition": "inline; filename=epg.xml"},
    )
