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
    r"\b(?:motogp|moto\s?gp|moto2|moto3|motoe|indycar|indy\s?car|nascar|"
    r"rally|wrc|wec|lemans|le\s+mans|superbike|dtm|supercars|"
    r"formula\s+e|formula\s+2|formula\s+3|f2|f3|fe)\b",
    re.IGNORECASE,
)

# Session type detection from post titles (more specific patterns first)
SESSION_PATTERNS = [
    (re.compile(r"\b(?:sprint qualifying|sprint shootout|sq)\b", re.IGNORECASE), "Sprint Qualifying"),
    (re.compile(r"\b(?:sprint race|sprint)\b", re.IGNORECASE), "Sprint"),
    (re.compile(r"\b(?:race)\b", re.IGNORECASE), "Race"),
    (re.compile(r"\b(?:qualifying|quali|q1|q2|q3)\b", re.IGNORECASE), "Qualifying"),
    (re.compile(r"\b(?:free practice|practice|fp1|fp2|fp3)\b", re.IGNORECASE), "Practice"),
    (re.compile(r"\b(?:pre-race|pre race|build.?up)\b", re.IGNORECASE), "Pre-Race"),
]

# Known GP names for event extraction
GP_NAME_PATTERN = re.compile(
    r"(?:(?:20\d{2})\s+)?(?:F1\s+)?"
    r"([\w\s]+?(?:Grand Prix|GP))",
    re.IGNORECASE,
)

# Link type detection
VIDEO_DOMAINS = {"streamable.com", "streamja.com", "streamff.com", "streamgg.com"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".avi"}
EMBED_DOMAINS = {"rerace.io", "f1full.com", "f1fullraces.com"}

# URL extraction from selftext markdown
URL_PATTERN = re.compile(r"https?://[^\s\)\]>\"]+")


@dataclass
class ReplayLink:
    url: str
    link_type: str  # "video", "embed", "external"
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


def _extract_session_type(title: str) -> str | None:
    """Extract the session type from a post title."""
    for pattern, session_type in SESSION_PATTERNS:
        if pattern.search(title):
            return session_type
    return None


def _extract_event_name(title: str) -> str | None:
    """Extract the Grand Prix name from a post title."""
    match = GP_NAME_PATTERN.search(title)
    if match:
        name = match.group(1).strip()
        # Normalize: ensure it ends with "Grand Prix" not just "GP"
        if name.lower().endswith(" gp"):
            name = name[:-3] + " Grand Prix"
        return name
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
    from urllib.parse import urlparse
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


class ReplayService:
    """Scrapes r/MotorsportsReplays, caches results, and serves grouped replays."""

    def __init__(self) -> None:
        self._posts: list[ReplayPost] = []
        self._last_updated: str | None = None

    async def scrape(self) -> None:
        """Fetch new posts from Reddit and update the cache."""
        logger.info("Scraping r/MotorsportsReplays...")
        cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
        cutoff_ts = cutoff.timestamp()

        posts: list[ReplayPost] = []

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

                        # Collect streamable links for concurrent resolution
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

        except Exception:
            logger.exception("[replays] Failed to scrape Reddit")
            return

        self._posts = posts
        self._last_updated = datetime.now(timezone.utc).isoformat()
        logger.info("[replays] Scraped %d F1 replay post(s)", len(posts))

    def get_replays_grouped(self, schedule_races: list[dict] | None = None) -> dict:
        """Return replay posts grouped by event, with sessions as sub-groups.

        Args:
            schedule_races: Optional list of races from schedule service for
                           cross-referencing official event names.
        """
        # Build event groups
        events: dict[str, dict] = {}  # event_name -> {event_name, event_date, sessions: {type: [posts]}}

        for post in self._posts:
            event_name = post.event_name or "Other"

            # Try to match with official schedule for better names and dates
            event_date = None
            if schedule_races and event_name != "Other":
                for race in schedule_races:
                    race_name = race.get("race_name", "")
                    if (
                        event_name.lower().replace("grand prix", "").strip()
                        in race_name.lower()
                        or race_name.lower().replace("grand prix", "").strip()
                        in event_name.lower()
                    ):
                        event_name = race_name
                        event_date = race.get("date")
                        break

            if event_name not in events:
                events[event_name] = {
                    "event_name": event_name,
                    "event_date": event_date,
                    "sessions": {},
                }
            elif event_date and events[event_name]["event_date"] is None:
                events[event_name]["event_date"] = event_date

            session_type = post.session_type or "Other"
            if session_type not in events[event_name]["sessions"]:
                events[event_name]["sessions"][session_type] = []

            events[event_name]["sessions"][session_type].append(post.to_dict())

        # Sort events by most recent post
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
