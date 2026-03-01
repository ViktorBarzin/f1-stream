"""Discord extractor - monitors Discord channels for F1 stream links.

Reads recent messages from configured Discord channels using a user token,
extracts URLs that look like stream links, and returns them as embed streams.
"""

import logging
import os
import re

import httpx

from backend.extractors.base import BaseExtractor
from backend.extractors.models import ExtractedStream

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v9"
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
# Comma-separated channel IDs to monitor
DISCORD_CHANNELS = os.getenv("DISCORD_CHANNELS", "").split(",")
# How many messages to fetch per channel
MESSAGE_LIMIT = 50

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# URL pattern to match stream links (exclude Discord CDN, images, etc.)
URL_PATTERN = re.compile(r"https?://[^\s<>\)\]\"']+", re.IGNORECASE)

# Domains to exclude (not stream links)
EXCLUDED_DOMAINS = {
    "discord.com", "discord.gg", "cdn.discordapp.com",
    "tenor.com", "giphy.com", "imgur.com",
    "youtube.com", "youtu.be", "twitter.com", "x.com",
    "reddit.com", "instagram.com", "tiktok.com",
    "fmhy.net", "github.com", "freemotorsports.com",
}

# Keywords that suggest a URL is a stream link
STREAM_KEYWORDS = {
    "stream", "live", "watch", "f1", "formula", "race", "sport",
    "motorsport", "grand prix", "gp", "sky", "ppv", "embed",
    "m3u8", "player", ".php", "channel",
}


def _is_stream_url(url: str) -> bool:
    """Check if a URL looks like a stream link."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        # Skip excluded domains
        for excluded in EXCLUDED_DOMAINS:
            if excluded in domain:
                return False
        # Skip image/media file links
        path = parsed.path.lower()
        if any(path.endswith(ext) for ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.mp4', '.webm', '.svg', '.css', '.js')):
            return False
        return True
    except Exception:
        return False


class DiscordExtractor(BaseExtractor):
    """Extracts stream links from Discord channel messages.

    Monitors configured Discord channels for URLs shared by users,
    filters to likely stream links, and returns them as embed streams.
    """

    @property
    def site_key(self) -> str:
        return "discord"

    @property
    def site_name(self) -> str:
        return "Discord Community"

    async def extract(self) -> list[ExtractedStream]:
        """Fetch recent messages from Discord channels and extract URLs."""
        if not DISCORD_TOKEN:
            logger.info("[discord] No DISCORD_TOKEN set, skipping")
            return []

        channels = [c.strip() for c in DISCORD_CHANNELS if c.strip()]
        if not channels:
            logger.info("[discord] No DISCORD_CHANNELS configured, skipping")
            return []

        streams: list[ExtractedStream] = []
        seen_urls: set[str] = set()

        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True,
                headers={
                    "Authorization": DISCORD_TOKEN,
                    "User-Agent": USER_AGENT,
                },
            ) as client:
                for channel_id in channels:
                    try:
                        channel_streams = await self._fetch_channel(
                            client, channel_id, seen_urls
                        )
                        streams.extend(channel_streams)
                    except Exception:
                        logger.debug(
                            "[discord] Failed to fetch channel %s",
                            channel_id,
                            exc_info=True,
                        )
        except Exception:
            logger.exception("[discord] Failed to connect to Discord API")

        logger.info("[discord] Extracted %d stream(s) from %d channel(s)", len(streams), len(channels))
        return streams

    async def _fetch_channel(
        self,
        client: httpx.AsyncClient,
        channel_id: str,
        seen_urls: set[str],
    ) -> list[ExtractedStream]:
        """Fetch messages from a single channel and extract stream URLs."""
        resp = await client.get(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            params={"limit": MESSAGE_LIMIT},
        )
        if resp.status_code != 200:
            logger.warning(
                "[discord] Channel %s returned HTTP %d", channel_id, resp.status_code
            )
            return []

        messages = resp.json()
        if not isinstance(messages, list):
            return []

        streams: list[ExtractedStream] = []

        for msg in messages:
            content = msg.get("content", "")
            author = msg.get("author", {}).get("username", "unknown")

            # Extract URLs from message content
            urls = URL_PATTERN.findall(content)

            # Also check embeds
            for embed in msg.get("embeds", []):
                if embed.get("url"):
                    urls.append(embed["url"])

            for url in urls:
                # Clean trailing punctuation
                url = url.rstrip(".,;:!?)")

                if url in seen_urls:
                    continue
                if not _is_stream_url(url):
                    continue

                seen_urls.add(url)
                streams.append(
                    ExtractedStream(
                        url=url,
                        site_key=self.site_key,
                        site_name=self.site_name,
                        quality="",
                        title=f"Shared by {author}",
                        stream_type="embed",
                        embed_url=url,
                    )
                )

        return streams
