"""Replay service — scrapes r/MotorsportsReplays for F1 replay links."""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger(__name__)

REDDIT_URL = "https://www.reddit.com/r/MotorsportsReplays/new.json"
USER_AGENT = "f1-stream-replay-scraper/1.0"
MAX_AGE_DAYS = 7

# Flair-based filtering (primary)
F1_FLAIRS = {"formula 1", "f1"}

# Keyword-based filtering (fallback for unflaired posts)
F1_KEYWORDS = {"formula 1", "formula one", "f1", "grand prix"}
_NON_F1_RE = re.compile(
    r"\b(?:motogp|moto\s?gp|moto2|moto3|motoe|indycar|indy\s?car|indy\s?nxt|nascar|"
    r"rally|wrc|wec|lemans|le\s+mans|superbike|dtm|supercars|"
    r"formula\s+e|formula\s+2|formula\s+3|f2|f3|fe)\b",
    re.IGNORECASE,
)

# Session type detection from post titles (more specific patterns first)
SESSION_PATTERNS = [
    (re.compile(r"\b(?:sprint qualifying|sprint shootout|sq)\b", re.IGNORECASE), "Sprint Qualifying"),
    (re.compile(r"\b(?:sprint race|sprint)\b", re.IGNORECASE), "Sprint"),
    (re.compile(r"\b(?:race|carrera)\b", re.IGNORECASE), "Race"),
    (re.compile(r"\b(?:qualifying|quali|qualy|q1|q2|q3|clasificaci[oó]n)\b", re.IGNORECASE), "Qualifying"),
    (re.compile(r"\b(?:free practice|practice|fp1|fp2|fp3)\b", re.IGNORECASE), "Practice"),
    (re.compile(r"\b(?:pre-race|pre race|build.?up)\b", re.IGNORECASE), "Pre-Race"),
    (re.compile(r"\b(?:full\s+weekend|complete\s+weekend|full\s+event|full\s+race\s+weekend)\b", re.IGNORECASE), "Full Event"),
]

# Known GP names for event extraction (requires at least one word before GP/Grand Prix)
GP_NAME_PATTERN = re.compile(
    r"(?:(?:20\d{2})\s+)?(?:F1\s+)?"
    r"([\w]+(?:\s+[\w]+)*?\s+(?:Grand Prix|GP))",
    re.IGNORECASE,
)

# Fallback: known F1 race locations for when GP_NAME_PATTERN fails
_LOCATION_PATTERN = re.compile(
    r"\b(Austral(?:ia|ian)|Bahrain|Saudi|Jeddah|Japan(?:ese)?|Chinese|China|Shanghai|Suzuka|"
    r"Miami|Imola|Monaco|Spain|Spanish|Barcelona|Canad(?:a|ian)|Montreal|Austria[n]?|Spielberg|"
    r"British|Silverstone|Hungar(?:y|ian)|Budapest|Belgian?|Spa|Dutch|Netherlands|Zandvoort|"
    r"Italy|Italian|Monza|Singapore(?:an)?|Azerbaijan|Baku|United States|Austin|COTA|"
    r"Mexic(?:o|an)|Brazil(?:ian)?|Interlagos|Las Vegas|Qatar|Abu Dhabi|Yas Marina|"
    r"Emilia Romagna|S[aã]o Paulo)\b",
    re.IGNORECASE,
)

_LOCATION_TO_GP: dict[str, str] = {
    "australia": "Australian Grand Prix", "australian": "Australian Grand Prix",
    "bahrain": "Bahrain Grand Prix",
    "saudi": "Saudi Arabian Grand Prix", "jeddah": "Saudi Arabian Grand Prix",
    "japan": "Japanese Grand Prix", "japanese": "Japanese Grand Prix", "suzuka": "Japanese Grand Prix",
    "chinese": "Chinese Grand Prix", "china": "Chinese Grand Prix", "shanghai": "Chinese Grand Prix",
    "miami": "Miami Grand Prix",
    "imola": "Emilia Romagna Grand Prix", "emilia romagna": "Emilia Romagna Grand Prix",
    "monaco": "Monaco Grand Prix",
    "spain": "Spanish Grand Prix", "spanish": "Spanish Grand Prix", "barcelona": "Spanish Grand Prix",
    "canada": "Canadian Grand Prix", "canadian": "Canadian Grand Prix", "montreal": "Canadian Grand Prix",
    "austria": "Austrian Grand Prix", "austrian": "Austrian Grand Prix", "spielberg": "Austrian Grand Prix",
    "british": "British Grand Prix", "silverstone": "British Grand Prix",
    "hungary": "Hungarian Grand Prix", "hungarian": "Hungarian Grand Prix", "budapest": "Hungarian Grand Prix",
    "belgium": "Belgian Grand Prix", "belgian": "Belgian Grand Prix", "spa": "Belgian Grand Prix",
    "dutch": "Dutch Grand Prix", "netherlands": "Dutch Grand Prix", "zandvoort": "Dutch Grand Prix",
    "italy": "Italian Grand Prix", "italian": "Italian Grand Prix", "monza": "Italian Grand Prix",
    "singapore": "Singapore Grand Prix", "singaporean": "Singapore Grand Prix",
    "azerbaijan": "Azerbaijan Grand Prix", "baku": "Azerbaijan Grand Prix",
    "united states": "United States Grand Prix", "austin": "United States Grand Prix", "cota": "United States Grand Prix",
    "mexico": "Mexican Grand Prix", "mexican": "Mexican Grand Prix",
    "brazil": "São Paulo Grand Prix", "brazilian": "São Paulo Grand Prix",
    "interlagos": "São Paulo Grand Prix", "são paulo": "São Paulo Grand Prix", "sao paulo": "São Paulo Grand Prix",
    "las vegas": "Las Vegas Grand Prix",
    "qatar": "Qatar Grand Prix",
    "abu dhabi": "Abu Dhabi Grand Prix", "yas marina": "Abu Dhabi Grand Prix",
}

# Link type detection
VIDEO_DOMAINS = {"streamable.com", "streamja.com", "streamff.com", "streamgg.com", "pixeldrain.com"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".avi"}
EMBED_DOMAINS = {"rerace.io", "f1full.com", "f1fullraces.com"}

# URL extraction from selftext markdown
URL_PATTERN = re.compile(r"https?://[^\s\)\]>\"]+")

# Magnet URI extraction (excludes ) to avoid capturing markdown link parens)
MAGNET_PATTERN = re.compile(r"magnet:\?xt=urn:btih:[^\s\"<>\)\]]+")
# Extract btih hash for dedup (hex-40 or base32 formats)
BTIH_HASH_RE = re.compile(r"xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", re.IGNORECASE)


@dataclass
class ReplayLink:
    url: str
    link_type: str  # "video", "embed", "external", "magnet"
    video_url: str | None = None  # resolved direct video URL for "video" type
    label: str = ""


@dataclass
class ReplayPost:
    title: str
    reddit_url: str
    created_utc: float
    flair: str | None = None
    event_name: str | None = None
    session_type: str | None = None
    links: list[ReplayLink] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "reddit_url": self.reddit_url,
            "created_utc": self.created_utc,
            "flair": self.flair,
            "event_name": self.event_name,
            "session_type": self.session_type,
            "links": [
                {"url": l.url, "link_type": l.link_type, "video_url": l.video_url, "label": l.label}
                for l in self.links
            ],
        }


def _is_f1_post(title: str, flair: str | None) -> bool:
    """Check if a Reddit post is F1-related via flair or title keywords."""
    lower_title = title.lower()

    # Reject if it contains non-F1 motorsport keywords (word-boundary regex)
    if _NON_F1_RE.search(title):
        return False

    # Flair-based (primary)
    if flair and flair.lower().strip() in F1_FLAIRS:
        return True

    # Keyword-based (fallback)
    if any(kw in lower_title for kw in F1_KEYWORDS):
        return True

    return False


def _extract_session_type(title: str) -> str:
    """Extract the session type from a post title."""
    matches = [st for pattern, st in SESSION_PATTERNS if pattern.search(title)]
    # Multiple distinct session types → full event compilation
    if len(set(matches)) > 1:
        return "Full Event"
    if matches:
        return matches[0]
    return "Full Event"


def _extract_event_name(title: str) -> str | None:
    """Extract the Grand Prix name from a post title."""
    match = GP_NAME_PATTERN.search(title)
    if match:
        name = match.group(1).strip()
        # Normalize: ensure it ends with "Grand Prix" not just "GP"
        if name.lower().endswith(" gp"):
            name = name[:-3] + " Grand Prix"
        return name

    # Fallback: look for known location keywords in the title
    loc_match = _LOCATION_PATTERN.search(title)
    if loc_match:
        key = loc_match.group(1).lower()
        if key in _LOCATION_TO_GP:
            return _LOCATION_TO_GP[key]

    return None


def _classify_link(url: str) -> str:
    """Classify a URL as video, embed, or external."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    domain = parsed.netloc.lower().removeprefix("www.")

    if domain in VIDEO_DOMAINS:
        return "video"

    # Check file extension
    path_lower = parsed.path.lower()
    for ext in VIDEO_EXTENSIONS:
        if path_lower.endswith(ext):
            return "video"

    if domain in EMBED_DOMAINS:
        return "embed"

    return "external"


def _make_label(url: str) -> str:
    """Generate a human-readable label from a URL."""
    from urllib.parse import urlparse, parse_qs, unquote_plus

    # Handle magnet URIs
    if url.startswith("magnet:"):
        # Try to extract display name from &dn= parameter
        try:
            # magnet URIs use ? as separator, parse_qs can handle the query part
            query_start = url.find("?")
            if query_start >= 0:
                params = parse_qs(url[query_start + 1:])
                dn = params.get("dn", [None])[0]
                if dn:
                    decoded = unquote_plus(dn)
                    # Truncate long names
                    if len(decoded) > 60:
                        return decoded[:57] + "..."
                    return decoded
        except Exception:
            pass
        return "Magnet"

    parsed = urlparse(url)
    domain = parsed.netloc.lower().removeprefix("www.")

    # Known domain labels
    labels = {
        "streamable.com": "Streamable",
        "streamja.com": "Streamja",
        "streamff.com": "StreamFF",
        "streamgg.com": "StreamGG",
        "rerace.io": "ReRace",
        "f1full.com": "F1Full",
        "f1fullraces.com": "F1FullRaces",
        "pixeldrain.com": "Pixeldrain",
        "mega.nz": "Mega",
        "drive.google.com": "Google Drive",
    }
    if domain in labels:
        return labels[domain]

    # Use domain name, capitalize
    return domain.split(".")[0].capitalize()


def _extract_links_from_post(post_data: dict) -> list[ReplayLink]:
    """Extract all links from a Reddit post (url field + selftext)."""
    links: list[ReplayLink] = []
    seen_urls: set[str] = set()

    # Main post URL (if it's not a self-post)
    post_url = post_data.get("url", "")
    if post_url and "reddit.com" not in post_url and "redd.it" not in post_url:
        link_type = _classify_link(post_url)
        links.append(ReplayLink(
            url=post_url,
            link_type=link_type,
            label=_make_label(post_url),
        ))
        seen_urls.add(post_url)

    # Extract URLs from selftext (markdown body)
    selftext = post_data.get("selftext", "")
    if selftext:
        for match in URL_PATTERN.finditer(selftext):
            url = match.group(0).rstrip(".,;:!?)")
            if url in seen_urls:
                continue
            if "reddit.com" in url or "redd.it" in url:
                continue
            seen_urls.add(url)
            link_type = _classify_link(url)
            links.append(ReplayLink(
                url=url,
                link_type=link_type,
                label=_make_label(url),
            ))

        # Extract magnet URIs from selftext
        for match in MAGNET_PATTERN.finditer(selftext):
            magnet_uri = match.group(0).rstrip(".,;:!?)")
            # Dedup by btih hash, not full URI (same torrent may appear with different trackers)
            btih_match = BTIH_HASH_RE.search(magnet_uri)
            if btih_match:
                btih_key = f"btih:{btih_match.group(1).lower()}"
                if btih_key in seen_urls:
                    continue
                seen_urls.add(btih_key)
            elif magnet_uri in seen_urls:
                continue
            else:
                seen_urls.add(magnet_uri)

            links.append(ReplayLink(
                url=magnet_uri,
                link_type="magnet",
                label=_make_label(magnet_uri),
            ))

    return links


async def _resolve_streamable_url(url: str, client: httpx.AsyncClient) -> str | None:
    """Extract the direct video URL from a Streamable page."""
    try:
        # Streamable's API: https://api.streamable.com/videos/{shortcode}
        from urllib.parse import urlparse
        parsed = urlparse(url)
        shortcode = parsed.path.strip("/")
        if not shortcode:
            return None

        resp = await client.get(f"https://api.streamable.com/videos/{shortcode}")
        if resp.status_code != 200:
            return None

        data = resp.json()
        files = data.get("files", {})
        # Prefer mp4 quality order: mp4-mobile for smaller, mp4 for full
        for key in ("mp4", "mp4-mobile"):
            if key in files and files[key].get("url"):
                video_url = files[key]["url"]
                if video_url.startswith("//"):
                    video_url = "https:" + video_url
                return video_url
        return None
    except Exception:
        logger.debug("Failed to resolve Streamable URL: %s", url, exc_info=True)
        return None


async def _resolve_pixeldrain_url(url: str, client: httpx.AsyncClient) -> str | None:
    """Extract the direct download URL from a Pixeldrain link."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")

        if len(path_parts) == 2 and path_parts[0] == "u":
            file_id = path_parts[1]
            resp = await client.head(
                f"https://pixeldrain.com/api/file/{file_id}",
                follow_redirects=True,
            )
            if resp.status_code == 200:
                return f"https://pixeldrain.com/api/file/{file_id}"
        return None
    except Exception:
        logger.debug("Failed to resolve Pixeldrain URL: %s", url, exc_info=True)
        return None


async def _fetch_post_comments(permalink: str, client: httpx.AsyncClient) -> list[ReplayLink]:
    """Extract links from Reddit post comments (top-level + 1 level of replies)."""
    try:
        url = f"https://www.reddit.com{permalink}.json?raw_json=1&limit=200"
        resp = await client.get(url)
        if resp.status_code == 429:
            logger.warning("[replays] Reddit rate limited during comment fetch")
            raise RateLimitError()
        if resp.status_code != 200:
            logger.debug("[replays] Comment fetch returned %d for %s", resp.status_code, permalink)
            return []

        data = resp.json()
        # Reddit returns [post_listing, comments_listing]
        if not isinstance(data, list) or len(data) < 2:
            return []

        comments_listing = data[1].get("data", {}).get("children", [])
        links: list[ReplayLink] = []
        seen_urls: set[str] = set()

        def _extract_from_body(body: str) -> None:
            """Extract URLs and magnet URIs from a comment body."""
            # Extract URLs
            for match in URL_PATTERN.finditer(body):
                found_url = match.group(0).rstrip(".,;:!?)")
                if found_url in seen_urls:
                    continue
                if "reddit.com" in found_url or "redd.it" in found_url:
                    continue
                seen_urls.add(found_url)
                link_type = _classify_link(found_url)
                links.append(ReplayLink(
                    url=found_url,
                    link_type=link_type,
                    label=_make_label(found_url),
                ))

            # Extract magnet URIs
            for match in MAGNET_PATTERN.finditer(body):
                magnet_uri = match.group(0).rstrip(".,;:!?)")
                btih_match = BTIH_HASH_RE.search(magnet_uri)
                if btih_match:
                    btih_key = f"btih:{btih_match.group(1).lower()}"
                    if btih_key in seen_urls:
                        continue
                    seen_urls.add(btih_key)
                elif magnet_uri in seen_urls:
                    continue
                else:
                    seen_urls.add(magnet_uri)
                links.append(ReplayLink(
                    url=magnet_uri,
                    link_type="magnet",
                    label=_make_label(magnet_uri),
                ))

        # Walk top-level comments + 1 level of replies
        for child in comments_listing:
            if child.get("kind") != "t1":
                continue
            comment_data = child.get("data", {})
            body = comment_data.get("body", "")
            if body:
                _extract_from_body(body)

            # Check replies (1 level deep)
            replies = comment_data.get("replies")
            if isinstance(replies, dict):
                reply_children = replies.get("data", {}).get("children", [])
                for reply_child in reply_children:
                    if reply_child.get("kind") != "t1":
                        continue
                    reply_body = reply_child.get("data", {}).get("body", "")
                    if reply_body:
                        _extract_from_body(reply_body)

        return links
    except RateLimitError:
        raise
    except Exception:
        logger.debug("[replays] Failed to fetch comments for %s", permalink, exc_info=True)
        return []


class RateLimitError(Exception):
    """Raised when Reddit returns 429."""
    pass


class ReplayService:
    """Scrapes r/MotorsportsReplays, caches results, and serves grouped replays."""

    def __init__(self) -> None:
        self._posts: list[ReplayPost] = []
        self._last_updated: str | None = None
        self._fetched_comment_ids: set[str] = set()  # permalink IDs for comment cache

    async def scrape(self) -> None:
        """Fetch new posts from Reddit and update the cache."""
        logger.info("Scraping r/MotorsportsReplays...")
        cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
        cutoff_ts = cutoff.timestamp()

        posts: list[ReplayPost] = []
        comment_enriched = 0

        try:
            async with httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                # Fetch up to 100 newest posts
                after = None
                for _ in range(2):  # Max 2 pages (200 posts)
                    params = {"limit": "100", "raw_json": "1"}
                    if after:
                        params["after"] = after

                    resp = await client.get(REDDIT_URL, params=params)
                    if resp.status_code == 429:
                        logger.warning("[replays] Reddit rate limited, using cached data")
                        return
                    if resp.status_code != 200:
                        logger.warning("[replays] Reddit returned %d", resp.status_code)
                        return

                    data = resp.json()
                    children = data.get("data", {}).get("children", [])
                    if not children:
                        break

                    for child in children:
                        post_data = child.get("data", {})
                        created_utc = post_data.get("created_utc", 0)

                        # Skip posts older than cutoff
                        if created_utc < cutoff_ts:
                            continue

                        title = post_data.get("title", "")
                        flair = post_data.get("link_flair_text")

                        if not _is_f1_post(title, flair):
                            continue

                        links = _extract_links_from_post(post_data)
                        if not links:
                            continue

                        # Resolve video URLs concurrently (Streamable + Pixeldrain)
                        streamable_links = [
                            link for link in links
                            if link.link_type == "video" and "streamable.com" in link.url
                        ]
                        if streamable_links:
                            results = await asyncio.gather(
                                *(_resolve_streamable_url(link.url, client) for link in streamable_links)
                            )
                            for link, video_url in zip(streamable_links, results):
                                if video_url:
                                    link.video_url = video_url

                        pixeldrain_links = [
                            link for link in links
                            if link.link_type == "video" and "pixeldrain.com" in link.url
                        ]
                        if pixeldrain_links:
                            results = await asyncio.gather(
                                *(_resolve_pixeldrain_url(link.url, client) for link in pixeldrain_links)
                            )
                            for link, video_url in zip(pixeldrain_links, results):
                                if video_url:
                                    link.video_url = video_url

                        permalink = post_data.get("permalink", "")
                        reddit_url = f"https://www.reddit.com{permalink}" if permalink else ""

                        post = ReplayPost(
                            title=title,
                            reddit_url=reddit_url,
                            created_utc=created_utc,
                            flair=flair,
                            event_name=_extract_event_name(title),
                            session_type=_extract_session_type(title),
                            links=links,
                        )
                        posts.append(post)

                    # Check for next page
                    after = data.get("data", {}).get("after")
                    if not after:
                        break

                # Comment scanning: enrich posts that have few links
                comment_enriched = 0
                for post in posts:
                    permalink = post.reddit_url.replace("https://www.reddit.com", "") if post.reddit_url else ""
                    if not permalink:
                        continue

                    # Skip posts we've already fetched comments for
                    if permalink in self._fetched_comment_ids:
                        continue

                    # Only fetch comments for posts with < 3 links (optimization)
                    if len(post.links) >= 3:
                        self._fetched_comment_ids.add(permalink)
                        continue

                    try:
                        # Rate limit: 1 req/sec for comment fetches
                        if comment_enriched > 0:
                            await asyncio.sleep(1.0)

                        comment_links = await _fetch_post_comments(permalink, client)
                        self._fetched_comment_ids.add(permalink)

                        if comment_links:
                            # Build dedup set from existing post links
                            existing_urls: set[str] = set()
                            for link in post.links:
                                existing_urls.add(link.url)
                                # Also track btih hashes for magnet dedup
                                if link.link_type == "magnet":
                                    btih_match = BTIH_HASH_RE.search(link.url)
                                    if btih_match:
                                        existing_urls.add(f"btih:{btih_match.group(1).lower()}")

                            # Merge new links, dedup against existing
                            added = 0
                            for clink in comment_links:
                                # Check URL dedup
                                if clink.url in existing_urls:
                                    continue
                                # Check btih dedup for magnets
                                if clink.link_type == "magnet":
                                    btih_match = BTIH_HASH_RE.search(clink.url)
                                    if btih_match:
                                        btih_key = f"btih:{btih_match.group(1).lower()}"
                                        if btih_key in existing_urls:
                                            continue
                                        existing_urls.add(btih_key)
                                existing_urls.add(clink.url)
                                post.links.append(clink)
                                added += 1

                            if added > 0:
                                logger.debug("[replays] Added %d links from comments for: %s", added, post.title)

                        comment_enriched += 1

                    except RateLimitError:
                        logger.warning("[replays] Rate limited during comment scanning, stopping comment fetch")
                        break

        except Exception:
            logger.exception("[replays] Failed to scrape Reddit")
            return

        self._posts = posts
        self._last_updated = datetime.now(timezone.utc).isoformat()
        logger.info("[replays] Scraped %d F1 replay post(s), enriched %d with comments", len(posts), comment_enriched)

    def get_replays_grouped(self, schedule_races: list[dict] | None = None) -> dict:
        """Return replay posts grouped by event, with sessions as sub-groups."""

        def _normalize_event_name(name: str) -> tuple[str, str | None]:
            """Match event name against schedule for canonical name + date."""
            if not schedule_races or not name or name == "Other":
                return name, None

            name_lower = name.lower()
            name_core = name_lower.replace("grand prix", "").replace(" gp", "").strip()

            for race in schedule_races:
                race_name = race.get("race_name", "")
                race_core = race_name.lower().replace("grand prix", "").strip()
                race_country = race.get("country", "").lower()
                race_locality = race.get("locality", "").lower()

                if name_core and race_core and (
                    name_core in race_core
                    or race_core in name_core
                    or name_core in race_country
                    or race_country in name_core
                    or (race_locality and name_core in race_locality)
                ):
                    return race_name, race.get("date")

            return name, None

        events: dict[str, dict] = {}

        for post in self._posts:
            raw_name = post.event_name or "Other"
            event_name, event_date = _normalize_event_name(raw_name)

            if event_name not in events:
                events[event_name] = {
                    "event_name": event_name,
                    "event_date": event_date,
                    "sessions": {},
                }
            elif event_date and events[event_name]["event_date"] is None:
                events[event_name]["event_date"] = event_date

            session_type = post.session_type or "Full Event"
            if session_type not in events[event_name]["sessions"]:
                events[event_name]["sessions"][session_type] = []

            events[event_name]["sessions"][session_type].append(post.to_dict())

        sorted_events = sorted(
            events.values(),
            key=lambda e: max(
                p["created_utc"]
                for posts in e["sessions"].values()
                for p in posts
            ),
            reverse=True,
        )

        return {
            "events": sorted_events,
            "last_updated": self._last_updated,
            "total_posts": len(self._posts),
        }
