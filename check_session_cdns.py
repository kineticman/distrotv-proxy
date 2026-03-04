#!/usr/bin/env python3
"""
check_session_cdns.py

Fetches the DistroTV live feed, resolves each channel's master playlist,
and checks whether following master→variant produces an hls_session_not_found
error. Outputs a list of CDN hosts that require session-passthrough mode.

Usage:
    python3 check_session_cdns.py
    python3 check_session_cdns.py --concurrency 10
    python3 check_session_cdns.py --output session_cdns.txt
"""

import argparse
import asyncio
import re
import sys
from collections import defaultdict
from urllib.parse import urljoin, urlsplit

import httpx

FEED_URL = "https://tv.jsrdn.com/tv_v5/getfeed.php?type=live"
CONCURRENCY = 6
TIMEOUT = 10

HLS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Origin":     "https://distro.tv",
    "Referer":    "https://distro.tv/",
}

MACRO_RE = re.compile(r"__[^_].*?__")


def sanitize_url(url: str) -> str:
    """Replace common macros with safe defaults."""
    import time, uuid
    from urllib.parse import parse_qsl, urlencode, urlunsplit
    replacements = {
        "__CACHE_BUSTER__": str(int(time.time() * 1000)),
        "__DEVICE_ID__":    str(uuid.uuid4()),
        "__env.i__":        str(uuid.uuid4()),
        "__env.u__":        "",
        "__WIDTH__":        "1920",
        "__HEIGHT__":       "1080",
        "__DEVICE__":       "Web",
        "__DEVICE_MAKE__":  "Web",
        "__DEVICE_CATEGORY__":        "web",
        "__DEVICE_ID_TYPE__":         "localStorage",
        "__DEVICE_CONNECTION_TYPE__": "2",
        "__CONNECTION_TYPE__":        "2",
        "__LIMIT_AD_TRACKING__":      "0",
        "__DNT__":          "0",
        "__IS_GDPR__":      "0",
        "__GDPR__":         "0",
        "__IS_CCPA__":      "0",
        "__USP__":          "0",
        "__GEO_COUNTRY__":  "US",
        "__GEO__":          "US",
        "__LATITUDE__":     "",
        "__LAT__":          "",
        "__LONGITUDE__":    "",
        "__LONG__":         "",
        "__GEO_DMA__":      "",
        "__DMA__":          "",
        "__GEO_TYPE__":     "2",
        "__PAGEURL_ESC__":  "https%3A%2F%2Fdistro.tv%2F",
        "__STORE_URL__":    "",
        "__APP_BUNDLE__":   "",
        "__APP_VERSION__":  "",
        "__APP_CATEGORY__": "entertainment",
        "__ADVERTISING_ID__": "",
        "__IDFA__":         "",
        "__CLIENT_IP__":    "",
        "__GDPR_CONSENT__": "",
        "__PALN__":         "",
    }
    parts = urlsplit(url)
    q = parse_qsl(parts.query, keep_blank_values=True)
    sanitized = []
    for k, v in q:
        if v in replacements:
            v = replacements[v]
        elif MACRO_RE.search(v or ""):
            v = ""
        sanitized.append((k, v))
    return url.split("?")[0] + ("?" + urlencode(sanitized, doseq=True) if sanitized else "")


def pick_best_variant(master_text: str, master_url: str) -> str | None:
    """Return the highest-bandwidth variant URL from a master playlist."""
    lines = [ln.strip() for ln in master_text.splitlines() if ln.strip()]
    best_bw, best_uri = -1, None
    for i, ln in enumerate(lines):
        if not ln.startswith("#EXT-X-STREAM-INF:"):
            continue
        bw = -1
        for part in ln.split(":", 1)[-1].split(","):
            if part.startswith("BANDWIDTH="):
                try: bw = int(part.split("=", 1)[1])
                except: pass
        j = i + 1
        while j < len(lines) and lines[j].startswith("#"):
            j += 1
        if j >= len(lines):
            continue
        if bw > best_bw:
            best_bw = bw
            best_uri = urljoin(master_url, lines[j])
    return best_uri


async def check_channel(ch: dict, client: httpx.AsyncClient, sem: asyncio.Semaphore) -> dict:
    """
    Returns a result dict:
      host        - CDN hostname
      name        - channel name
      session_err - True if variant fetch returned hls_session_not_found
      status      - 'session_err' | 'ok' | 'master_fail' | 'variant_fail' | 'no_variants'
    """
    result = {"host": None, "name": ch["name"], "status": "ok", "session_err": False}
    url = sanitize_url(ch["url"])
    host = urlsplit(url).netloc
    result["host"] = host

    async with sem:
        try:
            # Fetch master
            r = await client.get(url, headers=HLS_HEADERS, timeout=TIMEOUT)
            if r.status_code >= 400:
                result["status"] = "master_fail"
                return result

            text = r.text or ""

            # Not a master playlist — nothing to check
            if "#EXT-X-STREAM-INF" not in text:
                result["status"] = "ok"
                return result

            # Pick best variant
            variant = pick_best_variant(text, str(r.url))
            if not variant:
                result["status"] = "no_variants"
                return result

            # Fetch variant
            r2 = await client.get(variant, headers=HLS_HEADERS, timeout=TIMEOUT)
            body = r2.text or ""

            if "hls_session_not_found" in body or "session_not_found" in body.lower():
                result["status"] = "session_err"
                result["session_err"] = True
                return result

            if r2.status_code >= 400:
                result["status"] = "variant_fail"
                return result

            result["status"] = "ok"
            return result

        except Exception as e:
            result["status"] = f"error: {e}"
            return result


async def main(concurrency: int, output: str | None) -> None:
    print("Fetching feed…", flush=True)
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        r = await client.get(
            FEED_URL,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://distro.tv/"},
        )
        r.raise_for_status()
        data = r.json()

    shows = data.get("shows", {})
    channels = []
    for show in (shows.values() if isinstance(shows, dict) else shows):
        if not isinstance(show, dict) or show.get("type") != "live":
            continue
        name = (show.get("title") or "").strip()
        seasons = show.get("seasons") or []
        if not seasons: continue
        eps = seasons[0].get("episodes") or []
        if not eps: continue
        url = (eps[0].get("content") or {}).get("url", "")
        if url:
            channels.append({"name": name, "url": url})

    print(f"Testing {len(channels)} channels (concurrency={concurrency})…\n", flush=True)

    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT) as client:
        results = await asyncio.gather(*[check_channel(ch, client, sem) for ch in channels])

    # Aggregate by host
    host_stats: dict[str, dict] = defaultdict(lambda: {"total": 0, "session_err": 0, "channels": []})
    session_hosts = set()

    for r in results:
        host = r["host"] or "unknown"
        host_stats[host]["total"] += 1
        if r["session_err"]:
            host_stats[host]["session_err"] += 1
            host_stats[host]["channels"].append(r["name"])
            session_hosts.add(host)

    # Print summary
    print("=" * 70)
    print("RESULTS BY CDN HOST")
    print("=" * 70)
    for host, stats in sorted(host_stats.items(), key=lambda x: -x[1]["session_err"]):
        flag = "  ⚠  SESSION CDN" if stats["session_err"] > 0 else ""
        print(f"{host:<50} {stats['session_err']:>3}/{stats['total']:>3} session errors{flag}")
        if stats["session_err"] > 0:
            for ch in stats["channels"][:3]:
                print(f"    - {ch}")
            if len(stats["channels"]) > 3:
                print(f"    … and {len(stats['channels']) - 3} more")

    print()
    print("=" * 70)
    print("SESSION_CDN_HOSTS (paste into app.py)")
    print("=" * 70)
    print("SESSION_CDN_HOSTS = {")
    for host in sorted(session_hosts):
        print(f'    "{host}",')
    print("}")

    if output:
        with open(output, "w") as f:
            for host in sorted(session_hosts):
                f.write(host + "\n")
        print(f"\nWrote {len(session_hosts)} hosts to {output}")

    # Per-channel error summary
    errors = [r for r in results if r["status"] not in ("ok", "session_err")]
    if errors:
        print(f"\n{len(errors)} channels with other errors:")
        for r in errors[:10]:
            print(f"  [{r['status']}] {r['name']}")
        if len(errors) > 10:
            print(f"  … and {len(errors) - 10} more")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Detect session-based CDN hosts in DistroTV feed")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    parser.add_argument("--output", type=str, default=None, help="Write session CDN hosts to file")
    args = parser.parse_args()
    asyncio.run(main(args.concurrency, args.output))
