# distrotv-proxy

A lightweight FastAPI proxy that makes [DistroTV](https://distro.tv) live streams work reliably in **Channels DVR**, **VLC**, and other HLS clients.

## Why this exists

DistroTV streams use server-side ad insertion (SSAI) with time-anchored HLS URLs that expire within seconds. Standard M3U playlists cached by other tools go stale and stop working. This proxy resolves a fresh, playable media playlist URL at the moment each channel is requested — so streams always start.

## Features

- **`/playlist.m3u`** — M3U playlist pointing to local proxy endpoints, with channel logos
- **`/play/{id}.m3u8`** — Per-channel redirect to a current, segment-bearing HLS playlist
- **`/epg.xml`** — XMLTV guide data with real schedules from the DistroTV EPG API
- **`/admin`** — Web UI to monitor channel health and toggle channels in/out of the M3U
- Startup probe tests all ~300 channels and reports working/broken status
- Smart caching: `mediatailor` channels cache for 5 min; `serverside` (time-anchored) channels always resolve fresh
- Cooldown logic prevents hammering failing upstream servers
- Channel state (toggle exclusions) persists across restarts via JSON file

## Quick Start

### Docker Compose (recommended)

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/distrotv-proxy
cd distrotv-proxy

# 2. Start it
docker compose up -d

# 3. Check it's running
curl http://localhost:8787/health
```

The service will be available at `http://YOUR_HOST_IP:8787`.

### Docker CLI

```bash
docker run -d \
  --name distrotv-proxy \
  --restart unless-stopped \
  -p 8787:8787 \
  -v distrotv-data:/data \
  ghcr.io/YOUR_USERNAME/distrotv-proxy:latest
```

## Channels DVR Setup

### 1. Add the M3U Source

- Settings → Sources → **Add Source** → M3U Playlist
- URL: `http://YOUR_HOST_IP:8787/playlist.m3u`
- Refresh: every 24 hours (or as needed)

### 2. Add the EPG Source

- Settings → Sources → **Add Source** → XMLTV Guide Data
- URL: `http://YOUR_HOST_IP:8787/epg.xml`
- Refresh: every 1 hour

### 3. Map Channels to Guide Data

Channels DVR should auto-match by `tvg-id`. If it doesn't:
- Go to each channel → Guide → match by `tvg-id`

## Admin UI

Open `http://YOUR_HOST_IP:8787/admin` in a browser.

| Feature | Description |
|---|---|
| Status badges | ✅ ok / ❌ error / ⚠️ cooldown / · untested |
| Ad type | `mediatailor`, `serverside`, or `empty` — shows the upstream ad tech |
| Failures | Consecutive resolve failures since last success |
| In M3U toggle | Exclude broken/unwanted channels from the playlist |
| Probe button | Re-test a specific channel on demand |

The admin page auto-refreshes every 10 seconds.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8787` | Port to listen on |
| `STATE_FILE` | `/data/channel_state.json` | Path to persisted channel state |

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | `{"ok": true}` — liveness check |
| `GET /playlist.m3u` | M3U playlist (included channels only) |
| `GET /play/{id}.m3u8` | Per-channel HLS redirect |
| `GET /epg.xml` | XMLTV guide data (cached 1 hour) |
| `GET /admin` | Admin web UI |
| `GET /api/channels` | JSON channel list with status |
| `POST /api/channels/{id}/toggle` | Toggle channel inclusion |
| `POST /api/channels/{id}/probe` | Re-probe a single channel |

## Building from Source

```bash
# Build image
docker build -t distrotv-proxy .

# Run locally
docker compose up
```

## Publishing to ghcr.io

```bash
# Authenticate
echo $GITHUB_TOKEN | docker login ghcr.io -u YOUR_USERNAME --password-stdin

# Build and push
docker build -t ghcr.io/YOUR_USERNAME/distrotv-proxy:latest .
docker push ghcr.io/YOUR_USERNAME/distrotv-proxy:latest

# Tag a release
docker tag ghcr.io/YOUR_USERNAME/distrotv-proxy:latest ghcr.io/YOUR_USERNAME/distrotv-proxy:v1.0.0
docker push ghcr.io/YOUR_USERNAME/distrotv-proxy:v1.0.0
```

## GitHub Actions (Auto-publish on push)

Create `.github/workflows/docker.yml` to auto-build and push on every release tag:

```yaml
name: Build and Push Docker Image

on:
  push:
    tags: ["v*"]

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/metadata-action@v5
        id: meta
        with:
          images: ghcr.io/${{ github.repository }}
      - uses: docker/build-push-action@v5
        with:
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
```

## Known Limitations

- ~8 channels using AVOD segment stitching are incompatible with standard HLS clients regardless of proxy approach. They will show as `error` in the admin UI and can be toggled off.
- EPG schedule data depends on the DistroTV EPG API being available. If it's down, the proxy falls back to channel-info stubs automatically.
- `serverside` channels (time-anchored URLs) resolve fresh on every play request — slightly slower to start than cached channels but necessary for reliability.

## License

MIT
