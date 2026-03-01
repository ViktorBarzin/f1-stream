"""Streamed.pk extractor - fetches F1 streams via public JSON API.

Constructs direct m3u8 URLs from the API data using the known CDN pattern:
  https://rr.vipstreams.in/{source}/js/{id}/{streamNo}/playlist.m3u8
These require Referer: https://embedme.top/ which the HLS proxy handles.
"""

import logging

import httpx

from backend.extractors.base import BaseExtractor
from backend.extractors.models import ExtractedStream

logger = logging.getLogger(__name__)

BASE_URL = "https://streamed.su"
M3U8_CDN = "https://rr.vipstreams.in"
REQUIRED_REFERER = "https://embedme.top/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Only include events matching these keywords (case-insensitive)
F1_KEYWORDS = {"formula 1", "formula one", "f1", "sky sports f1"}
# Grand Prix is shared with MotoGP/IndyCar — only match if no other series keywords
GP_KEYWORD = "grand prix"
NON_F1_KEYWORDS = {"motogp", "moto gp", "moto2", "moto3", "motoe", "indycar",
                    "indy car", "firestone", "nascar", "rally", "wrc", "wec",
                    "lemans", "le mans", "superbike", "dtm", "supercars"}


def _is_f1_event(title: str) -> bool:
    """Check if an event title is Formula 1 related."""
    lower = title.lower()
    if any(kw in lower for kw in F1_KEYWORDS):
        return True
    if GP_KEYWORD in lower and not any(kw in lower for kw in NON_F1_KEYWORDS):
        return True
    return False


class StreamedExtractor(BaseExtractor):
    """Extracts direct m3u8 streams from Streamed.pk's public JSON API.

    Uses the API to discover F1 events and constructs direct m3u8 URLs
    using the known CDN pattern instead of embed URLs.
    """

    @property
    def site_key(self) -> str:
        return "streamed"

    @property
    def site_name(self) -> str:
        return "Streamed"

    async def extract(self) -> list[ExtractedStream]:
        """Fetch F1 events and construct direct m3u8 URLs."""
        streams: list[ExtractedStream] = []

        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            ) as client:
                resp = await client.get(f"{BASE_URL}/api/matches/motor-sports")
                if resp.status_code != 200:
                    logger.warning(
                        "[streamed] Events API returned HTTP %d", resp.status_code
                    )
                    return []

                events = resp.json()
                if not isinstance(events, list):
                    logger.warning("[streamed] Unexpected events response type")
                    return []

                logger.info("[streamed] Found %d motorsport event(s)", len(events))

                for event in events:
                    title = event.get("title", "Unknown Event")
                    if not _is_f1_event(title):
                        continue
                    sources = event.get("sources", [])
                    if not sources:
                        continue

                    for source_info in sources:
                        source_name = source_info.get("source", "")
                        source_id = source_info.get("id", "")
                        if not source_name or not source_id:
                            continue

                        try:
                            stream_resp = await client.get(
                                f"{BASE_URL}/api/stream/{source_name}/{source_id}"
                            )
                            if stream_resp.status_code != 200:
                                continue

                            stream_data = stream_resp.json()
                            if not isinstance(stream_data, list):
                                stream_data = [stream_data]

                            for item in stream_data:
                                language = item.get("language", "")
                                hd = item.get("hd", False)
                                stream_no = item.get("streamNo", 1)

                                # Construct direct m3u8 URL
                                m3u8_url = (
                                    f"{M3U8_CDN}/{source_name}/js/"
                                    f"{source_id}/{stream_no}/playlist.m3u8"
                                )

                                quality = "HD" if hd else "SD"
                                stream_title = f"{title}"
                                if language:
                                    stream_title += f" ({language})"
                                if stream_no > 1:
                                    stream_title += f" #{stream_no}"

                                streams.append(
                                    ExtractedStream(
                                        url=m3u8_url,
                                        site_key=self.site_key,
                                        site_name=self.site_name,
                                        quality=quality,
                                        title=stream_title,
                                        stream_type="m3u8",
                                    )
                                )
                        except Exception:
                            logger.debug(
                                "[streamed] Failed to fetch stream for %s/%s",
                                source_name,
                                source_id,
                                exc_info=True,
                            )

        except Exception:
            logger.exception("[streamed] Failed to fetch events")

        logger.info("[streamed] Extracted %d stream(s)", len(streams))
        return streams
