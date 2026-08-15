"""
Instagram Public Profile Scraper API
-------------------------------------
Scrapes public Instagram profiles (no login) and returns posts
classified as photo / video, deduplicated, with basic pagination,
caching and rate limiting.

Endpoints:
  GET /                              -> health check
  GET /health                        -> health check
  GET /instagram/details?url=...     -> full details (photos + videos)
  GET /instagram/videos?url=...      -> videos only (lighter payload)

IMPORTANT LIMITATION (please read):
Without logging into Instagram, the public web endpoint only reliably
returns the most recent batch of posts (usually ~12, sometimes a bit
more via one extra page). Deeper pagination requires an authenticated
session and is intentionally NOT implemented here, per the requirement
to never use Instagram credentials or bypass privacy/security controls.
This API will return everything it can safely retrieve and will report
the actual retrieved count in the response - it will never fabricate
data to reach a target number.
"""

import os
import re
import time
import logging
from collections import defaultdict, deque
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

# --------------------------------------------------------------------------
# Config (all overridable via environment variables)
# --------------------------------------------------------------------------
MAX_POSTS = int(os.environ.get("MAX_POSTS", "5000"))
CACHE_TTL = int(os.environ.get("CACHE_TTL", "1800"))          # seconds
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "10"))          # requests / minute / IP
PORT = int(os.environ.get("PORT", "10000"))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
IG_APP_ID = "936619743392459"  # public web app id used by instagram.com itself

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("insta-api")

app = FastAPI(title="Instagram Details API")

# --------------------------------------------------------------------------
# Very small in-memory cache:  { username: (expires_at, result_dict) }
# --------------------------------------------------------------------------
_cache: dict[str, tuple[float, dict]] = {}

# --------------------------------------------------------------------------
# Very small in-memory rate limiter:  { ip: deque[timestamps] }
# --------------------------------------------------------------------------
_hits: dict[str, deque] = defaultdict(deque)


def rate_limit_ok(ip: str) -> bool:
    if RATE_LIMIT <= 0:
        return True
    now = time.time()
    window = _hits[ip]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= RATE_LIMIT:
        return False
    window.append(now)
    return True


# --------------------------------------------------------------------------
# URL / username helpers
# --------------------------------------------------------------------------
USERNAME_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)/?(?:\?.*)?$"
)


def extract_username(profile_url: str) -> Optional[str]:
    if not profile_url:
        return None
    match = USERNAME_RE.match(profile_url.strip())
    if not match:
        return None
    username = match.group(1)
    # reserved paths that are not usernames
    if username.lower() in {"p", "reel", "reels", "stories", "explore", "accounts", "tv"}:
        return None
    return username


# --------------------------------------------------------------------------
# Instagram scraping
# --------------------------------------------------------------------------
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "X-IG-App-ID": IG_APP_ID,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return s


def fetch_profile(username: str) -> dict:
    """Fetch profile info + first batch of timeline media via Instagram's
    public web_profile_info endpoint (no login required)."""
    url = "https://i.instagram.com/api/v1/users/web_profile_info/"
    s = _session()
    resp = s.get(url, params={"username": username}, timeout=20)

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Instagram profile not found.")
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="Instagram is rate-limiting requests. Try again later.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Instagram returned unexpected status {resp.status_code}.")

    try:
        data = resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Instagram response could not be parsed.")

    user = (data or {}).get("data", {}).get("user")
    if not user:
        raise HTTPException(status_code=404, detail="Instagram profile not found.")

    if user.get("is_private"):
        raise HTTPException(status_code=403, detail="This profile is private. Only public profiles are supported.")

    return user


def _classify_node(node: dict) -> str:
    """Return 'video' or 'photo' for a single media node."""
    typename = node.get("__typename", "")
    if typename == "GraphVideo" or node.get("is_video"):
        return "video"
    if typename == "GraphSidecar":
        children = node.get("edge_sidecar_to_children", {}).get("edges", [])
        for child in children:
            child_node = child.get("node", {})
            if child_node.get("is_video") or child_node.get("__typename") == "GraphVideo":
                return "video"
        return "photo"
    return "photo"


def _post_url(node: dict, kind: str) -> Optional[str]:
    shortcode = node.get("shortcode")
    if not shortcode:
        return None
    if kind == "video":
        return f"https://www.instagram.com/reel/{shortcode}/"
    return f"https://www.instagram.com/p/{shortcode}/"


def collect_posts(user: dict) -> dict:
    """Collect + classify + dedup posts from the profile payload.
    Attempts one extra page if available; deeper pagination is not
    attempted without an authenticated session (see module docstring)."""
    timeline = user.get("edge_owner_to_timeline_media", {})
    edges = timeline.get("edges", [])

    seen = set()
    photos, videos = [], []

    def ingest(edge_list):
        for edge in edge_list:
            if len(photos) + len(videos) >= MAX_POSTS:
                return
            node = edge.get("node", {})
            shortcode = node.get("shortcode")
            if not shortcode or shortcode in seen:
                continue
            seen.add(shortcode)
            kind = _classify_node(node)
            post_url = _post_url(node, kind)
            if not post_url:
                continue
            item = {"type": kind, "url": post_url}
            (videos if kind == "video" else photos).append(item)

    ingest(edges)

    page_info = timeline.get("page_info", {})
    attempted_extra_page = False
    blocked_reason = None

    if page_info.get("has_next_page") and page_info.get("end_cursor"):
        attempted_extra_page = True
        try:
            more_edges = fetch_next_page(user.get("id"), page_info["end_cursor"])
            ingest(more_edges)
        except HTTPException as e:
            blocked_reason = e.detail
        except Exception as e:  # pragma: no cover - defensive
            blocked_reason = str(e)

    return {
        "photos": photos,
        "videos": videos,
        "attempted_extra_page": attempted_extra_page,
        "blocked_reason": blocked_reason,
    }


def fetch_next_page(user_id: str, end_cursor: str) -> list:
    """Best-effort single extra page via Instagram's public GraphQL
    endpoint. This frequently gets rate-limited / blocked without a
    logged-in session - that is expected and handled gracefully by
    the caller."""
    s = _session()
    query_hash = "e769aa130647d2354c40ea6a439bfc08"  # public user timeline query hash
    variables = {
        "id": user_id,
        "first": 50,
        "after": end_cursor,
    }
    resp = s.get(
        "https://www.instagram.com/graphql/query/",
        params={"query_hash": query_hash, "variables": __import__("json").dumps(variables)},
        timeout=20,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=429, detail="Instagram blocked pagination beyond the first batch.")
    data = resp.json()
    media = (
        data.get("data", {})
        .get("user", {})
        .get("edge_owner_to_timeline_media", {})
    )
    return media.get("edges", [])


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("/")
@app.get("/health")
def health():
    return {"status": "ok", "service": "Instagram Details API"}


def _handle_request(request: Request, url: str, videos_only: bool):
    client_ip = request.client.host if request.client else "unknown"
    if not rate_limit_ok(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")

    username = extract_username(url)
    if not username:
        raise HTTPException(status_code=400, detail="Please provide a valid Instagram profile URL.")

    cached = _cache.get(username)
    now = time.time()
    if cached and cached[0] > now:
        result = cached[1]
        log.info(f"cache hit for {username}")
    else:
        log.info(f"scraping profile: {username}")
        user = fetch_profile(username)
        collected = collect_posts(user)
        result = {
            "success": True,
            "username": username,
            "profile_url": f"https://www.instagram.com/{username}/",
            "photos": collected["photos"],
            "videos": collected["videos"],
            "total_photos": len(collected["photos"]),
            "total_videos": len(collected["videos"]),
            "total_posts": len(collected["photos"]) + len(collected["videos"]),
            "note": (
                "Retrieved without login; Instagram only exposes a limited "
                "recent batch of public posts this way."
                + (f" Extra page blocked: {collected['blocked_reason']}" if collected["blocked_reason"] else "")
            ),
        }
        if CACHE_TTL > 0:
            _cache[username] = (now + CACHE_TTL, result)

    if result["total_posts"] == 0:
        return JSONResponse(
            {
                "success": True,
                "username": username,
                "profile_url": f"https://www.instagram.com/{username}/",
                "photos": [],
                "videos": [],
                "total_photos": 0,
                "total_videos": 0,
                "total_posts": 0,
                "note": "No public posts were found on this profile.",
            }
        )

    if videos_only:
        return {
            "success": True,
            "username": result["username"],
            "profile_url": result["profile_url"],
            "videos": result["videos"],
            "total_videos": result["total_videos"],
            "note": result["note"],
        }

    return result


@app.get("/instagram/details")
def instagram_details(request: Request, url: str = Query(..., description="Instagram profile URL")):
    return _handle_request(request, url, videos_only=False)


@app.get("/instagram/videos")
def instagram_videos(request: Request, url: str = Query(..., description="Instagram profile URL")):
    return _handle_request(request, url, videos_only=True)


@app.exception_handler(HTTPException)
def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"success": False, "error": exc.detail})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=PORT)
