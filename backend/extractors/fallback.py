"""Fallback extractor - static iframe sources from the original index.html.

These are known-good F1 streaming aggregator sites that can be embedded
in iframes. They serve as fallbacks when other extractors fail to find
streams or when the extracted streams don't play.
"""

import logging

from backend.extractors.base import BaseExtractor
from backend.extractors.models import ExtractedStream

logger = logging.getLogger(__name__)

# Static iframe sources from the original index.html
# These are aggregator sites that list F1 streams
FALLBACK_SOURCES = [
    {
        "url": "https://wearechecking.live/streams-pages/motorsports",
        "title": "WeAreChecking - Motorsports",
        "quality": "",
    },
    {
        "url": "https://vipleague.im/formula-1-schedule-streaming-links",
        "title": "VIPLeague - Formula 1",
        "quality": "",
    },
    {
        "url": "https://www.vipbox.lc/",
        "title": "VIPBox",
        "quality": "",
    },
    {
        "url": "https://f1box.me/",
        "title": "F1Box",
        "quality": "",
    },
    {
        "url": "https://1stream.vip/formula-1-streams/",
        "title": "1Stream - Formula 1",
        "quality": "",
    },
    {
        "url": "https://aceztrims.pages.dev/f1/",
        "title": "Aceztrims - F1",
        "quality": "",
    },
    {
        "url": "https://thetvapp.to/",
        "title": "TheTVApp",
        "quality": "",
    },
]


class FallbackExtractor(BaseExtractor):
    """Returns static iframe sources as fallback streams.

    These are aggregator sites from the original index.html that can
    be embedded in iframes with sandbox restrictions.
    """

    @property
    def site_key(self) -> str:
        return "fallback"

    @property
    def site_name(self) -> str:
        return "Fallback Sources"

    async def extract(self) -> list[ExtractedStream]:
        """Return static fallback iframe sources."""
        streams = []
        for source in FALLBACK_SOURCES:
            streams.append(
                ExtractedStream(
                    url=source["url"],
                    site_key=self.site_key,
                    site_name=self.site_name,
                    quality=source["quality"],
                    title=source["title"],
                    stream_type="embed",
                    embed_url=source["url"],
                )
            )

        logger.info("[fallback] Returning %d fallback source(s)", len(streams))
        return streams
