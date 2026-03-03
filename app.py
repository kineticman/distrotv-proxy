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
BROWSER_UA  = "Mozilla/5.0"

# -----------------------------
# Tunables
# -----------------------------
DEFAULT_GROUP             = "DistroTV"
DEFAULT_TTL_SECONDS       = 43200        # feed cache TTL (12h — feed changes infrequently)
HTTP_TIMEOUT_SECONDS      = 15           # per-request timeout
FEED_RETRY_ATTEMPTS       = 3
FEED_RETRY_SLEEP_SECONDS  = 0.75
EPG_TTL_SECONDS                = 3600  # cache EPG data for 1 hour
VARIANT_TTL_SECONDS            = 300   # mediatailor / empty channels — 5 min
VARIANT_TTL_SERVERSIDE_SECONDS = 0     # serverside channels — never cache (time-anchored URLs)
CHANNEL_COOLDOWN_SECONDS       = 120   # cooldown after MAX_CONSECUTIVE_FAILURES
MAX_CONSECUTIVE_FAILURES  = 3
PROBE_CONCURRENCY         = 12           # parallel workers during startup probe
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


async def resolve_to_media_playlist_url(client: httpx.AsyncClient, upstream_url: str) -> str:
    r = await client.get(
        upstream_url,
        headers={"User-Agent": BROWSER_UA, "Referer": "https://distro.tv/"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    r.raise_for_status()
    text = r.text or ""
    if "#EXT-X-STREAM-INF" not in text:
        return upstream_url
    variant = pick_variant_from_master(text, upstream_url)
    return variant or upstream_url


# -----------------------------
# Startup probe
# -----------------------------
async def _probe_channel(ch: Channel, client: httpx.AsyncClient) -> None:
    upstream = sanitize_upstream_url(ch.upstream_url)
    try:
        media_url = await resolve_to_media_playlist_url(client, upstream)
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
            async with httpx.AsyncClient(follow_redirects=True, timeout=HTTP_TIMEOUT_SECONDS) as client:
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
# Admin UI
# ------------------------------------------------------------------
ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>DistroTV Admin</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:       #0a0c10;
    --surface:  #111318;
    --border:   #1e2330;
    --accent:   #00e5ff;
    --ok:       #00c896;
    --err:      #ff4757;
    --warn:     #ffa502;
    --muted:    #4a5568;
    --text:     #cdd6f4;
    --text-dim: #6c7a96;
    --mono:     'IBM Plex Mono', monospace;
    --sans:     'IBM Plex Sans', sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--sans); font-size: 14px; min-height: 100vh; }

  body::before {
    content: '';
    position: fixed; inset: 0;
    background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,229,255,.012) 2px, rgba(0,229,255,.012) 4px);
    pointer-events: none; z-index: 9999;
  }

  header {
    border-bottom: 1px solid var(--border);
    padding: 16px 28px;
    display: flex; align-items: center; gap: 14px;
    background: var(--surface);
  }
  header h1 { font-family: var(--mono); font-size: 17px; color: var(--accent); letter-spacing: .08em; text-transform: uppercase; }
  .version { font-family: var(--mono); font-size: 11px; color: var(--muted); background: var(--border); padding: 2px 8px; border-radius: 3px; }
  .m3u-link { font-family: var(--mono); font-size: 11px; color: var(--accent); text-decoration: none; border: 1px solid rgba(0,229,255,.25); padding: 3px 10px; border-radius: 3px; transition: background .15s; }
  .m3u-link:hover { background: rgba(0,229,255,.08); }
  .pulse { width: 8px; height: 8px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 8px var(--ok); animation: pulse 2s ease-in-out infinite; margin-left: auto; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }

  .summary-bar { display: flex; gap: 1px; padding: 14px 28px; background: var(--surface); border-bottom: 1px solid var(--border); }
  .stat { flex: 1; padding: 12px 16px; background: var(--bg); border: 1px solid var(--border); }
  .stat:first-child { border-radius: 6px 0 0 6px; }
  .stat:last-child  { border-radius: 0 6px 6px 0; }
  .stat .val { font-family: var(--mono); font-size: 26px; font-weight: 600; line-height: 1; }
  .stat .lbl { font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: .1em; margin-top: 4px; }
  .stat.s-info .val { color: var(--accent); }
  .stat.s-ok   .val { color: var(--ok); }
  .stat.s-err  .val { color: var(--err); }
  .stat.s-dim  .val { color: var(--muted); }

  .toolbar {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    padding: 12px 28px; border-bottom: 1px solid var(--border); background: var(--surface);
  }
  .toolbar input, .toolbar select {
    background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
    color: var(--text); font-family: var(--mono); font-size: 12px; padding: 6px 10px; outline: none;
    transition: border-color .2s;
  }
  .toolbar input { width: 200px; }
  .toolbar input:focus { border-color: var(--accent); }
  .spacer { flex: 1; }
  .refresh-info { font-family: var(--mono); font-size: 11px; color: var(--muted); }
  .btn { background: transparent; border: 1px solid var(--border); border-radius: 4px; color: var(--text-dim); font-family: var(--mono); font-size: 12px; padding: 6px 14px; cursor: pointer; transition: all .15s; }
  .btn:hover, .btn.primary { border-color: var(--accent); color: var(--accent); }

  .table-wrap { overflow-x: auto; padding: 0 28px 60px; }
  table { width: 100%; border-collapse: collapse; margin-top: 16px; }
  thead th { font-family: var(--mono); font-size: 11px; text-transform: uppercase; letter-spacing: .1em; color: var(--text-dim); text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border); cursor: pointer; white-space: nowrap; user-select: none; }
  thead th:hover { color: var(--accent); }
  thead th.sorted { color: var(--accent); }
  thead th.sorted::after { content: attr(data-dir); margin-left: 4px; }
  tbody tr { border-bottom: 1px solid var(--border); transition: background .1s; }
  tbody tr:hover { background: rgba(0,229,255,.025); }
  tbody td { padding: 9px 12px; vertical-align: middle; }

  .ch-logo { width: 32px; height: 32px; object-fit: contain; background: var(--border); border-radius: 4px; display: block; }
  .ch-logo-ph { width: 32px; height: 32px; background: var(--border); border-radius: 4px; display: flex; align-items: center; justify-content: center; font-size: 9px; color: var(--muted); }
  .ch-name { font-weight: 600; }
  .ch-id   { font-family: var(--mono); font-size: 11px; color: var(--text-dim); margin-top: 2px; }

  .badge { display: inline-block; font-family: var(--mono); font-size: 10px; padding: 2px 7px; border-radius: 3px; text-transform: uppercase; letter-spacing: .05em; font-weight: 600; }
  .b-ok       { background: rgba(0,200,150,.12); color: var(--ok);    border: 1px solid rgba(0,200,150,.3); }
  .b-error    { background: rgba(255,71,87,.12);  color: var(--err);   border: 1px solid rgba(255,71,87,.3); }
  .b-cooldown { background: rgba(255,165,2,.12);  color: var(--warn);  border: 1px solid rgba(255,165,2,.3); }
  .b-untested { background: rgba(74,85,104,.15);  color: var(--muted); border: 1px solid rgba(74,85,104,.4); }
  .b-mediatailor { background: rgba(124,58,237,.12); color: #a78bfa; border: 1px solid rgba(124,58,237,.3); }
  .b-serverside  { background: rgba(0,229,255,.08);  color: var(--accent); border: 1px solid rgba(0,229,255,.25); }
  .b-empty       { background: rgba(0,200,150,.08);  color: var(--ok);     border: 1px solid rgba(0,200,150,.25); }

  .failures { font-family: var(--mono); font-size: 12px; }
  .failures.bad { color: var(--err); }
  .ago { font-family: var(--mono); font-size: 11px; color: var(--text-dim); }

  .toggle { position: relative; display: inline-block; width: 36px; height: 20px; flex-shrink: 0; }
  .toggle input { opacity: 0; width: 0; height: 0; }
  .toggle-slider { position: absolute; inset: 0; background: var(--border); border-radius: 20px; cursor: pointer; transition: background .2s; }
  .toggle-slider::before { content: ''; position: absolute; width: 14px; height: 14px; left: 3px; bottom: 3px; background: var(--muted); border-radius: 50%; transition: transform .2s, background .2s; }
  .toggle input:checked + .toggle-slider { background: rgba(0,200,150,.25); }
  .toggle input:checked + .toggle-slider::before { transform: translateX(16px); background: var(--ok); }

  .act-btn { background: transparent; border: 1px solid var(--border); border-radius: 3px; color: var(--text-dim); font-family: var(--mono); font-size: 10px; padding: 3px 8px; cursor: pointer; transition: all .15s; }
  .act-btn:hover { border-color: var(--accent); color: var(--accent); }

  .loading, .empty-state { text-align: center; padding: 60px; font-family: var(--mono); color: var(--muted); font-size: 13px; }
  .blink { animation: blink 1s step-start infinite; }
  @keyframes blink { 50% { opacity: 0; } }
</style>
</head>
<body>

<header>
  <h1>⬡ DistroTV Admin</h1>
  <span class="version">v0.7.0</span>
  <a class="m3u-link" href="/playlist.m3u" target="_blank">↓ playlist.m3u</a>
  <div class="pulse"></div>
</header>

<div class="summary-bar">
  <div class="stat s-info"><div class="val" id="s-total">—</div><div class="lbl">Total</div></div>
  <div class="stat s-ok">  <div class="val" id="s-ok">—</div><div class="lbl">Working</div></div>
  <div class="stat s-err"> <div class="val" id="s-err">—</div><div class="lbl">Errors</div></div>
  <div class="stat s-dim"> <div class="val" id="s-untested">—</div><div class="lbl">Untested</div></div>
  <div class="stat s-info"><div class="val" id="s-included">—</div><div class="lbl">In M3U</div></div>
</div>

<div class="toolbar">
  <input type="text" id="search" placeholder="Search channels…" oninput="applyFilters()"/>
  <select id="f-status" onchange="applyFilters()">
    <option value="">All statuses</option>
    <option value="ok">✅ OK</option>
    <option value="error">❌ Error</option>
    <option value="cooldown">⚠️ Cooldown</option>
    <option value="untested">· Untested</option>
  </select>
  <select id="f-adtype" onchange="applyFilters()">
    <option value="">All ad types</option>
    <option value="mediatailor">mediatailor</option>
    <option value="serverside">serverside</option>
    <option value="empty">empty</option>
  </select>
  <select id="f-included" onchange="applyFilters()">
    <option value="">All channels</option>
    <option value="true">Included in M3U</option>
    <option value="false">Excluded</option>
  </select>
  <div class="spacer"></div>
  <span class="refresh-info" id="refresh-info">—</span>
  <button class="btn primary" onclick="loadData()">↺ Refresh</button>
</div>

<div class="table-wrap">
  <div class="loading" id="loading">Loading<span class="blink">_</span></div>
  <div class="empty-state" id="empty-state" style="display:none">No channels match your filters.</div>
  <table id="table" style="display:none">
    <thead>
      <tr>
        <th style="width:40px;cursor:default"></th>
        <th data-key="name" data-dir="↑" onclick="sortBy(this)">Channel</th>
        <th data-key="status" onclick="sortBy(this)">Status</th>
        <th data-key="ad_type" onclick="sortBy(this)">Ad Type</th>
        <th data-key="failures" onclick="sortBy(this)">Failures</th>
        <th data-key="last_ok_ago" onclick="sortBy(this)">Last OK</th>
        <th data-key="last_fail_ago" onclick="sortBy(this)">Last Fail</th>
        <th style="cursor:default">In M3U</th>
        <th style="cursor:default">Actions</th>
      </tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>
</div>

<script>
let allChannels = [];
let sortKey = 'name';
let sortAsc = true;
let timer = null;
let lastLoaded = null;

async function loadData() {
  try {
    const d = await fetch('/api/channels').then(r => r.json());
    allChannels = d.channels;
    lastLoaded  = new Date();
    document.getElementById('s-total').textContent    = d.summary.total;
    document.getElementById('s-ok').textContent       = d.summary.ok;
    document.getElementById('s-err').textContent      = d.summary.error;
    document.getElementById('s-untested').textContent = d.summary.untested;
    document.getElementById('s-included').textContent = d.summary.included;
    applyFilters();
    clearTimeout(timer);
    timer = setTimeout(loadData, 10000);
    document.getElementById('refresh-info').textContent =
      'Updated ' + lastLoaded.toLocaleTimeString() + ' · auto 10s';
  } catch(e) {
    document.getElementById('refresh-info').textContent = '⚠ fetch error';
  }
}

function fmtAgo(s) {
  if (s == null) return '—';
  if (s < 60)   return Math.round(s) + 's ago';
  if (s < 3600) return Math.round(s/60) + 'm ago';
  return Math.round(s/3600) + 'h ago';
}

const STATUS_BADGE = {
  ok:       '<span class="badge b-ok">✓ ok</span>',
  error:    '<span class="badge b-error">✗ error</span>',
  cooldown: '<span class="badge b-cooldown">⏸ cooldown</span>',
  untested: '<span class="badge b-untested">· untested</span>',
};

function applyFilters() {
  const q  = document.getElementById('search').value.toLowerCase();
  const fs = document.getElementById('f-status').value;
  const fa = document.getElementById('f-adtype').value;
  const fi = document.getElementById('f-included').value;

  let rows = allChannels.filter(c => {
    if (q  && !c.name.toLowerCase().includes(q) && !c.tvg_id.includes(q)) return false;
    if (fs && c.status   !== fs)              return false;
    if (fa && c.ad_type  !== fa)              return false;
    if (fi && String(c.included) !== fi)      return false;
    return true;
  });

  rows.sort((a, b) => {
    let av = a[sortKey], bv = b[sortKey];
    const nil = sortAsc ? Infinity : -Infinity;
    if (av == null) av = nil;
    if (bv == null) bv = nil;
    if (typeof av === 'string') return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
    return sortAsc ? av - bv : bv - av;
  });

  const loading = document.getElementById('loading');
  const table   = document.getElementById('table');
  const empty   = document.getElementById('empty-state');
  const tbody   = document.getElementById('tbody');

  loading.style.display = 'none';

  if (rows.length === 0) {
    table.style.display = 'none';
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';
  table.style.display = 'table';

  tbody.innerHTML = rows.map(c => {
    const logo = c.logo
      ? `<img class="ch-logo" src="${c.logo}" alt="" loading="lazy" onerror="this.outerHTML='<div class=ch-logo-ph>?</div>'">`
      : `<div class="ch-logo-ph">?</div>`;
    const badge = STATUS_BADGE[c.status] || `<span class="badge b-untested">${c.status}</span>`;
    const adB   = `<span class="badge b-${c.ad_type}">${c.ad_type}</span>`;
    const fail  = `<span class="failures ${c.failures > 0 ? 'bad' : ''}">${c.failures}</span>`;
    return `
    <tr>
      <td>${logo}</td>
      <td><div class="ch-name">${c.name}</div><div class="ch-id">#${c.tvg_id}</div></td>
      <td>${badge}</td>
      <td>${adB}</td>
      <td>${fail}</td>
      <td class="ago">${fmtAgo(c.last_ok_ago)}</td>
      <td class="ago">${fmtAgo(c.last_fail_ago)}</td>
      <td>
        <label class="toggle">
          <input type="checkbox" ${c.included ? 'checked' : ''}
            onchange="toggleCh('${c.tvg_id}', this)"/>
          <span class="toggle-slider"></span>
        </label>
      </td>
      <td>
        <button class="act-btn" onclick="probeCh('${c.tvg_id}', this)">probe</button>
      </td>
    </tr>`;
  }).join('');
}

function sortBy(th) {
  const key = th.dataset.key;
  if (sortKey === key) { sortAsc = !sortAsc; }
  else { sortKey = key; sortAsc = true; }
  document.querySelectorAll('thead th').forEach(t => { t.classList.remove('sorted'); delete t.dataset.dir; });
  th.classList.add('sorted');
  th.dataset.dir = sortAsc ? '↑' : '↓';
  applyFilters();
}

async function toggleCh(id, cb) {
  cb.disabled = true;
  try {
    const d = await fetch(`/api/channels/${id}/toggle`, {method:'POST'}).then(r=>r.json());
    cb.checked = d.included;
    const c = allChannels.find(x => x.tvg_id === id);
    if (c) c.included = d.included;
    document.getElementById('s-included').textContent = allChannels.filter(x=>x.included).length;
  } catch { cb.checked = !cb.checked; }
  cb.disabled = false;
}

async function probeCh(id, btn) {
  btn.textContent = '…'; btn.disabled = true;
  try {
    const d = await fetch(`/api/channels/${id}/probe`, {method:'POST'}).then(r=>r.json());
    const c = allChannels.find(x => x.tvg_id === id);
    if (c) { c.status = d.status; c.failures = d.failures; if (d.status==='ok') c.last_ok_ago=0; else c.last_fail_ago=0; }
    applyFilters();
  } catch { btn.textContent = '!'; }
  btn.textContent = 'probe'; btn.disabled = false;
}

loadData();
</script>
</body>
</html>"""


@app.get("/admin", response_class=HTMLResponse)
async def admin() -> HTMLResponse:
    return HTMLResponse(content=ADMIN_HTML)


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
