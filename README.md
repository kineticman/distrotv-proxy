# distrotv-proxy

A lightweight proxy that makes [DistroTV](https://distro.tv) live streams work reliably in **Channels DVR**, **VLC**, and other HLS clients.

DistroTV streams use time-anchored URLs that expire within seconds. Standard M3U playlists cached by other tools go stale and stop working. This proxy fetches a fresh, playable URL at the moment each channel is requested — so streams always start.

## Features

- ~300 live channels with working HLS streams
- Real EPG schedule data from DistroTV
- Admin UI to monitor channel health and exclude broken channels from your playlist
- Startup probe tests all channels on boot and reports status
- Channel exclusions persist across restarts

---

## Installation

### Portainer (recommended)

1. In Portainer, go to **Stacks → Add Stack**
2. Name it `distrotv-proxy`
3. Paste the following into the Web Editor:

```yaml
services:
  distrotv-proxy:
    image: ghcr.io/kineticman/distrotv-proxy:latest
    container_name: distrotv-proxy
    restart: unless-stopped
    ports:
      - "8787:8787"
    volumes:
      - distrotv-data:/data

volumes:
  distrotv-data:
```

4. Click **Deploy the stack**
5. Wait about 30 seconds for the startup probe to finish testing all channels

The service will be available at `http://YOUR_HOST_IP:8787`.

---

### Docker Compose

```bash
docker compose up -d
```

### Docker CLI

```bash
docker run -d \
  --name distrotv-proxy \
  --restart unless-stopped \
  -p 8787:8787 \
  -v distrotv-data:/data \
  ghcr.io/kineticman/distrotv-proxy:latest
```

---

## Channels DVR Setup

### 1. Add the M3U Source

- Settings → Sources → **Add Source** → M3U Playlist
- URL: `http://YOUR_HOST_IP:8787/playlist.m3u`

### 2. Add the EPG Source

- Settings → Sources → **Add Source** → XMLTV Guide Data
- URL: `http://YOUR_HOST_IP:8787/epg.xml`

Channels DVR will auto-match guide data to channels by `tvg-id`.

---

## Admin UI

Open `http://YOUR_HOST_IP:8787/admin` in a browser to see channel health and manage your playlist.

| Column | Description |
|---|---|
| Status | ✅ ok / ❌ error / ⚠️ cooldown / · untested |
| Ad Type | The upstream ad technology used by each channel |
| Failures | Consecutive failed attempts since last success |
| In M3U | Toggle to include/exclude a channel from your playlist |
| Probe | Re-test a channel on demand |

The page auto-refreshes every 10 seconds. Toggle exclusions are saved automatically and survive restarts.

---

## Known Limitations

- A small number of channels (~8) use a segment-stitching technology that is fundamentally incompatible with standard HLS clients. These will show as `error` in the admin UI and can be toggled off.
- EPG data depends on the DistroTV EPG API. If unavailable, the proxy falls back to channel-info stubs automatically.

## License

MIT
