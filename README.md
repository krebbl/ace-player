# Ace Player (self-hosted)

Mobile-friendly web UI to browse and play Ace Stream channels via HLS.

> For personal use only. The catalog is built by scraping public Ace Stream
> directories. You are responsible for what you watch.

## What it gives you

- `blaiseio/acelink` Ace Stream Engine running in a container.
- A small FastAPI sidecar that:
  - Searches [search-ace.stream](https://search-ace.stream/) on demand via
    its JSON API (`GET /search?query=…`) when the user submits a query.
    Results are upserted into a local SQLite catalog and a per-query TTL
    (1 h) keeps repeat searches off the wire.
  - Resolves a content ID via the engine, then ffmpeg-repackages the live
    MPEG-TS stream into HLS (`-c copy`, no re-encode) with permissive CORS.
  - Serves a mobile-first SPA at `/`.

The catalog grows organically: anything any user has ever searched for stays
browsable in the grid forever (until you `docker volume rm` the `catalog`
volume). There is no boot-time scrape and no scheduler.

Two containers, one internal network. The web UI is bound to `127.0.0.1:8080`
by default — expose it over Tailscale or your LAN as you see fit.

## Run it

```sh
docker compose up -d --build
open http://localhost:8080            # macOS
xdg-open http://localhost:8080         # Linux
```

The grid is empty until the first search — type a keyword and the backend
hits search-ace.stream's JSON API. Subsequent identical searches hit the
local SQLite cache (1 h TTL). Force a refresh with
`POST /api/refresh?q=<keyword>`.

```sh
curl -s http://localhost:8080/api/health | jq
docker compose logs -f app
```

## Configuration

Environment variables (see `docker-compose.yml`):

| Var                  | Default                  | Notes                              |
|----------------------|--------------------------|------------------------------------|
| `ACE_ENGINE_URL`     | `http://acestream:6878`  | Internal DNS, don't change         |
| `HLS_IDLE_TIMEOUT`   | `120`                    | Seconds before idle ffmpeg killed  |
| `DB_PATH`            | `/data/catalog.db`       | SQLite location (volume `catalog`) |
| `HLS_DIR`            | `/tmp/hls`               | Segment scratch dir                |

Search behaviour constants live in `app/scraper.py`:
`CACHE_TTL_S` (default 1 h), `TIMEOUT_S` (10 s).

## Exposing on your network

By default the app listens on `0.0.0.0:8080` so any device on the same LAN
can reach it. There is **no auth** — only run this on a trusted network.

**Tailscale**: simplest path is to keep the LAN bind and run a
`tailscale/tailscale` sidecar that joins the same Docker network and
exposes `http://app:8080` via Tailscale Serve. Out of scope for this
repo's compose file — add it locally.

**LAN-only**: change the port mapping in `docker-compose.yml` to
`127.0.0.1:8080:8080` if you don't want it reachable from other devices.

## API

| Endpoint                                 | Purpose                        |
|------------------------------------------|--------------------------------|
| `GET /api/health`                        | Engine + recent searches + active streams |
| `GET /api/categories`                    | Distinct categories            |
| `GET /api/streams?q=&category=&page=`    | Search the catalog (triggers a fan-out if `q` isn't fresh in cache) |
| `GET /playlist?category=`                | M3U channel list for IPTV players (`group-title` = category) |
| `POST /api/refresh?q=<keyword>`          | Force a fresh upstream fan-out for `q`     |
| `POST /api/stop/<content_id>`            | Tear down a running ffmpeg     |
| `GET /hls/<content_id>/index.m3u8`       | HLS playlist (lazy-starts ffmpeg) |
| `GET /hls/<content_id>/seg_NNNNN.ts`     | HLS segment                    |

## Caveats

- **`blaiseio/acelink:latest` is third-party and amd64-only.** The compose
  file forces `platform: linux/amd64`, so on Apple Silicon / arm64 Linux it
  runs emulated. On macOS this is Rosetta (fast, near-native) if you've
  enabled it in Docker Desktop → Settings → General → "Use Rosetta for x86_64
  emulation"; otherwise qemu (works, ~2× slower). On native amd64 hosts the
  `platform:` line is a no-op. Pin to a digest in production
  (`image: blaiseio/acelink@sha256:…`) once you've settled on a known-good build.
- **Cold start is slow.** Ace Engine peer discovery on a fresh content ID can
  take 10–30 seconds. The UI shows a "Resolving stream…" message.
- **Single upstream source.** If search-ace.stream changes its API or goes
  down, search returns nothing. Check `GET /api/health` — `recent_searches`
  shows last_count and last_error per query. Swap or add a source by
  editing `app/scraper.py`.
- **First search per keyword takes 1–10 s** while the upstream call runs.
  Cached hits return instantly. The frontend shows "Searching upstream
  source…" during the wait.
- **HLS adds ~10–20 s of latency.** That's HLS, not this code. If you need
  near-live latency, fall back to the original MPEG-TS approach with
  mpegts.js — but it won't work on iPhone Safari.
- **Engine-native HLS.** If your Ace build returns an `.m3u8` directly from
  `playback_url`, the ffmpeg layer is redundant. Verify with:
  `docker compose exec app curl -s 'http://acestream:6878/ace/manifest.m3u8?id=<id>&format=json' | jq`.
  If `playback_url` ends in `.m3u8`, replace `hls.py` with a thin CORS proxy
  later.

## Layout

```
.
├── docker-compose.yml
├── README.md
└── app/
    ├── Dockerfile
    ├── requirements.txt
    ├── main.py            FastAPI app + routes
    ├── db.py              SQLite (aiosqlite)
    ├── acestream.py       Engine HTTP API client
    ├── hls.py             ffmpeg subprocess pool + idle GC
    ├── scraper.py         On-demand parallel scrapers + per-query cache
    └── static/
        ├── index.html
        ├── app.js
        ├── style.css
        └── vendor/hls.min.js   (fetched at image build time)
```
