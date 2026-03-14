"""F1 Streams - FastAPI backend with schedule, stream extraction, health checking, HLS proxy, and token refresh."""

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from starlette.responses import Response, StreamingResponse

from backend.extractors import create_extraction_service
from backend.proxy import proxy_playlist, relay_stream
from backend.replays import ReplayService
from backend.schedule import ScheduleService
from backend.token_refresh import TokenRefreshManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

schedule_service = ScheduleService()
extraction_service = create_extraction_service()
token_refresh_manager = TokenRefreshManager(extraction_service)
replay_service = ReplayService()
scheduler = AsyncIOScheduler()

# --- TorrServer config ---
TORRSERVER_URL = os.environ.get("TORRSERVER_URL", "http://torrserver.tor-proxy.svc.cluster.local:8090")
_TORRENT_HASH_RE = re.compile(r"^[a-fA-F0-9]{40}$")
_active_torrents: dict[str, float] = {}  # hash → last_access_timestamp
_torrent_file_names: dict[str, dict[int, str]] = {}  # hash → {file_index: basename}
_torrent_media_info_cache: dict[str, dict[int, dict]] = {}  # hash → {file_index: media_info}
_torrent_hls_sessions: dict[str, dict[int, dict]] = {}  # hash → {file_index: session_info}
_torrents_lock = asyncio.Lock()
_torrent_hls_session_guard = asyncio.Lock()
_torrent_status_cache: tuple[bool, float] = (False, 0.0)  # (available, timestamp)
_DIRECT_PLAY_FILE_EXTENSIONS = {".mp4", ".m4v", ".webm"}
_DIRECT_PLAY_VIDEO_CODECS = {"h264", "hevc", "av1", "vp8", "vp9"}
_DIRECT_PLAY_AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis", "flac", "pcm_s16le", "pcm_s24le"}
_MP4_COPY_VIDEO_CODECS = {"h264", "hevc", "av1"}
_HLS_COPY_VIDEO_CODECS = {"h264"}


# --- Pydantic models for request bodies ---


class ActivateStreamRequest(BaseModel):
    """Request body for POST /streams/activate."""

    url: str
    site_key: str = ""


class DeactivateStreamRequest(BaseModel):
    """Request body for POST /streams/deactivate."""

    url: str


class TorrentFilesRequest(BaseModel):
    """Request body for POST /api/replays/torrent-files."""

    magnet: str


class TorrentStopRequest(BaseModel):
    """Request body for POST /api/replays/torrent-stop."""

    hash: str


class TorrentHeartbeatRequest(BaseModel):
    """Request body for POST /api/replays/torrent-heartbeat."""

    hash: str


async def _resolve_tracked_torrent_file_name(hash_value: str, index: int) -> str | None:
    """Resolve a tracked torrent file name from cache or TorrServer metadata."""
    async with _torrents_lock:
        file_name = _torrent_file_names.get(hash_value, {}).get(index)

    if file_name:
        return file_name

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            get_resp = await client.post(
                f"{TORRSERVER_URL}/torrents",
                json={"action": "get", "hash": hash_value},
            )
            if get_resp.status_code != 200:
                return None

            data = get_resp.json()
            file_stats = data.get("file_stats") or []
            file_name_map: dict[int, str] = {}
            for i, f in enumerate(file_stats):
                try:
                    file_index = int(f.get("id", i))
                except (TypeError, ValueError):
                    continue
                file_path = str(f.get("path", f"file_{i}")).replace("\\", "/")
                file_name_map[file_index] = file_path.rsplit("/", 1)[-1] or f"file_{file_index}"

            if file_name_map:
                async with _torrents_lock:
                    _torrent_file_names[hash_value] = file_name_map
                return file_name_map.get(index)
    except Exception:
        logger.debug("[torrent] Failed to resolve filename for hash=%s index=%d", hash_value, index, exc_info=True)

    return None


def _build_torrserver_stream_url(hash_value: str, index: int, file_name: str) -> str:
    """Build the direct TorrServer URL for a specific file within a torrent."""
    return f"{TORRSERVER_URL}/stream/{quote(file_name)}?link={hash_value}&index={index}&play"


async def _probe_torrent_media_info(hash_value: str, index: int) -> dict:
    """Inspect a torrent file and determine whether browsers can direct-play it."""
    async with _torrents_lock:
        cached = _torrent_media_info_cache.get(hash_value, {}).get(index)
    if cached:
        return cached

    file_name = await _resolve_tracked_torrent_file_name(hash_value, index)
    if not file_name:
        raise ValueError("Unknown torrent file index")

    _, ext = os.path.splitext(file_name.lower())
    stream_url = _build_torrserver_stream_url(hash_value, index, file_name)

    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "stream=index,codec_type,codec_name",
        "-of", "json",
        stream_url,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=8.0)
    except asyncio.TimeoutError as e:
        raise RuntimeError("ffprobe timed out") from e
    except FileNotFoundError as e:
        raise RuntimeError("ffprobe is not installed in the backend image") from e

    if proc.returncode != 0:
        error_text = stderr.decode(errors="ignore").strip()
        raise RuntimeError(error_text or f"ffprobe failed with exit code {proc.returncode}")

    probe_data = json.loads(stdout.decode() or "{}")
    streams = probe_data.get("streams", [])
    video_codecs = [s.get("codec_name", "") for s in streams if s.get("codec_type") == "video"]
    audio_codecs = [s.get("codec_name", "") for s in streams if s.get("codec_type") == "audio"]

    reasons: list[str] = []
    direct_play_supported = True

    if ext not in _DIRECT_PLAY_FILE_EXTENSIONS:
        direct_play_supported = False
        reasons.append(f"container {ext or 'unknown'} is not browser-friendly")

    if video_codecs and any(codec not in _DIRECT_PLAY_VIDEO_CODECS for codec in video_codecs):
        direct_play_supported = False
        reasons.append(f"unsupported video codec(s): {', '.join(video_codecs)}")

    if audio_codecs and any(codec not in _DIRECT_PLAY_AUDIO_CODECS for codec in audio_codecs):
        direct_play_supported = False
        reasons.append(f"unsupported audio codec(s): {', '.join(audio_codecs)}")

    result = {
        "file_name": file_name,
        "extension": ext,
        "streams": streams,
        "video_codecs": video_codecs,
        "audio_codecs": audio_codecs,
        "direct_play_supported": direct_play_supported,
        "transcode_recommended": not direct_play_supported,
        "reasons": reasons,
    }
    async with _torrents_lock:
        _torrent_media_info_cache.setdefault(hash_value, {})[index] = result
    return result


async def _drain_process_stderr(stream: asyncio.StreamReader | None, buffer: bytearray) -> None:
    """Continuously read stderr from a subprocess to avoid pipe backpressure."""
    if stream is None:
        return

    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            if len(buffer) < 32768:
                remaining = 32768 - len(buffer)
                buffer.extend(chunk[:remaining])
    except Exception:
        logger.debug("[torrent] Failed while draining ffmpeg stderr", exc_info=True)


async def _cleanup_hls_session(session: dict | None) -> None:
    """Stop an active HLS transcode session and remove temporary files."""
    if not session:
        return

    proc = session.get("process")
    if proc and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            logger.debug("[torrent] Failed to stop ffmpeg HLS process", exc_info=True)

        try:
            await proc.wait()
        except Exception:
            logger.debug("[torrent] Failed waiting for ffmpeg HLS process to exit", exc_info=True)

    stderr_task = session.get("stderr_task")
    if stderr_task:
        try:
            await asyncio.wait_for(stderr_task, timeout=1.0)
        except Exception:
            stderr_task.cancel()

    output_dir = session.get("output_dir")
    if output_dir:
        shutil.rmtree(output_dir, ignore_errors=True)


async def _wait_for_hls_output(
    file_path: Path,
    session: dict,
    *,
    timeout: float = 20.0,
) -> None:
    """Wait until ffmpeg creates a non-empty HLS output file."""
    deadline = time.time() + timeout
    proc = session["process"]
    stderr_buffer = session["stderr_buffer"]

    while time.time() < deadline:
        if file_path.exists():
            try:
                if file_path.stat().st_size > 0:
                    return
            except FileNotFoundError:
                pass

        if proc.returncode not in (None, 0):
            stderr_text = stderr_buffer.decode(errors="ignore").strip()
            raise RuntimeError(stderr_text or f"ffmpeg exited with code {proc.returncode}")

        await asyncio.sleep(0.25)

    stderr_text = stderr_buffer.decode(errors="ignore").strip()
    raise RuntimeError(stderr_text or f"Timed out waiting for {file_path.name}")


async def _ensure_hls_transcode_session(hash_value: str, index: int, media_info: dict) -> dict:
    """Create or reuse an HLS transcode session for a torrent file."""
    async with _torrent_hls_session_guard:
        stale_session = None

        async with _torrents_lock:
            existing = _torrent_hls_sessions.get(hash_value, {}).get(index)
            if existing:
                playlist_path = Path(existing["playlist_path"])
                if existing["process"].returncode is None and playlist_path.exists():
                    existing["last_access"] = time.time()
                    _active_torrents[hash_value] = time.time()
                    return existing
                stale_session = _torrent_hls_sessions.get(hash_value, {}).pop(index, None)
                if not _torrent_hls_sessions.get(hash_value):
                    _torrent_hls_sessions.pop(hash_value, None)

        if stale_session:
            await _cleanup_hls_session(stale_session)

        stream_url = _build_torrserver_stream_url(hash_value, index, media_info["file_name"])
        output_dir = tempfile.mkdtemp(prefix=f"f1-torrent-hls-{hash_value[:8]}-{index}-")
        playlist_path = Path(output_dir) / "stream.m3u8"
        segment_pattern = str(Path(output_dir) / "segment_%05d.ts")

        video_codec = media_info["video_codecs"][0] if media_info["video_codecs"] else ""
        copy_video = video_codec in _HLS_COPY_VIDEO_CODECS

        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-i", stream_url,
            "-map", "0:v:0?",
            "-map", "0:a:0?",
            "-sn",
            "-dn",
            "-c:v", "copy" if copy_video else "libx264",
            "-preset", "veryfast",
            "-c:a", "aac",
            "-ac", "2",
            "-b:a", "160k",
            "-f", "hls",
            "-hls_time", "6",
            "-hls_playlist_type", "event",
            "-hls_flags", "independent_segments+append_list",
            "-hls_segment_filename", segment_pattern,
            str(playlist_path),
        ]

        logger.info(
            "[torrent] Starting HLS compatibility transcode hash=%s index=%d via ffmpeg: %s",
            hash_value, index, " ".join(shlex.quote(part) for part in ffmpeg_cmd),
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise RuntimeError("ffmpeg is not installed in the backend image") from e
        except Exception as e:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise RuntimeError(f"Failed to start HLS transcoder: {e}") from e

        stderr_buffer = bytearray()
        stderr_task = asyncio.create_task(_drain_process_stderr(proc.stderr, stderr_buffer))
        session = {
            "hash": hash_value,
            "index": index,
            "output_dir": output_dir,
            "playlist_path": str(playlist_path),
            "process": proc,
            "stderr_task": stderr_task,
            "stderr_buffer": stderr_buffer,
            "created_at": time.time(),
            "last_access": time.time(),
        }

        async with _torrents_lock:
            _torrent_hls_sessions.setdefault(hash_value, {})[index] = session
            _active_torrents[hash_value] = time.time()

        try:
            await _wait_for_hls_output(playlist_path, session, timeout=20.0)
        except Exception:
            async with _torrents_lock:
                current = _torrent_hls_sessions.get(hash_value, {}).get(index)
                if current is session:
                    _torrent_hls_sessions[hash_value].pop(index, None)
                    if not _torrent_hls_sessions.get(hash_value):
                        _torrent_hls_sessions.pop(hash_value, None)
            await _cleanup_hls_session(session)
            raise

        return session


async def _resolve_hls_output_file(hash_value: str, index: int, file_name: str) -> tuple[dict, Path]:
    """Resolve an HLS playlist or segment file for an active compatibility session."""
    media_info = await _probe_torrent_media_info(hash_value, index)
    if media_info["direct_play_supported"]:
        raise ValueError("Direct play is supported for this file")

    session = await _ensure_hls_transcode_session(hash_value, index, media_info)
    file_path = Path(session["output_dir"]) / file_name

    await _wait_for_hls_output(file_path, session, timeout=20.0)

    async with _torrents_lock:
        active = _torrent_hls_sessions.get(hash_value, {}).get(index)
        if active:
            active["last_access"] = time.time()
        _active_torrents[hash_value] = time.time()

    return session, file_path


# --- Scheduled callbacks ---


async def _scheduled_refresh() -> None:
    """Callback for APScheduler daily schedule refresh."""
    logger.info("Running scheduled schedule refresh...")
    await schedule_service.refresh()


async def _scheduled_extraction() -> None:
    """Callback for APScheduler stream extraction.

    Adjusts its own interval based on whether a session is currently live:
    - During a live session: reschedule to every 5 minutes
    - Otherwise: reschedule to every 30 minutes
    """
    logger.info("Running scheduled extraction...")
    await extraction_service.run_extraction()

    # Check if any session is currently live and adjust polling interval
    schedule_data = schedule_service.get_schedule()
    is_live = False
    for race in schedule_data.get("races", []):
        for session in race.get("sessions", []):
            if session.get("status") == "live":
                is_live = True
                break
        if is_live:
            break

    # Update the extraction job interval based on live status
    job = scheduler.get_job("stream_extraction")
    if job:
        current_interval = getattr(job.trigger, "interval_length", None)
        desired_interval = 300 if is_live else 1800  # 5 min or 30 min

        if current_interval != desired_interval:
            interval_minutes = 5 if is_live else 30
            scheduler.reschedule_job(
                "stream_extraction",
                trigger=IntervalTrigger(minutes=interval_minutes),
            )
            logger.info(
                "Extraction interval adjusted to %d minutes (live=%s)",
                interval_minutes,
                is_live,
            )


async def _scheduled_token_refresh() -> None:
    """Callback for APScheduler token refresh.

    Only performs work when there are active streams. Re-runs extractors
    to get fresh CDN tokens for streams being actively watched.
    """
    if not token_refresh_manager.has_active_streams:
        return

    logger.info("Running scheduled token refresh...")
    try:
        await token_refresh_manager.refresh_active_streams()
    except Exception:
        logger.exception("Token refresh failed (non-fatal)")


async def _scheduled_replay_scrape() -> None:
    """Callback for APScheduler replay scraping."""
    logger.info("Running scheduled replay scrape...")
    await replay_service.scrape()


async def _scheduled_torrent_idle_cleanup() -> None:
    """Remove idle torrents not accessed in 4 hours."""
    cutoff = time.time() - 4 * 3600  # 4 hours

    # Collect stale hashes under lock
    stale_hashes: list[str] = []
    hls_sessions_to_cleanup: list[dict] = []
    async with _torrents_lock:
        for h, ts in _active_torrents.items():
            if ts < cutoff:
                stale_hashes.append(h)
        for h in stale_hashes:
            hls_sessions_to_cleanup.extend(_torrent_hls_sessions.pop(h, {}).values())

    if not stale_hashes:
        return

    # Drop them from TorrServer WITHOUT holding the lock
    logger.info("[torrent] Cleaning up %d idle torrent(s)", len(stale_hashes))
    async with httpx.AsyncClient(timeout=10.0) as client:
        for h in stale_hashes:
            try:
                await client.post(
                    f"{TORRSERVER_URL}/torrents",
                    json={"action": "drop", "hash": h},
                )
            except Exception:
                logger.debug("[torrent] Failed to drop idle torrent %s", h, exc_info=True)

    # Re-acquire lock and remove dropped hashes
    async with _torrents_lock:
        for h in stale_hashes:
            _active_torrents.pop(h, None)
            _torrent_file_names.pop(h, None)
            _torrent_media_info_cache.pop(h, None)

    for session in hls_sessions_to_cleanup:
        await _cleanup_hls_session(session)


async def _scheduled_torrent_daily_cleanup() -> None:
    """Remove all torrents older than 7 days from TorrServer."""
    from datetime import datetime, timezone as tz, timedelta

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{TORRSERVER_URL}/torrents",
                json={"action": "list"},
            )
            if resp.status_code != 200:
                logger.warning("[torrent] Daily cleanup: TorrServer returned %d", resp.status_code)
                return

            torrents = resp.json()
            if not isinstance(torrents, list):
                return

            cutoff = datetime.now(tz.utc) - timedelta(days=7)
            dropped = 0
            dropped_hashes: list[str] = []
            hls_sessions_to_cleanup: list[dict] = []

            for torrent in torrents:
                ts = torrent.get("timestamp", 0)
                try:
                    if isinstance(ts, (int, float)):
                        created = datetime.fromtimestamp(ts, tz=tz.utc)
                    else:
                        created = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                except Exception:
                    continue

                if created < cutoff:
                    h = torrent.get("hash", "")
                    if h:
                        try:
                            await client.post(
                                f"{TORRSERVER_URL}/torrents",
                                json={"action": "drop", "hash": h},
                            )
                            dropped += 1
                            dropped_hashes.append(h)
                        except Exception:
                            logger.debug("[torrent] Failed to drop old torrent %s", h, exc_info=True)

            if dropped:
                async with _torrents_lock:
                    for h in dropped_hashes:
                        _active_torrents.pop(h, None)
                        _torrent_file_names.pop(h, None)
                        _torrent_media_info_cache.pop(h, None)
                        hls_sessions_to_cleanup.extend(_torrent_hls_sessions.pop(h, {}).values())
                for session in hls_sessions_to_cleanup:
                    await _cleanup_hls_session(session)
                logger.info("[torrent] Daily cleanup: dropped %d old torrent(s)", dropped)

    except Exception:
        logger.debug("[torrent] Daily cleanup failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle handler."""
    # Startup: load schedule and start background scheduler
    await schedule_service.initialize()

    # Run initial extraction
    logger.info("Running initial stream extraction...")
    await extraction_service.run_extraction()

    # Schedule daily schedule refresh
    scheduler.add_job(
        _scheduled_refresh,
        trigger=CronTrigger(hour=3, minute=0, timezone="UTC"),
        id="daily_schedule_refresh",
        name="Refresh F1 schedule daily at 03:00 UTC",
        replace_existing=True,
    )

    # Schedule periodic stream extraction (default: every 30 minutes)
    scheduler.add_job(
        _scheduled_extraction,
        trigger=IntervalTrigger(minutes=30),
        id="stream_extraction",
        name="Extract streams from all registered sites",
        replace_existing=True,
    )

    # Schedule token refresh every 4 minutes (safe margin for 5-min CDN tokens).
    # The callback is a no-op when there are no active streams.
    scheduler.add_job(
        _scheduled_token_refresh,
        trigger=IntervalTrigger(minutes=4),
        id="token_refresh",
        name="Refresh CDN tokens for active streams",
        replace_existing=True,
    )

    # Run initial replay scrape
    logger.info("Running initial replay scrape...")
    await replay_service.scrape()

    # Schedule periodic replay scraping (every 30 minutes)
    scheduler.add_job(
        _scheduled_replay_scrape,
        trigger=IntervalTrigger(minutes=30),
        id="replay_scrape",
        name="Scrape r/MotorsportsReplays for F1 replays",
        replace_existing=True,
    )

    # Schedule torrent idle cleanup every 10 minutes
    scheduler.add_job(
        _scheduled_torrent_idle_cleanup,
        trigger=IntervalTrigger(minutes=10),
        id="torrent_idle_cleanup",
        name="Clean up idle torrents from TorrServer",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Schedule torrent 7-day cleanup daily at 04:00 UTC
    scheduler.add_job(
        _scheduled_torrent_daily_cleanup,
        trigger=CronTrigger(hour=4, minute=0, timezone="UTC"),
        id="torrent_daily_cleanup",
        name="Remove torrents older than 7 days from TorrServer",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "APScheduler started - schedule refresh at 03:00 UTC, extraction every 30m, token refresh every 4m, replay scrape every 30m, torrent cleanup every 10m + daily"
    )

    yield

    # Shutdown
    scheduler.shutdown(wait=False)
    logger.info("APScheduler shut down")


app = FastAPI(title="F1 Streams", lifespan=lifespan)

# --- CORS Middleware ---
# Required for browser-based HLS players to access proxy/relay endpoints
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Range", "Content-Type"],
    expose_headers=["Content-Range", "Content-Length", "Content-Type"],
)


# --- Health & Info ---


@app.get("/health")
async def health():
    return {"status": "ok"}


# --- Schedule ---


@app.get("/schedule")
async def get_schedule():
    """Return the F1 race schedule for the current season with session statuses."""
    return schedule_service.get_schedule()


@app.post("/schedule/refresh")
async def refresh_schedule():
    """Manually trigger a schedule refresh from the jolpica API."""
    await schedule_service.refresh()
    return {"status": "refreshed"}


# --- Streams & Extraction ---


@app.get("/streams")
async def get_streams():
    """Return all currently cached streams that passed health checks.

    Streams are sorted by fallback priority:
    1. Live streams only (is_live=True)
    2. Fastest response time first (lowest response_time_ms)
    """
    streams = extraction_service.get_streams()
    return {
        "streams": streams,
        "count": len(streams),
    }


@app.get("/streams/all")
async def get_all_streams():
    """Return ALL cached streams including unhealthy ones (for debugging).

    Unlike GET /streams, this endpoint includes streams that failed health
    checks. Useful for diagnosing extraction or health check issues.
    """
    streams = extraction_service.get_all_streams_unfiltered()
    return {
        "streams": streams,
        "count": len(streams),
    }


@app.post("/streams/activate")
async def activate_stream(body: ActivateStreamRequest):
    """Mark a stream as actively being watched.

    When a stream is active, the token refresh manager will periodically
    re-run the extractor that found it to get fresh CDN tokens before
    they expire.

    If site_key is not provided, attempts to look it up from the cached
    streams.

    Body:
        {"url": "https://...", "site_key": "optional-site-key"}
    """
    url = body.url
    site_key = body.site_key

    # If site_key not provided, try to look it up from cached streams
    if not site_key:
        for streams in extraction_service._cache.values():
            for stream in streams:
                if stream.url == url:
                    site_key = stream.site_key
                    break
            if site_key:
                break

    if not site_key:
        return {
            "status": "error",
            "detail": "Could not determine site_key for this URL. Provide it explicitly.",
        }

    token_refresh_manager.mark_stream_active(url, site_key)
    return {
        "status": "activated",
        "url": url,
        "site_key": site_key,
        "active_count": len(token_refresh_manager.get_active_streams()),
    }


@app.post("/streams/deactivate")
async def deactivate_stream(body: DeactivateStreamRequest):
    """Mark a stream as no longer being watched.

    Stops the token refresh manager from refreshing CDN tokens for this stream.

    Body:
        {"url": "https://..."}
    """
    token_refresh_manager.mark_stream_inactive(body.url)
    return {
        "status": "deactivated",
        "url": body.url,
        "active_count": len(token_refresh_manager.get_active_streams()),
    }


@app.get("/streams/active")
async def get_active_streams():
    """List currently active streams with their refresh status.

    Returns all streams that are being actively watched, including
    their current (potentially refreshed) URLs and refresh counts.
    """
    active = token_refresh_manager.get_active_streams()
    return {
        "streams": active,
        "count": len(active),
    }


@app.get("/extractors")
async def get_extractors():
    """List registered extractors and their current status."""
    return extraction_service.get_status()


@app.post("/extract")
async def trigger_extraction():
    """Manually trigger an extraction run across all registered extractors."""
    await extraction_service.run_extraction()
    status = extraction_service.get_status()
    return {
        "status": "extraction_complete",
        "streams_found": status["total_cached_streams"],
        "live_streams": status["total_live_streams"],
        "extractors_run": len(status["extractors"]),
    }


# --- Replays ---


@app.get("/api/replays")
async def get_replays():
    """Return F1 replay posts grouped by race event."""
    schedule_data = schedule_service.get_schedule()
    races = schedule_data.get("races", [])
    return replay_service.get_replays_grouped(schedule_races=races)


@app.post("/api/replays/refresh")
async def refresh_replays():
    """Manually trigger a replay scrape from Reddit."""
    await replay_service.scrape()
    schedule_data = schedule_service.get_schedule()
    races = schedule_data.get("races", [])
    return replay_service.get_replays_grouped(schedule_races=races)


@app.get("/api/replays/video")
async def replay_video(
    request: Request,
    url: str = Query(..., description="Base64url-encoded video URL"),
):
    """Proxy a video file for inline playback.

    Streams the video with appropriate Content-Type headers for
    HTML5 <video> tag playback. Supports Range requests for seeking.
    """
    from backend.m3u8_rewriter import decode_url

    try:
        decoded_url = decode_url(url)
    except Exception as e:
        return Response(content=f"Invalid URL: {e}", status_code=400)

    range_header = request.headers.get("range")
    headers_to_send = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if range_header:
        headers_to_send["Range"] = range_header

    client = httpx.AsyncClient(timeout=60.0, follow_redirects=True)
    try:
        resp = await client.send(
            client.build_request("GET", decoded_url, headers=headers_to_send),
            stream=True,
        )

        response_headers = {
            "Content-Type": resp.headers.get("Content-Type", "video/mp4"),
            "Accept-Ranges": "bytes",
        }
        if "Content-Length" in resp.headers:
            response_headers["Content-Length"] = resp.headers["Content-Length"]
        if "Content-Range" in resp.headers:
            response_headers["Content-Range"] = resp.headers["Content-Range"]

        async def stream_video():
            try:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_video(),
            status_code=resp.status_code,
            headers=response_headers,
        )

    except Exception as e:
        await client.aclose()
        logger.exception("Replay video proxy error for %s", decoded_url)
        return Response(content=f"Proxy error: {e}", status_code=502)


@app.get("/api/replays/download")
async def replay_download(
    url: str = Query(..., description="Base64url-encoded video URL"),
):
    """Proxy a video file with Content-Disposition header to trigger download."""
    from backend.m3u8_rewriter import decode_url
    from urllib.parse import urlparse
    import os

    try:
        decoded_url = decode_url(url)
    except Exception as e:
        return Response(content=f"Invalid URL: {e}", status_code=400)

    # Derive filename from URL
    parsed = urlparse(decoded_url)
    filename = os.path.basename(parsed.path) or "replay.mp4"

    client = httpx.AsyncClient(timeout=60.0, follow_redirects=True)
    try:
        resp = await client.send(
            client.build_request("GET", decoded_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }),
            stream=True,
        )

        response_headers = {
            "Content-Type": resp.headers.get("Content-Type", "application/octet-stream"),
            "Content-Disposition": f'attachment; filename="{filename}"',
        }
        if "Content-Length" in resp.headers:
            response_headers["Content-Length"] = resp.headers["Content-Length"]

        async def stream_download():
            try:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_download(),
            status_code=200,
            headers=response_headers,
        )

    except Exception as e:
        await client.aclose()
        logger.exception("Replay download error for %s", decoded_url)
        return Response(content=f"Download error: {e}", status_code=502)


# --- Torrent Streaming ---


@app.get("/api/replays/torrent-status")
async def torrent_status():
    """Check if TorrServer is reachable. Result cached for 30s."""
    global _torrent_status_cache
    now = time.time()
    cached_available, cached_ts = _torrent_status_cache
    if now - cached_ts < 30:
        return {"available": cached_available}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{TORRSERVER_URL}/echo")
            available = resp.status_code == 200
    except Exception:
        available = False

    _torrent_status_cache = (available, now)
    return {"available": available}


@app.post("/api/replays/torrent-files")
async def torrent_files(body: TorrentFilesRequest):
    """Add a magnet to TorrServer and return the file listing."""
    magnet = body.magnet
    if not magnet.startswith("magnet:?xt=urn:btih:"):
        return Response(content="Invalid magnet URI", status_code=400)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Step 1: Add the magnet to TorrServer
            add_resp = await client.post(
                f"{TORRSERVER_URL}/torrents",
                json={"action": "add", "link": magnet, "title": "", "save_to_db": False},
            )
            if add_resp.status_code != 200:
                return Response(content=f"TorrServer add failed: {add_resp.status_code}", status_code=502)

            torrent_data = add_resp.json()
            torrent_hash = torrent_data.get("hash", "")
            if not torrent_hash:
                return Response(content="TorrServer returned no hash", status_code=502)

            # Track in active torrents
            async with _torrents_lock:
                _active_torrents[torrent_hash] = time.time()

            # Step 2: Poll for file_stats (metadata may take time to arrive)
            file_stats = []
            for _ in range(15):  # 15 * 2s = 30s max
                get_resp = await client.post(
                    f"{TORRSERVER_URL}/torrents",
                    json={"action": "get", "hash": torrent_hash},
                )
                if get_resp.status_code == 200:
                    data = get_resp.json()
                    file_stats = data.get("file_stats") or []
                    if file_stats:
                        break
                await asyncio.sleep(2.0)

            if not file_stats:
                return Response(content="Torrent metadata timeout - no files found after 30s", status_code=504)

            files = [
                {
                    "name": f.get("path", f"file_{i}"),
                    "length": f.get("length", 0),
                    "index": f.get("id", i),
                }
                for i, f in enumerate(file_stats)
            ]

            file_name_map: dict[int, str] = {}
            for f in files:
                try:
                    file_index = int(f["index"])
                except (TypeError, ValueError):
                    continue
                file_path = str(f["name"]).replace("\\", "/")
                file_name_map[file_index] = file_path.rsplit("/", 1)[-1] or f"file_{file_index}"

            async with _torrents_lock:
                _torrent_file_names[torrent_hash] = file_name_map

            return {"hash": torrent_hash, "files": files}

    except Exception as e:
        logger.exception("[torrent] Failed to add magnet")
        return Response(content=f"TorrServer error: {e}", status_code=502)


@app.get("/api/replays/torrent-media-info")
async def torrent_media_info(
    hash: str = Query(..., description="Torrent info hash (hex-40)"),
    index: int = Query(..., description="File index within the torrent", ge=0),
):
    """Return browser playback compatibility info for a torrent file."""
    if not _TORRENT_HASH_RE.match(hash):
        return Response(content="Invalid hash format", status_code=400)

    async with _torrents_lock:
        if hash not in _active_torrents:
            return Response(content="Unknown torrent hash", status_code=404)
        _active_torrents[hash] = time.time()

    try:
        info = await _probe_torrent_media_info(hash, index)
        return info
    except ValueError as e:
        return Response(content=str(e), status_code=404)
    except RuntimeError as e:
        logger.warning("[torrent] Media probe failed for hash=%s index=%d: %s", hash, index, e)
        return Response(content=f"Media probe failed: {e}", status_code=502)
    except Exception:
        logger.exception("[torrent] Unexpected media probe failure for hash=%s index=%d", hash, index)
        return Response(content="Unexpected media probe error", status_code=500)


@app.get("/api/replays/torrent-stream")
async def torrent_stream(
    request: Request,
    hash: str = Query(..., description="Torrent info hash (hex-40)"),
    index: int = Query(..., description="File index within the torrent", ge=0),
):
    """Stream a file from TorrServer."""
    if not _TORRENT_HASH_RE.match(hash):
        return Response(content="Invalid hash format", status_code=400)

    # Guard: hash must be tracked
    async with _torrents_lock:
        if hash not in _active_torrents:
            return Response(content="Unknown torrent hash", status_code=404)
        _active_torrents[hash] = time.time()
    file_name = await _resolve_tracked_torrent_file_name(hash, index)

    if not file_name:
        return Response(content="Unknown torrent file index", status_code=404)

    # Proxy the stream from TorrServer
    stream_url = _build_torrserver_stream_url(hash, index, file_name)
    range_header = request.headers.get("range")
    headers_to_send = {}
    if range_header:
        headers_to_send["Range"] = range_header

    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=120.0, read=120.0, write=30.0, pool=30.0), follow_redirects=True)
    try:
        resp = await client.send(
            client.build_request("GET", stream_url, headers=headers_to_send),
            stream=True,
        )

        response_headers = {
            "Content-Type": resp.headers.get("Content-Type", "video/mp4"),
            "Accept-Ranges": "bytes",
        }
        if "Content-Length" in resp.headers:
            response_headers["Content-Length"] = resp.headers["Content-Length"]
        if "Content-Range" in resp.headers:
            response_headers["Content-Range"] = resp.headers["Content-Range"]

        async def stream_torrent():
            try:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_torrent(),
            status_code=resp.status_code,
            headers=response_headers,
        )

    except Exception as e:
        await client.aclose()
        logger.exception("[torrent] Stream proxy error for hash=%s index=%d", hash, index)
        return Response(content=f"Stream error: {e}", status_code=502)


@app.get("/api/replays/torrent-stream-transcode")
async def torrent_stream_transcode(
    request: Request,
    hash: str = Query(..., description="Torrent info hash (hex-40)"),
    index: int = Query(..., description="File index within the torrent", ge=0),
):
    """Stream a torrent file with browser-safe MP4/AAC transcoding when needed."""
    if not _TORRENT_HASH_RE.match(hash):
        return Response(content="Invalid hash format", status_code=400)

    async with _torrents_lock:
        if hash not in _active_torrents:
            return Response(content="Unknown torrent hash", status_code=404)
        _active_torrents[hash] = time.time()

    try:
        media_info = await _probe_torrent_media_info(hash, index)
    except ValueError as e:
        return Response(content=str(e), status_code=404)
    except RuntimeError as e:
        logger.warning("[torrent] Media probe failed for transcode hash=%s index=%d: %s", hash, index, e)
        return Response(content=f"Media probe failed: {e}", status_code=502)
    except Exception:
        logger.exception("[torrent] Unexpected media probe failure for transcode hash=%s index=%d", hash, index)
        return Response(content="Unexpected media probe error", status_code=500)

    if media_info["direct_play_supported"]:
        return Response(
            content="Direct play is supported for this file; use /api/replays/torrent-stream instead",
            status_code=409,
        )

    if request.headers.get("range"):
        return Response(content="Range requests are not supported for transcoded playback", status_code=416)

    stream_url = _build_torrserver_stream_url(hash, index, media_info["file_name"])

    video_codec = media_info["video_codecs"][0] if media_info["video_codecs"] else ""
    copy_video = video_codec in _MP4_COPY_VIDEO_CODECS

    ffmpeg_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-i", stream_url,
        "-map", "0:v:0?",
        "-map", "0:a:0?",
        "-c:v", "copy" if copy_video else "libx264",
        "-c:a", "aac",
        "-ac", "2",
        "-b:a", "160k",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "pipe:1",
    ]

    logger.info(
        "[torrent] Transcoding hash=%s index=%d via ffmpeg: %s",
        hash, index, " ".join(shlex.quote(part) for part in ffmpeg_cmd),
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return Response(content="ffmpeg is not installed in the backend image", status_code=500)
    except Exception as e:
        logger.exception("[torrent] Failed to start ffmpeg transcode for hash=%s index=%d", hash, index)
        return Response(content=f"Failed to start transcoder: {e}", status_code=500)

    async def stream_transcoded():
        stderr_buffer = bytearray()
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk

            if proc.stderr:
                stderr_output = await proc.stderr.read()
                if stderr_output:
                    stderr_buffer.extend(stderr_output)
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()
            if proc.stderr and not stderr_buffer:
                try:
                    stderr_output = await proc.stderr.read()
                    if stderr_output:
                        stderr_buffer.extend(stderr_output)
                except Exception:
                    pass
            if proc.returncode not in (0, None) and stderr_buffer:
                logger.warning(
                    "[torrent] ffmpeg transcode exited with code %s for hash=%s index=%d: %s",
                    proc.returncode, hash, index, stderr_buffer.decode(errors="ignore")[:2000],
                )

    response_headers = {
        "Content-Type": "video/mp4",
        "Accept-Ranges": "none",
        "X-Transcode": "audio-aac",
    }
    return StreamingResponse(stream_transcoded(), status_code=200, headers=response_headers)


@app.get("/api/replays/torrent-stream-transcode-hls")
async def torrent_stream_transcode_hls(
    hash: str = Query(..., description="Torrent info hash (hex-40)"),
    index: int = Query(..., description="File index within the torrent", ge=0),
):
    """Start or reuse a browser-friendly HLS compatibility transcode session."""
    if not _TORRENT_HASH_RE.match(hash):
        return Response(content="Invalid hash format", status_code=400)

    async with _torrents_lock:
        if hash not in _active_torrents:
            return Response(content="Unknown torrent hash", status_code=404)
        _active_torrents[hash] = time.time()

    try:
        media_info = await _probe_torrent_media_info(hash, index)
    except ValueError as e:
        return Response(content=str(e), status_code=404)
    except RuntimeError as e:
        logger.warning("[torrent] Media probe failed for HLS transcode hash=%s index=%d: %s", hash, index, e)
        return Response(content=f"Media probe failed: {e}", status_code=502)
    except Exception:
        logger.exception("[torrent] Unexpected media probe failure for HLS transcode hash=%s index=%d", hash, index)
        return Response(content="Unexpected media probe error", status_code=500)

    if media_info["direct_play_supported"]:
        return Response(
            content="Direct play is supported for this file; use /api/replays/torrent-stream instead",
            status_code=409,
        )

    try:
        await _resolve_hls_output_file(hash, index, "stream.m3u8")
    except RuntimeError as e:
        logger.warning("[torrent] HLS transcode startup failed for hash=%s index=%d: %s", hash, index, e)
        return Response(content=f"HLS transcode failed: {e}", status_code=502)
    except Exception:
        logger.exception("[torrent] Unexpected HLS transcode failure for hash=%s index=%d", hash, index)
        return Response(content="Unexpected HLS transcode error", status_code=500)

    return {
        "playlist_url": f"/api/replays/torrent-transcode-files/{hash}/{index}/stream.m3u8",
        "segment_prefix": f"/api/replays/torrent-transcode-files/{hash}/{index}/",
        "mode": "hls-audio-aac",
    }


@app.get("/api/replays/torrent-transcode-files/{hash}/{index}/{file_name:path}")
async def torrent_transcode_files(
    hash: str,
    index: int,
    file_name: str,
):
    """Serve playlist/segment files for an active replay compatibility HLS session."""
    if not _TORRENT_HASH_RE.match(hash):
        return Response(content="Invalid hash format", status_code=400)

    if not file_name or file_name.startswith("/") or ".." in Path(file_name).parts:
        return Response(content="Invalid file path", status_code=400)

    async with _torrents_lock:
        if hash not in _active_torrents:
            return Response(content="Unknown torrent hash", status_code=404)
        _active_torrents[hash] = time.time()

    try:
        session, file_path = await _resolve_hls_output_file(hash, index, file_name)
    except ValueError as e:
        return Response(content=str(e), status_code=409)
    except RuntimeError as e:
        logger.warning(
            "[torrent] HLS file serve failed for hash=%s index=%d file=%s: %s",
            hash, index, file_name, e,
        )
        return Response(content=f"HLS file unavailable: {e}", status_code=502)
    except FileNotFoundError:
        return Response(content="Transcoded file not found", status_code=404)
    except Exception:
        logger.exception(
            "[torrent] Unexpected HLS file serve failure for hash=%s index=%d file=%s",
            hash, index, file_name,
        )
        return Response(content="Unexpected HLS file serve error", status_code=500)

    if not file_path.exists():
        return Response(content="Transcoded file not found", status_code=404)

    async with _torrents_lock:
        active = _torrent_hls_sessions.get(hash, {}).get(index)
        if active:
            active["last_access"] = time.time()
        _active_torrents[hash] = time.time()

    media_type = "application/vnd.apple.mpegurl" if file_path.suffix == ".m3u8" else "video/mp2t"
    headers = {
        "Cache-Control": "no-store",
        "X-Transcode": "audio-aac-hls",
    }

    if file_path.suffix == ".m3u8":
        # Ensure file exists before responding so the first manifest load does not race.
        await _wait_for_hls_output(Path(session["playlist_path"]), session, timeout=5.0)

    return FileResponse(file_path, media_type=media_type, headers=headers)


@app.post("/api/replays/torrent-stop")
async def torrent_stop(body: TorrentStopRequest):
    """Stop a torrent and remove it from TorrServer."""
    h = body.hash
    if not _TORRENT_HASH_RE.match(h):
        return Response(content="Invalid hash format", status_code=400)

    sessions_to_cleanup: list[dict] = []
    async with _torrents_lock:
        if h not in _active_torrents:
            return Response(content="Unknown torrent hash", status_code=404)
        del _active_torrents[h]
        _torrent_file_names.pop(h, None)
        _torrent_media_info_cache.pop(h, None)
        sessions_to_cleanup.extend(_torrent_hls_sessions.pop(h, {}).values())

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{TORRSERVER_URL}/torrents",
                json={"action": "drop", "hash": h},
            )
    except Exception:
        logger.debug("[torrent] Failed to drop torrent %s from TorrServer", h, exc_info=True)

    for session in sessions_to_cleanup:
        await _cleanup_hls_session(session)

    return {"status": "stopped", "hash": h}


@app.post("/api/replays/torrent-heartbeat")
async def torrent_heartbeat(body: TorrentHeartbeatRequest):
    """Keep a torrent stream alive by updating its last-access timestamp."""
    h = body.hash
    if not _TORRENT_HASH_RE.match(h):
        return Response(content="Invalid hash format", status_code=400)

    async with _torrents_lock:
        if h not in _active_torrents:
            return Response(content="Unknown torrent hash", status_code=404)
        _active_torrents[h] = time.time()

    return {"ok": True}


# --- HLS Proxy ---


def _get_proxy_base(request: Request) -> str:
    """Derive the proxy base URL from the incoming request.

    Uses X-Forwarded-Proto and X-Forwarded-Host headers if present
    (behind a reverse proxy), otherwise falls back to request URL.
    """
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    return f"{proto}://{host}"


@app.get("/proxy")
async def proxy_endpoint(
    request: Request,
    url: str = Query(..., description="Base64url-encoded m3u8 playlist URL"),
    quality: int | None = Query(
        None,
        description="0-based quality variant index (0=highest bandwidth). "
        "Only applies to master playlists.",
    ),
):
    """Proxy an upstream m3u8 playlist with URI rewriting.

    Fetches the upstream m3u8 playlist, rewrites all URIs to route through
    our /proxy (for sub-playlists) and /relay (for segments) endpoints,
    and returns the rewritten playlist.

    The `url` parameter must be base64url-encoded to avoid URL encoding issues.

    If `quality` is specified and the upstream is a master playlist (with
    multiple quality variants), the proxy will fetch the selected variant's
    media playlist directly instead of returning the master playlist.
    Quality index 0 = highest bandwidth, 1 = second highest, etc.

    Examples:
        GET /proxy?url=aHR0cHM6Ly9leGFtcGxlLmNvbS9zdHJlYW0ubTN1OA
        GET /proxy?url=aHR0cHM6Ly9leGFtcGxlLmNvbS9zdHJlYW0ubTN1OA&quality=0
    """
    # Check if we have a fresher URL from token refresh
    fresh_url = token_refresh_manager.get_fresh_url(url)
    if fresh_url != url:
        logger.info("Using refreshed URL from token manager")

    proxy_base = _get_proxy_base(request)
    rewritten = await proxy_playlist(fresh_url, proxy_base, quality=quality)

    return Response(
        content=rewritten,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@app.get("/relay")
async def relay_endpoint(
    request: Request,
    url: str = Query(..., description="Base64url-encoded segment URL"),
):
    """Relay an upstream media segment as a chunked byte stream.

    Fetches the upstream segment (TS, fMP4, init segment, etc.) and streams
    it to the client using chunked transfer encoding. Never buffers the
    full segment in memory.

    The `url` parameter must be base64url-encoded to avoid URL encoding issues.

    Supports HTTP Range requests for seeking.

    Example:
        GET /relay?url=aHR0cHM6Ly9leGFtcGxlLmNvbS9zZWdtZW50LnRz
    """
    range_header = request.headers.get("range")

    stream_gen, headers, status_code = await relay_stream(url, range_header)

    return StreamingResponse(
        stream_gen,
        status_code=status_code,
        headers=headers,
    )


# --- Frontend Static Files ---
# Mount the SvelteKit static build AFTER all API routes so API endpoints take priority.
# SvelteKit adapter-static with ssr=false produces {page}.html files and a fallback index.html.
import re as _re

# Ad script patterns to strip from embed pages
_AD_PATTERNS = [
    _re.compile(r'<script[^>]*>.*?aclib\.runPop.*?</script>', _re.DOTALL | _re.IGNORECASE),
    _re.compile(r'<script[^>]*src=["\'][^"\']*adsco\.re[^"\']*["\'][^>]*></script>', _re.IGNORECASE),
    _re.compile(r'<script[^>]*>.*?runPop.*?</script>', _re.DOTALL | _re.IGNORECASE),
    _re.compile(r'<script[^>]*>.*?popunder.*?</script>', _re.DOTALL | _re.IGNORECASE),
    # Remove the hidden ad iframe loader
    _re.compile(r"\(.*?insertAdjacentHTML.*?ad\.html.*?\)\(\);", _re.DOTALL),
]


@app.get("/embed-proxy")
async def embed_proxy(
    url: str = Query(..., description="Base64url-encoded embed URL"),
):
    """Proxy an embed page, stripping ad/popup scripts.

    Fetches the embed page, removes ad scripts (aclib.runPop, popunder,
    adsco.re), and serves the cleaned HTML. This allows iframe embedding
    without popups or redirects.
    """
    import httpx
    from backend.m3u8_rewriter import decode_url

    try:
        decoded_url = decode_url(url)
    except Exception as e:
        return Response(content=f"Invalid URL: {e}", status_code=400)

    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://streamed.pk/",
            },
        ) as client:
            resp = await client.get(decoded_url)
            if resp.status_code != 200:
                return Response(content=f"Upstream returned {resp.status_code}", status_code=502)

            html = resp.text

            # Strip ad scripts
            for pattern in _AD_PATTERNS:
                html = pattern.sub('', html)

            return HTMLResponse(content=html)

    except Exception as e:
        logger.exception("Embed proxy error for %s", decoded_url)
        return Response(content=f"Proxy error: {e}", status_code=502)


# Starlette StaticFiles(html=True) only checks {path}/index.html, not {path}.html.
# We use a catch-all route to handle both patterns and the SPA fallback.
_frontend_dir = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "frontend", "build"))
if os.path.exists(_frontend_dir):
    from starlette.responses import FileResponse, HTMLResponse

    _fallback_path = os.path.join(_frontend_dir, "index.html")

    @app.get("/{path:path}")
    async def serve_frontend(path: str):
        """Serve SvelteKit frontend files with SPA fallback."""
        for candidate in [
            os.path.join(_frontend_dir, path),
            os.path.join(_frontend_dir, f"{path}.html"),
            os.path.join(_frontend_dir, path, "index.html"),
        ]:
            real = os.path.realpath(candidate)
            if real.startswith(_frontend_dir) and os.path.isfile(real):
                return FileResponse(real)
        # SPA fallback for client-side routing
        if os.path.isfile(_fallback_path):
            return FileResponse(_fallback_path)
        return Response(content="Not Found", status_code=404)

    logger.info("Serving frontend from %s", _frontend_dir)
else:
    # Fallback root when no frontend build exists
    @app.get("/")
    async def root():
        return {"service": "f1-streams", "version": "5.0.0"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
