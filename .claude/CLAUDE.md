# F1 Stream - Project Knowledge

## Architecture
- **Repo**: github.com/ViktorBarzin/f1-stream (standalone, moved from infra/stacks/f1-stream/files)
- **Backend**: Python 3.13, FastAPI, uvicorn
- **Frontend**: SvelteKit 5, Tailwind CSS, hls.js
- **Deployment**: Kubernetes (infra repo stacks/f1-stream/main.tf), Woodpecker CI
- **CI**: Pushes to `main` trigger build + deploy. Image tagged with commit SHA (e.g., `viktorbarzin/f1-stream:40a99942`). Deploy step uses `kubectl set image`.
- **Domain**: f1.viktorbarzin.me

## Extractors (8 active)
| Extractor | Source | Type | Notes |
|-----------|--------|------|-------|
| demo | Hardcoded test streams | m3u8 | Big Buck Bunny, Apple Bipbop, Tears of Steel |
| streamed | streamed.su API → embedsports.top | embed | F1 keyword filter. CDN (vipstreams.in) blocked by FingerprintJS — can't extract m3u8 |
| daddylive | dlhd.link → server lookup | m3u8 | Sky Sports F1 UK (channel 60). AES-128 encrypted segments, hls.js handles natively |
| aceztrims | acestrlms.pages.dev scraping | mixed | HTML scraping for iframe/m3u8 patterns |
| pitsport | pitsport.xyz RSC payload | m3u8+embed | Tries m3u8 first (dash.serveplay.site, needs Referer: pushembdz.store), falls back to embed |
| ppv | api.ppv.to/api/streams | embed | Public API, F1 events at pooembed.eu. Fallback: api.ppv.st |
| timstreams | stra.viaplus.site/main | embed | Public JSON API. 24/7 Sky Sports F1 + DAZN F1 channels at hmembeds.one |
| discord | Discord user token → channel messages | embed | Monitors 4 channels in WAC server (guild 1249169549509525545) |
| fallback | Static list from freemotorsports.com | embed | 10 aggregator sites (pitsport, rerace, timstreams, ppv, aceztrims, etc.) |

## Replays
- **Source**: r/MotorsportsReplays (Reddit public JSON API, no auth)
- **Filtering**: Flair-based ("Formula 1") + keyword fallback (F1, Grand Prix, etc.), word-boundary regex for non-F1 rejection (includes Indy NXT, MotoGP, NASCAR, etc.)
- **Event extraction**: GP_NAME_PATTERN regex + location fallback mapping (~25 F1 locations → canonical GP names)
- **Session detection**: Multi-language (English + Spanish), multi-session detection → "Full Event" for compilations
- **Grouping**: Posts grouped by race event, normalized against schedule using race_name/country/locality
- **Link types**: video (Streamable, Pixeldrain, direct .mp4 → inline player + download), embed (rerace.io, etc. → new tab), external (→ new tab)
- **API prefix**: Replay endpoints at /api/replays, /api/replays/refresh, /api/replays/video, /api/replays/download (avoids route conflict with SvelteKit /replays page)
- **Scheduling**: APScheduler every 30 min + manual refresh via POST /api/replays/refresh
- **Frontend**: /replays route with collapsible event groups, session sub-groups, SESSION_ORDER includes "Full Event"
- **Video proxy**: /api/replays/video and /api/replays/download endpoints with streaming + Range support

## Key Learnings
- **Embed iframe sandbox doesn't work**: embedsports.top explicitly checks for sandbox attribute and refuses to load player. Must use iframes without sandbox.
- **Embed proxy doesn't work**: Proxying embed pages through backend breaks `window.location.pathname` references and relative URLs in the embedded JS. Load embeds directly.
- **vipstreams.in FingerprintJS**: Direct m3u8 extraction from Streamed CDN is blocked by browser fingerprinting. Must use embed approach.
- **Click shields don't work**: Cross-origin iframes can't have their events intercepted from the parent page. window.open overrides on parent don't affect iframe context.
- **Proxy referer headers**: proxy.py has DOMAIN_REFERERS dict for passing required Referer headers. Currently: vipstreams.in → embedme.top, serveplay.site → pushembdz.store.
- **CI image tags**: Use commit SHA tags, not hardcoded versions. Previous approach with `:latest` + pull-through cache caused stale images.

## Secrets (in infra terraform.tfvars, git-crypt encrypted)
- `discord_user_token` — Discord user token for channel monitoring
- `discord_f1_guild_id` — WAC Discord guild ID
- `discord_f1_channel_ids` — Comma-separated channel IDs to monitor
- `dockerhub_registry_password` — Docker Hub PAT (also in Woodpecker as `dockerhub-pat`)

## Sites Investigated but Skipped
- **rerace.io** — HARD: Cloudflare challenge, X-Frame-Options SAMEORIGIN, triple-nested iframes
- **thetvapp.to** — MEDIUM: US geo-blocked, no Sky F1, Vigenere-encrypted m3u8 URLs
- **f1box.me** — HARD: Site is DOWN (DNS broken), heavy CSRF + AES-128 encryption
