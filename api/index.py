"""
KuroAnime API — Vercel Serverless & Standalone FastAPI Backend
Provides JSON API over SQLite hianime.db + Direct Ad-Free HLS Stream Resolver & Proxy
"""
import os
import time as _time
import sqlite3
import re
import html as _html
import unicodedata
import difflib
import base64
import json
import urllib.parse
from typing import Optional

import requests as _requests
try:
    from dotenv import load_dotenv
    for _env_file in ['.env.local', '.env', 'anime_app/web/.env.local', 'anime_app/web/.env']:
        if os.path.exists(_env_file):
            load_dotenv(_env_file, override=True)
except Exception:
    pass
from requests.adapters import HTTPAdapter
from fastapi import FastAPI, HTTPException, Query, APIRouter, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import ipaddress as _ipaddress
import socket as _socket
import threading as _threading

# ─── Resilient DoH DNS Fallback (handles flaky upstream NS like zokoanime.video) ─
_ORIG_GETADDRINFO = _socket.getaddrinfo
_DOH_LOCK = _threading.Lock()
_DOH_TLS = _threading.local()
_DOH_CACHE: dict[str, tuple[float, list[str]]] = {}
_OS_DNS_BAD: dict[str, float] = {}
_DOH_STALE: dict[str, list[str]] = {
    "zokoanime.video": ["5.253.86.249"],
}


def _is_ip_literal(host: str) -> bool:
    try:
        _ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _doh_lookup(host: str) -> list[str]:
    if not host or _is_ip_literal(host) or host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        return []
    now = _time.time()
    with _DOH_LOCK:
        cached = _DOH_CACHE.get(host)
        if cached and now < cached[0]:
            return cached[1]

    ips: list[str] = []
    _DOH_TLS.active = True
    try:
        for doh_url, hdrs in (
            (f"https://8.8.8.8/resolve?name={urllib.parse.quote(host)}&type=A", {"Accept": "application/dns-json"}),
            (f"https://8.8.4.4/resolve?name={urllib.parse.quote(host)}&type=A", {"Accept": "application/dns-json"}),
            (f"https://1.1.1.1/dns-query?name={urllib.parse.quote(host)}&type=A", {"Accept": "application/dns-json"}),
        ):
            try:
                dr = _requests.get(doh_url, headers=hdrs, timeout=3.0)
                if dr.status_code != 200:
                    continue
                payload = dr.json()
                for ans in payload.get("Answer") or []:
                    if ans.get("type") == 1 and ans.get("data"):
                        cand = str(ans["data"]).strip()
                        try:
                            if _ipaddress.ip_address(cand).is_global and cand not in ips:
                                ips.append(cand)
                        except ValueError:
                            pass
                if ips:
                    break
            except Exception:
                continue
    finally:
        _DOH_TLS.active = False

    with _DOH_LOCK:
        if ips:
            _DOH_CACHE[host] = (now + 3600.0, ips)
            _DOH_STALE[host] = ips
            return ips
        stale = _DOH_STALE.get(host)
        if stale:
            return stale
    return []


def _resilient_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    norm_host = host.decode("ascii", "ignore") if isinstance(host, bytes) else host
    host_key = norm_host.strip().lower() if isinstance(norm_host, str) else ""

    # Skip slow failing OS DNS lookups if recently known broken and cached in DoH
    if (
        host_key
        and not getattr(_DOH_TLS, "active", False)
        and (_OS_DNS_BAD.get(host_key, 0.0) > _time.time())
    ):
        ips = _doh_lookup(host_key)
        for ip in ips:
            try:
                return _ORIG_GETADDRINFO(ip, port, family, type, proto, flags)
            except _socket.gaierror:
                continue

    try:
        return _ORIG_GETADDRINFO(host, port, family, type, proto, flags)
    except _socket.gaierror:
        if getattr(_DOH_TLS, "active", False) or not host_key or _is_ip_literal(host_key):
            raise
        with _DOH_LOCK:
            _OS_DNS_BAD[host_key] = _time.time() + 600.0
        ips = _doh_lookup(host_key)
        if not ips:
            raise
        for ip in ips:
            try:
                return _ORIG_GETADDRINFO(ip, port, family, type, proto, flags)
            except _socket.gaierror:
                continue
        raise


_socket.getaddrinfo = _resilient_getaddrinfo

# ─── Persistent HTTP Pool for Fast HLS Segment Proxying ─────────────
_STREAM_SESSION = _requests.Session()
_STREAM_ADAPTER = HTTPAdapter(pool_connections=64, pool_maxsize=64)
_STREAM_SESSION.mount("https://", _STREAM_ADAPTER)
_STREAM_SESSION.mount("http://", _STREAM_ADAPTER)
_STREAM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://zokoanime.video/",
    "Origin": "https://zokoanime.video",
}

# ─── Locate Database File ────────────────────────────────────────────
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(CURRENT_DIR)

CANDIDATES = [
    os.environ.get("ANIME_DB"),
    os.path.join(CURRENT_DIR, "hianime.db"),
    os.path.join(PARENT_DIR, "hianime.db"),
    os.path.join(PARENT_DIR, "anime_app", "hianime.db"),
    os.path.join(os.getcwd(), "hianime.db"),
    os.path.join(os.getcwd(), "anime_app", "hianime.db"),
]

def find_db():
    candidates = [
        os.environ.get("ANIME_DB"),
        os.path.join(CURRENT_DIR, "hianime.db"),
        os.path.join(PARENT_DIR, "hianime.db"),
        os.path.join(os.getcwd(), "hianime.db"),
        os.path.join(CURRENT_DIR, "hianime.db.gz"),
        os.path.join(PARENT_DIR, "hianime.db.gz"),
        os.path.join(CURRENT_DIR, "api", "hianime.db.gz"),
        os.path.join(PARENT_DIR, "api", "hianime.db.gz"),
        os.path.join(os.getcwd(), "api", "hianime.db.gz"),
        os.path.join(os.getcwd(), "anime_app", "web", "api", "hianime.db.gz"),
        os.path.join(CURRENT_DIR, "api", "hianime.db"),
        os.path.join(PARENT_DIR, "api", "hianime.db"),
        os.path.join(os.getcwd(), "api", "hianime.db"),
        os.path.join(os.getcwd(), "anime_app", "web", "api", "hianime.db"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return os.path.abspath(c)
    return os.path.join(CURRENT_DIR, "hianime.db")


def find_munib_db():
    """Locate the independent Server 2 database (munib_server.db or compressed munib_server.db.gz)."""
    root_dir = os.path.dirname(PARENT_DIR)
    candidates = [
        os.environ.get("MUNIB_DB"),
        os.path.join(CURRENT_DIR, "munib_server.db"),
        os.path.join(PARENT_DIR, "munib_server.db"),
        os.path.join(root_dir, "munib_server.db"),
        os.path.join(os.getcwd(), "munib_server.db"),
        os.path.join(CURRENT_DIR, "munib_server.db.gz"),
        os.path.join(PARENT_DIR, "munib_server.db.gz"),
        os.path.join(CURRENT_DIR, "api", "munib_server.db.gz"),
        os.path.join(PARENT_DIR, "api", "munib_server.db.gz"),
        os.path.join(os.getcwd(), "api", "munib_server.db.gz"),
        os.path.join(os.getcwd(), "anime_app", "web", "api", "munib_server.db.gz"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return os.path.abspath(c)
    return None


_INITIALIZED_MUNIB_DB = None
_DB_STAGE_LOCK = _threading.Lock()


def _stage_sqlite_file(source: str, filename: str) -> str:
    """Return local writable .db directly, or atomically stage .gz/.db into serverless /tmp."""
    is_gz = source.endswith(".gz")
    is_vercel = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    if not is_gz and not is_vercel:
        return source

    import gzip
    import shutil
    import tempfile

    tmp_dir = "/tmp" if is_vercel else tempfile.gettempdir()
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_db = os.path.join(tmp_dir, filename)

    with _DB_STAGE_LOCK:
        need_stage = not os.path.exists(tmp_db) or os.path.getsize(tmp_db) == 0
        if not need_stage and not is_gz and os.path.getsize(tmp_db) != os.path.getsize(source):
            need_stage = True
        if need_stage:
            part_path = f"{tmp_db}.{os.getpid()}.part"
            try:
                if is_gz:
                    with gzip.open(source, "rb") as f_in, open(part_path, "wb") as f_out:
                        shutil.copyfileobj(f_in, f_out)
                else:
                    shutil.copyfile(source, part_path)
                os.replace(part_path, tmp_db)
            except Exception:
                if os.path.exists(part_path):
                    try:
                        os.unlink(part_path)
                    except Exception:
                        pass
                raise
    return tmp_db


def get_munib_db_path():
    global _INITIALIZED_MUNIB_DB
    if _INITIALIZED_MUNIB_DB and os.path.exists(_INITIALIZED_MUNIB_DB):
        return _INITIALIZED_MUNIB_DB

    source = find_munib_db()
    if not source or not os.path.exists(source):
        return None

    try:
        staged = _stage_sqlite_file(source, "munib_server.db")
        if staged and os.path.exists(staged) and os.path.getsize(staged) > 0:
            _INITIALIZED_MUNIB_DB = staged
            return _INITIALIZED_MUNIB_DB
    except Exception:
        if not source.endswith(".gz"):
            _INITIALIZED_MUNIB_DB = source
            return source
        return None
    return None


def _open_sqlite_ro(path: str, timeout: int = 15):
    """Open SQLite safely on local disk or serverless /var/task (/tmp)."""
    if path.startswith("/tmp"):
        try:
            conn = sqlite3.connect(path, timeout=timeout, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn
        except Exception:
            pass

    norm_path = os.path.abspath(path).replace("\\", "/")
    if not norm_path.startswith("/"):
        norm_path = "/" + norm_path

    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        try:
            conn = sqlite3.connect(f"file://{norm_path}?mode=ro&immutable=1", uri=True, timeout=timeout, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn
        except Exception:
            pass

    try:
        conn = sqlite3.connect(f"file://{norm_path}?mode=ro", uri=True, timeout=timeout, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        pass

    conn = sqlite3.connect(path, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def munib_db():
    path = get_munib_db_path()
    if not path or not os.path.exists(path):
        return None
    return _open_sqlite_ro(path, timeout=10)


def munib_rows(sql, params=()):
    """Query the independent munib_server.db without mixing into hianime.db."""
    conn = munib_db()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass

app = FastAPI(title="KuroAnime API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Zero-Overhead AI Bot & Scraper Blocker ──────────────────────────
# Single pre-compiled regex checked in <0.002ms; completely skips /player/* streaming routes
_AI_BOT_UA_RE = re.compile(
    r"(GPTBot|ChatGPT-User|OAI-SearchBot|ClaudeBot|Claude-Web|anthropic-ai|"
    r"PerplexityBot|Perplexity-User|Google-Extended|CCBot|Bytespider|Bytedance|"
    r"Amazonbot|Meta-ExternalAgent|FacebookBot|Applebot-Extended|Diffbot|"
    r"Cohere-ai|Omgilibot|YouBot|PetalBot|ImagesiftBot|Scrapy|HTTrack|HeadlessChrome)",
    re.IGNORECASE,
)

_ROBOTS_TXT = """# KuroAnime — Block All AI Scrapers & LLM Training Crawlers
User-agent: GPTBot
Disallow: /

User-agent: ChatGPT-User
Disallow: /

User-agent: OAI-SearchBot
Disallow: /

User-agent: ClaudeBot
Disallow: /

User-agent: Claude-Web
Disallow: /

User-agent: anthropic-ai
Disallow: /

User-agent: PerplexityBot
Disallow: /

User-agent: Perplexity-User
Disallow: /

User-agent: Google-Extended
Disallow: /

User-agent: CCBot
Disallow: /

User-agent: Bytespider
Disallow: /

User-agent: Amazonbot
Disallow: /

User-agent: Meta-ExternalAgent
Disallow: /

User-agent: FacebookBot
Disallow: /

User-agent: Applebot-Extended
Disallow: /

User-agent: Diffbot
Disallow: /

User-agent: Cohere-ai
Disallow: /

User-agent: Omgilibot
Disallow: /

User-agent: YouBot
Disallow: /

User-agent: PetalBot
Disallow: /

User-agent: Scrapy
Disallow: /

User-agent: *
Disallow: /api/
"""


@app.middleware("http")
async def block_ai_bots_middleware(request: Request, call_next):
    path = request.url.path
    # Never inspect or slow down HLS video streaming, segments, subtitles, or player routes
    if "/player/" in path or "/clean-stream" in path or path.startswith("/assets/"):
        return await call_next(request)

    ua = request.headers.get("user-agent", "")
    if ua and _AI_BOT_UA_RE.search(ua):
        return Response(
            content='{"detail":"Access denied for automated AI crawlers and scrapers."}',
            status_code=403,
            media_type="application/json",
            headers={"X-Robots-Tag": "noai, noimageai"},
        )

    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noai, noimageai"
    return response


@app.get("/robots.txt", include_in_schema=False)
def serve_robots_txt():
    return Response(content=_ROBOTS_TXT, media_type="text/plain")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import traceback
    return Response(
        content=json.dumps({
            "error": str(exc),
            "type": type(exc).__name__,
            "path": request.url.path,
            "traceback": traceback.format_exc(),
        }),
        status_code=500,
        media_type="application/json",
    )

router = APIRouter()

CARD_COLS = """anime_id, title, title_english, title_romaji, type, status, episode_count,
               anilist_score, mal_score, season_year,
               COALESCE(NULLIF(poster_large,''), poster_url) AS poster"""


_INITIALIZED_DB = None

def get_db_path():
    global _INITIALIZED_DB
    if _INITIALIZED_DB and os.path.exists(_INITIALIZED_DB):
        return _INITIALIZED_DB

    source = find_db()
    if not source or not os.path.exists(source):
        raise HTTPException(status_code=500, detail="Database file not found on server")

    try:
        staged = _stage_sqlite_file(source, "hianime.db")
        if staged and os.path.exists(staged) and os.path.getsize(staged) > 0:
            _INITIALIZED_DB = staged
            return _INITIALIZED_DB
    except Exception as e:
        if source.endswith(".gz"):
            raise HTTPException(status_code=500, detail=f"Failed to decompress DB: {e}")

    _INITIALIZED_DB = source
    return source


def db():
    return _open_sqlite_ro(get_db_path(), timeout=15)


def rows(sql, params=()):
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def one(sql, params=()):
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        r = cur.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def audio_flags(anime_ids):
    """Return {anime_id: {'sub': bool, 'dub': bool}} for card badges."""
    if not anime_ids:
        return {}
    q = ",".join("?" * len(anime_ids))
    out = {i: {"sub": False, "dub": False} for i in anime_ids}
    for r in rows(f"SELECT DISTINCT anime_id, type FROM streams WHERE anime_id IN ({q})", anime_ids):
        if r["type"] in ("sub", "dub"):
            out[r["anime_id"]][r["type"]] = True
    for r in munib_rows(f"SELECT DISTINCT anime_id, type FROM streams WHERE anime_id IN ({q})", anime_ids):
        if r["type"] in ("sub", "dub") and r["anime_id"] in out:
            out[r["anime_id"]][r["type"]] = True
    return out


_CANONICAL_MULTI_GENRES = {
    "slice of life": "Slice of Life",
    "martial arts": "Martial Arts",
    "super power": "Super Power",
    "adult cast": "Adult Cast",
    "boys love": "Boys Love",
    "girls love": "Girls Love",
    "mahou shoujo": "Mahou Shoujo",
    "magical girl": "Magical Girl",
    "avant garde": "Avant Garde",
    "award winning": "Award Winning",
    "time travel": "Time Travel",
    "team sports": "Team Sports",
    "combat sports": "Combat Sports",
    "reverse harem": "Reverse Harem",
    "visual arts": "Visual Arts",
    "performing arts": "Performing Arts",
    "high stakes game": "High Stakes Game",
    "video game": "Video Game",
    "school life": "School Life",
    "sci-fi": "Sci-Fi",
    "post-apocalyptic": "Post-Apocalyptic",
}

_KNOWN_SINGLE_GENRES = {
    "action", "adventure", "comedy", "drama", "ecchi", "fantasy", "gore", "harem",
    "hentai", "historical", "horror", "isekai", "josei", "kids", "magic", "mecha",
    "military", "music", "mystery", "mythology", "parody", "police", "psychological",
    "romance", "samurai", "school", "seinen", "shoujo", "shounen", "space", "sports",
    "supernatural", "suspense", "thriller", "vampire", "anthropomorphic", "CGDCT",
    "delinquents", "detective", "educational", "erotica", "gag humor", "gourmet",
    "idols", "medical", "organized crime", "otaku culture", "pets", "racing",
    "reincarnation", "showbiz", "strategy game", "survival", "workplace",
}


def slugify_title(text: str) -> str:
    """Convert anime title or genre into a clean URL-safe lowercase slug."""
    if not text:
        return ""
    norm = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", norm.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)


def extract_clean_genres(raw: str) -> list[str]:
    """Parse messy comma/space-separated genre strings into clean canonical anime genres."""
    if not raw:
        return []
    out = []
    seen = set()

    def _add(label: str):
        key = label.lower().strip()
        if key and key not in seen:
            seen.add(key)
            out.append(label.strip())

    for part in str(raw).split(","):
        tok = re.sub(r"\s+", " ", part).strip()
        if not tok or len(tok) > 60 or any(ch in tok for ch in (".", ";", ":", "!", "?")):
            continue
        low = tok.lower()
        if low in _CANONICAL_MULTI_GENRES:
            _add(_CANONICAL_MULTI_GENRES[low])
            continue
        if len(tok) <= 22 and tok.replace("-", "").replace(" ", "").isalpha() and " " not in tok:
            _add(tok)
            continue
        # Handle space-joined scrape artifacts like "Adventure Fantasy Action"
        remaining = low
        for m_low, m_canon in _CANONICAL_MULTI_GENRES.items():
            if m_low in remaining:
                _add(m_canon)
                remaining = remaining.replace(m_low, " ")
        for w in remaining.split():
            if w in _KNOWN_SINGLE_GENRES:
                _add(w.title())
    return out


def _normalize_genre_query(raw_genre: str) -> str:
    """Resolve URL slug (e.g. 'slice-of-life', 'sci-fi') back to readable DB genre name."""
    g = (raw_genre or "").strip()
    if not g:
        return ""
    low = g.lower()
    if low in _CANONICAL_MULTI_GENRES:
        return _CANONICAL_MULTI_GENRES[low]
    slug_low = low.replace(" ", "-")
    for m_low, m_canon in _CANONICAL_MULTI_GENRES.items():
        if m_low.replace(" ", "-") == slug_low:
            return m_canon
    if "-" in g:
        # e.g. action -> Action, sci-fi handled above
        return " ".join(w.capitalize() for w in g.split("-") if w)
    return g.title() if g.islower() else g


_SLUG_INDEX_LOCK = None
_SLUG_INDEX_CACHE = None


def _get_slug_index():
    """Lazily build bidirectional (id_to_slug, slug_to_id) maps across both DBs."""
    global _SLUG_INDEX_CACHE, _SLUG_INDEX_LOCK
    if _SLUG_INDEX_CACHE is not None:
        return _SLUG_INDEX_CACHE
    import threading
    if _SLUG_INDEX_LOCK is None:
        _SLUG_INDEX_LOCK = threading.Lock()
    with _SLUG_INDEX_LOCK:
        if _SLUG_INDEX_CACHE is not None:
            return _SLUG_INDEX_CACHE
        id_to_slug: dict[int, str] = {}
        slug_to_id: dict[str, int] = {}
        by_id: dict[int, dict] = {}
        try:
            for r in rows("""
                SELECT anime_id, title, title_english, title_romaji, anilist_popularity
                FROM anime
            """):
                by_id[int(r["anime_id"])] = r
        except Exception:
            pass
        try:
            for r in munib_rows("""
                SELECT anime_id, title, title_english, title_romaji, anilist_popularity
                FROM anime
            """):
                aid = int(r["anime_id"])
                if aid not in by_id:
                    by_id[aid] = r
        except Exception:
            pass

        deduped = sorted(
            by_id.values(),
            key=lambda r: (-int(r.get("anilist_popularity") or 0), int(r["anime_id"])),
        )

        # Pass 1: assign canonical slugs using distinct primary catalog titles first
        for r in deduped:
            aid = int(r["anime_id"])
            base_title = r.get("title") or r.get("title_english") or r.get("title_romaji") or f"anime-{aid}"
            base_slug = slugify_title(base_title) or f"anime-{aid}"
            canonical = base_slug if base_slug not in slug_to_id else f"{base_slug}-{aid}"
            id_to_slug[aid] = canonical
            slug_to_id[canonical] = aid
            slug_to_id[f"{base_slug}-{aid}"] = aid

        # Pass 2: register alternate English/Romaji aliases without overriding canonical slugs
        for r in deduped:
            aid = int(r["anime_id"])
            for alt_field in ("title_english", "title_romaji", "title"):
                alt_slug = slugify_title(r.get(alt_field) or "")
                if alt_slug and alt_slug not in slug_to_id:
                    slug_to_id[alt_slug] = aid

        _SLUG_INDEX_CACHE = (id_to_slug, slug_to_id)
        return _SLUG_INDEX_CACHE


def get_anime_slug(anime_id: int, fallback_title: str = "") -> str:
    id_to_slug, _ = _get_slug_index()
    if anime_id in id_to_slug:
        return id_to_slug[anime_id]
    s = slugify_title(fallback_title)
    return f"{s}-{anime_id}" if s else str(anime_id)


def _resolve_anime_id(ident) -> int:
    """Resolve either numeric anime_id or URL slug (e.g. 'attack-on-titan') to anime_id."""
    if isinstance(ident, int):
        return ident
    raw = str(ident or "").strip()
    if not raw:
        raise HTTPException(404, "Anime not found")
    if raw.isdigit():
        return int(raw)
    id_to_slug, slug_to_id = _get_slug_index()
    low = slugify_title(raw)
    if low in slug_to_id:
        return slug_to_id[low]
    if raw.lower() in slug_to_id:
        return slug_to_id[raw.lower()]
    # Support trailing "-<numeric_id>" slugs only when the prefix shares words with that anime's slug
    m_tail = re.search(r"^(.+)-(\d+)$", low)
    if m_tail:
        prefix, cand_id = m_tail.group(1), int(m_tail.group(2))
        canon = id_to_slug.get(cand_id, "")
        if canon:
            canon_tokens = {t for t in canon.split("-") if len(t) >= 3}
            pref_tokens = {t for t in prefix.split("-") if len(t) >= 3}
            if canon.startswith(prefix) or (canon_tokens and (canon_tokens & pref_tokens)):
                return cand_id
    raise HTTPException(404, "Anime not found")


_DUB_IDS_CACHE: Optional[tuple[float, set[int]]] = None


def _get_dub_anime_ids() -> set[int]:
    """Return cached set of anime_id values that have English Dub streams across both servers."""
    global _DUB_IDS_CACHE
    now = _time.time()
    if _DUB_IDS_CACHE and (now - _DUB_IDS_CACHE[0]) < 1800:
        return _DUB_IDS_CACHE[1]
    dub_ids: set[int] = set()
    try:
        for r in rows("SELECT DISTINCT anime_id FROM streams WHERE type = 'dub' AND server_name != 'HD-1'"):
            if r.get("anime_id") is not None:
                dub_ids.add(int(r["anime_id"]))
    except Exception:
        pass
    try:
        for r in munib_rows("SELECT DISTINCT anime_id FROM streams WHERE type = 'dub'"):
            if r.get("anime_id") is not None:
                dub_ids.add(int(r["anime_id"]))
    except Exception:
        pass
    _DUB_IDS_CACHE = (now, dub_ids)
    return dub_ids


def with_flags(cards):
    flags = audio_flags([c["anime_id"] for c in cards])
    id_to_slug, _ = _get_slug_index()
    for c in cards:
        aid = c["anime_id"]
        c.update(flags.get(aid, {"sub": False, "dub": False}))
        if aid in id_to_slug:
            c["slug"] = id_to_slug[aid]
        else:
            s = slugify_title(c.get("title_english") or c.get("title") or "")
            c["slug"] = f"{s}-{aid}" if s else str(aid)
    return cards


@router.get("/health")
def health():
    db_file = find_db()
    munib_file = find_munib_db()
    return {
        "status": "ok",
        "db_path": db_file,
        "db_exists": os.path.exists(db_file),
        "db_size": os.path.getsize(db_file) if os.path.exists(db_file) else 0,
        "munib_db_path": munib_file,
        "munib_db_exists": bool(munib_file and os.path.exists(munib_file)),
        "munib_db_size": os.path.getsize(munib_file) if munib_file and os.path.exists(munib_file) else 0,
        "files_here": os.listdir(CURRENT_DIR) if os.path.exists(CURRENT_DIR) else [],
    }


@router.get("/debug-db")
def debug_db():
    import traceback
    result = {
        "ok": False,
        "db_path": None,
        "db_exists": False,
        "db_size": 0,
        "tmp_exists": os.path.exists("/tmp/hianime.db") if os.path.exists("/tmp") else False,
        "count_anime": None,
        "munib_db_path": None,
        "munib_db_exists": False,
        "munib_db_size": 0,
        "count_munib_anime": 0,
        "count_munib_streams": 0,
        "error": None,
        "traceback": None,
    }
    try:
        path = get_db_path()
        result["db_path"] = path
        result["db_exists"] = os.path.exists(path)
        result["db_size"] = os.path.getsize(path) if os.path.exists(path) else 0
        res = rows("SELECT COUNT(*) AS n FROM anime")
        result["count_anime"] = res[0]["n"]
        mpath = get_munib_db_path()
        result["munib_db_path"] = mpath
        result["munib_db_exists"] = bool(mpath and os.path.exists(mpath))
        result["munib_db_size"] = os.path.getsize(mpath) if mpath and os.path.exists(mpath) else 0
        m_anime = munib_rows("SELECT COUNT(*) AS n FROM anime")
        m_streams = munib_rows("SELECT COUNT(*) AS n FROM streams")
        result["count_munib_anime"] = m_anime[0]["n"] if m_anime else 0
        result["count_munib_streams"] = m_streams[0]["n"] if m_streams else 0
        result["ok"] = True
    except Exception as e:
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()
    return result


@router.get("/stats")
def stats():
    m_streams = munib_rows("SELECT COUNT(*) AS n FROM streams")
    munib_stream_count = m_streams[0]["n"] if m_streams else 0
    return {
        "anime": one("SELECT COUNT(*) n FROM anime")["n"],
        "episodes": one("SELECT COUNT(*) n FROM episodes")["n"],
        "streams": one("SELECT COUNT(*) n FROM streams")["n"] + munib_stream_count,
        "munib_streams": munib_stream_count,
    }


@router.get("/home")
def home():
    spotlight = rows(f"""
        SELECT {CARD_COLS}, banner_image, synopsis, genres
        FROM anime
        WHERE banner_image IS NOT NULL 
          AND banner_image != ''
          AND (status LIKE '%Currently Airing%' OR anilist_status='RELEASING' OR category='airing')
          AND status NOT LIKE '%Finished%'
          AND (anilist_status IS NULL OR anilist_status != 'FINISHED')
          AND episode_count > 1
          AND (title NOT LIKE '%Special%' AND title NOT LIKE '%OVA%' AND title NOT LIKE '%Movie%' AND title NOT LIKE '%Uncensored%')
        ORDER BY
          CASE WHEN anime_id IN (1, 90, 6, 144, 4587, 2866, 1898, 5) THEN 0 ELSE 1 END,
          CASE WHEN banner_image LIKE '%image.tmdb.org%' THEN 0 ELSE 1 END,
          anilist_popularity DESC NULLS LAST
        LIMIT 8
    """)
    rails = {
        "trending": rows(f"SELECT {CARD_COLS} FROM anime ORDER BY anilist_popularity DESC NULLS LAST LIMIT 20"),
        "top_rated": rows(f"SELECT {CARD_COLS} FROM anime WHERE anilist_score IS NOT NULL ORDER BY anilist_score DESC LIMIT 20"),
        "airing": rows(f"""
            SELECT {CARD_COLS} 
            FROM anime 
            WHERE (status LIKE '%Currently Airing%' OR anilist_status='RELEASING' OR category='airing')
              AND status NOT LIKE '%Finished%' 
              AND (anilist_status IS NULL OR anilist_status != 'FINISHED')
            ORDER BY anilist_popularity DESC NULLS LAST 
            LIMIT 40
        """),
        "movies": rows(f"SELECT {CARD_COLS} FROM anime WHERE type LIKE '%Movie%' ORDER BY anilist_popularity DESC NULLS LAST LIMIT 20"),
        "recent": rows(f"SELECT {CARD_COLS} FROM anime ORDER BY scraped_at DESC LIMIT 30"),
    }
    for k in rails:
        with_flags(rails[k])
    with_flags(spotlight)
    schedule_items = build_schedule_catalog()
    return {"spotlight": spotlight, "rails": rails, "schedule": schedule_items}


def build_schedule_catalog():
    """Return clean airing broadcast TV anime enriched with stream availability counts for the PKT Schedule."""
    items = rows("""
        SELECT a.anime_id, a.anilist_id, a.title, a.title_english, a.type, a.status,
               a.episode_count, a.al_episodes, a.anilist_score, a.mal_score, a.season_year,
               a.genres, a.duration,
               COALESCE(NULLIF(a.poster_large,''), a.poster_url) AS poster,
               (SELECT COUNT(DISTINCT s.episode_id)
                FROM streams s
                WHERE s.anime_id = a.anime_id
                  AND s.embed_url IS NOT NULL AND s.embed_url != '') AS available_streams
        FROM anime a
        WHERE (a.status LIKE '%Currently Airing%' OR a.anilist_status='RELEASING' OR a.category='airing')
          AND a.status NOT LIKE '%Finished%'
          AND (a.anilist_status IS NULL OR a.anilist_status != 'FINISHED')
          AND (a.title NOT LIKE '%Uncensored%' AND a.title NOT LIKE '%Special%' AND a.title NOT LIKE '%OVA%' AND a.title NOT LIKE '%Hentai%')
          AND (a.genres IS NULL OR (a.genres NOT LIKE '%Hentai%' AND a.genres NOT LIKE '%Ecchi%'))
          AND (a.type IS NULL OR a.type = 'TV' OR a.type LIKE '%TV%')
        ORDER BY a.anilist_popularity DESC NULLS LAST
        LIMIT 70
    """)
    return with_flags(items)


@router.get("/schedule")
def get_schedule():
    return {"timezone": "Asia/Karachi", "utc_offset": "+05:00", "items": build_schedule_catalog()}


MUNIB_CARD_COLS = """anime_id, title, title_english, title_romaji, type, status, episode_count,
                     anilist_score, NULL AS mal_score, season_year,
                     COALESCE(NULLIF(poster_large,''), poster_url) AS poster"""



def clean_youtube_trailer_id(raw_id: str):
    if not raw_id:
        return None
    s = str(raw_id).strip()
    m_embed = re.search(r"(?:embed/|v=|youtu\.be/)([a-zA-Z0-9_-]{11})", s)
    if m_embed:
        return m_embed.group(1)
    m = re.search(r"^[a-zA-Z0-9_-]{11}$", s) or re.search(r"[a-zA-Z0-9_-]{11}", s)
    return m.group(0) if m else None


def _lookup_cached_trailer(anime_id: int, anilist_id=None):
    try:
        t_row = one("SELECT trailer_id, site FROM anime_trailers WHERE anime_id = ?", (anime_id,))
        if t_row and t_row.get("trailer_id"):
            cid = clean_youtube_trailer_id(t_row["trailer_id"])
            if cid:
                return cid, (t_row.get("site") or "youtube")
    except Exception:
        pass
    try:
        m_rows_t = munib_rows("SELECT trailer_id, site FROM anime_trailers WHERE anime_id = ?", (anime_id,))
        if m_rows_t and m_rows_t[0].get("trailer_id"):
            cid = clean_youtube_trailer_id(m_rows_t[0]["trailer_id"])
            if cid:
                return cid, (m_rows_t[0].get("site") or "youtube")
    except Exception:
        pass
    if anilist_id:
        try:
            t_al = one("""
                SELECT t.trailer_id, t.site
                FROM anime_trailers t
                JOIN anime a ON a.anime_id = t.anime_id
                WHERE a.anilist_id = ? AND t.trailer_id IS NOT NULL AND t.trailer_id != ''
                LIMIT 1
            """, (int(anilist_id),))
            if t_al and t_al.get("trailer_id"):
                cid = clean_youtube_trailer_id(t_al["trailer_id"])
                if cid:
                    return cid, (t_al.get("site") or "youtube")
        except Exception:
            pass
        try:
            m_al = munib_rows("""
                SELECT t.trailer_id, t.site
                FROM anime_trailers t
                JOIN anime a ON a.anime_id = t.anime_id
                WHERE a.anilist_id = ? AND t.trailer_id IS NOT NULL AND t.trailer_id != ''
                LIMIT 1
            """, (int(anilist_id),))
            if m_al and m_al[0].get("trailer_id"):
                cid = clean_youtube_trailer_id(m_al[0]["trailer_id"])
                if cid:
                    return cid, (m_al[0].get("site") or "youtube")
        except Exception:
            pass
    return None, None


def _search_youtube_innertube_trailer(query_title: str):
    if not query_title:
        return None
    try:
        yt_url = (
            "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"
            "&fields=contents.twoColumnSearchResultsRenderer.primaryContents.sectionListRenderer.contents.itemSectionRenderer.contents.videoRenderer.videoId"
        )
        r = _requests.post(
            yt_url,
            json={
                "context": {"client": {"clientName": "WEB", "clientVersion": "2.20241114.01.00", "hl": "en", "gl": "US"}},
                "query": f"{query_title} official anime trailer PV"
            },
            timeout=3.5
        )
        if r.status_code == 200:
            matches = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', r.text)
            if matches:
                return matches[0]
    except Exception:
        pass
    return None


def resolve_trailer_id(anilist_id=None, title=None, alt_titles=None):
    candidate_titles = []
    for t in ([title] + (list(alt_titles) if alt_titles else [])):
        if t and str(t).strip() and str(t).strip() not in candidate_titles:
            candidate_titles.append(str(t).strip())

    if anilist_id:
        try:
            q = "query ($id: Int) { Media(id: $id, type: ANIME) { trailer { id site } } }"
            r = _requests.post("https://graphql.anilist.co", json={"query": q, "variables": {"id": int(anilist_id)}}, timeout=3)
            if r.status_code == 200:
                tr = r.json().get("data", {}).get("Media", {}).get("trailer")
                if tr and tr.get("site") == "youtube" and tr.get("id"):
                    cid = clean_youtube_trailer_id(tr["id"])
                    if cid:
                        return cid
        except Exception:
            pass

    # Fast YouTube InnerTube API search (946-byte field-filtered JSON, ~200ms)
    for cand in candidate_titles[:2]:
        cid = _search_youtube_innertube_trailer(cand)
        if cid:
            return cid

    for cand in candidate_titles[:2]:
        try:
            enc = urllib.parse.quote(cand)
            r = _requests.get(f"https://api.jikan.moe/v4/anime?q={enc}&limit=1", timeout=3.5)
            if r.status_code == 200:
                d = r.json().get("data", [])
                if d and isinstance(d, list) and len(d) > 0:
                    tr_obj = d[0].get("trailer") or {}
                    yt_id = tr_obj.get("youtube_id") or tr_obj.get("embed_url") or tr_obj.get("url")
                    if yt_id:
                        cid = clean_youtube_trailer_id(yt_id)
                        if cid:
                            return cid
        except Exception:
            pass

    for cand in candidate_titles[:1]:
        try:
            enc = urllib.parse.quote(f"{cand} official anime trailer")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
                "Cookie": "SOCS=CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"
            }
            r = _requests.get(f"https://www.youtube.com/results?search_query={enc}", headers=headers, timeout=3.5)
            if r.status_code == 200:
                json_matches = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', r.text)
                if json_matches:
                    return json_matches[0]
                matches = re.findall(r"/watch\?v=([a-zA-Z0-9_-]{11})", r.text)
                if matches:
                    return matches[0]
        except Exception:
            pass

    return None


@router.get("/anime/{anime_ident}")
def anime_detail(anime_ident: str):
    anime_id = _resolve_anime_id(anime_ident)
    a = one("SELECT * FROM anime WHERE anime_id = ?", (anime_id,))
    if not a:
        m_list = munib_rows("SELECT * FROM anime WHERE anime_id = ?", (anime_id,))
        if not m_list:
            raise HTTPException(404, "Anime not found")
        a = m_list[0]
        a["poster"] = a.get("poster_large") or a.get("poster_url")
        a["seasons"] = []
        a.update(audio_flags([anime_id]).get(anime_id, {"sub": True, "dub": False}))
        a["slug"] = get_anime_slug(anime_id, a.get("title_english") or a.get("title") or "")
        tid, tsite = _lookup_cached_trailer(anime_id, a.get("anilist_id"))
        if tid:
            a["trailer_id"] = tid
            a["trailer_site"] = tsite or "youtube"
        return a
    a["poster"] = a.get("poster_large") or a.get("poster_url")
    a["seasons"] = rows("""
        SELECT season_name, season_anime_id, season_poster, is_current
        FROM anime_seasons WHERE parent_anime_id = ? ORDER BY id
    """, (anime_id,))
    have = {r["anime_id"] for r in rows("SELECT anime_id FROM anime")} if a["seasons"] else set()
    for s in a["seasons"]:
        s["available"] = s["season_anime_id"] in have
        s["season_slug"] = get_anime_slug(s["season_anime_id"], s.get("season_name") or "")
    a.update(audio_flags([anime_id]).get(anime_id, {}))
    a["slug"] = get_anime_slug(anime_id, a.get("title_english") or a.get("title") or "")
    tid, tsite = _lookup_cached_trailer(anime_id, a.get("anilist_id"))
    if tid:
        a["trailer_id"] = tid
        a["trailer_site"] = tsite or "youtube"
    a.pop("seasons_json", None)
    return a



@router.get("/anime/{anime_ident}/trailer")
def anime_trailer(anime_ident: str):
    anime_id = _resolve_anime_id(anime_ident)
    # 1. Get anime record from either Server 1 (hianime.db) or Server 2 (munib_server.db)
    a = one("SELECT anime_id, title, title_english, title_romaji, anilist_id FROM anime WHERE anime_id = ?", (anime_id,))
    is_munib_only = False
    if not a:
        m_list = munib_rows("SELECT anime_id, title, title_english, title_romaji, anilist_id FROM anime WHERE anime_id = ?", (anime_id,))
        a = m_list[0] if m_list else None
        is_munib_only = True

    # 2. Check cached anime_trailers across both DBs (and by anilist_id)
    tid, tsite = _lookup_cached_trailer(anime_id, a.get("anilist_id") if a else None)
    if tid:
        return {
            "ok": True,
            "anime_id": anime_id,
            "trailer_id": tid,
            "site": tsite or "youtube",
            "embed_url": f"https://www.youtube.com/embed/{tid}?autoplay=1&playsinline=1&rel=0&modestbranding=1"
        }

    if not a:
        raise HTTPException(404, "Anime not found")

    anilist_id = a.get("anilist_id")
    title = a.get("title_english") or a.get("title")
    alt_titles = [a.get("title"), a.get("title_romaji")]

    # 3. Dynamic multi-source resolution (AniList + YouTube InnerTube + Jikan + YouTube Web)
    tid = resolve_trailer_id(anilist_id, title, alt_titles=alt_titles)
    if tid:
        try:
            target_db_fn = munib_db if is_munib_only else db
            conn = target_db_fn()
            if conn:
                cur = conn.cursor()
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS anime_trailers (
                        anime_id INTEGER PRIMARY KEY,
                        trailer_id TEXT,
                        site TEXT DEFAULT 'youtube',
                        source TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cur.execute("INSERT OR REPLACE INTO anime_trailers (anime_id, trailer_id, site, source) VALUES (?, ?, 'youtube', 'auto')", (anime_id, tid))
                conn.commit()
                conn.close()
        except Exception:
            pass

        return {
            "ok": True,
            "anime_id": anime_id,
            "trailer_id": tid,
            "site": "youtube",
            "embed_url": f"https://www.youtube.com/embed/{tid}?autoplay=1&playsinline=1&rel=0&modestbranding=1"
        }

    return {"ok": False, "message": "No trailer available"}


@router.get("/anime/{anime_ident}/episodes")
def anime_episodes(anime_ident: str):
    anime_id = _resolve_anime_id(anime_ident)
    eps = rows("""
        SELECT e.episode_id, e.episode_number, e.title, e.japanese_title,
               MAX(CASE WHEN s.type='sub' THEN 1 ELSE 0 END) AS sub,
               MAX(CASE WHEN s.type='dub' THEN 1 ELSE 0 END) AS dub
        FROM episodes e
        LEFT JOIN streams s ON s.episode_id = e.episode_id AND s.anime_id = e.anime_id
        WHERE e.anime_id = ?
        GROUP BY e.id
        ORDER BY e.episode_number
    """, (anime_id,))
    # Merge sub/dub availability from independent Munib Server DB (munib_server.db)
    munib_ep_flags = {}
    for mr in munib_rows("SELECT episode_number, type FROM streams WHERE anime_id = ?", (anime_id,)):
        ep_no = mr.get("episode_number")
        st = mr.get("type")
        if ep_no is not None and st in ("sub", "dub"):
            munib_ep_flags.setdefault(ep_no, {"sub": 0, "dub": 0})[st] = 1
    if not eps:
        # Fallback to episodes stored directly in munib_server.db (e.g. Hentai & Munib-exclusive titles)
        m_eps = munib_rows(
            "SELECT episode_id, episode_number, title FROM episodes WHERE anime_id = ? ORDER BY episode_number",
            (anime_id,),
        )
        for me in m_eps:
            mf = munib_ep_flags.get(me["episode_number"], {"sub": 1, "dub": 0})
            eps.append({
                "episode_id": me["episode_id"],
                "episode_number": me["episode_number"],
                "title": me.get("title") or f"Episode {me['episode_number']}",
                "japanese_title": "",
                "sub": mf["sub"],
                "dub": mf["dub"],
            })
        return {"anime_id": anime_id, "episodes": eps}
    for ep in eps:
        mf = munib_ep_flags.get(ep["episode_number"])
        if mf:
            ep["sub"] = max(ep.get("sub") or 0, mf["sub"])
            ep["dub"] = max(ep.get("dub") or 0, mf["dub"])
    return {"anime_id": anime_id, "episodes": eps}


@router.get("/episode/{episode_id}/streams")
def episode_streams(episode_id: str, anime_id: Optional[int] = None):
    sql = "SELECT type, server_name, provider, embed_url FROM streams WHERE episode_id = ? AND server_name != 'HD-1' AND provider NOT LIKE '%megaplay%'"
    params = [episode_id]
    if anime_id is not None:
        sql += " AND anime_id = ?"
        params.append(anime_id)
    data = rows(sql + " ORDER BY type, CASE WHEN server_name = 'ZokoAnime' THEN 0 ELSE 1 END, server_name", params)

    # Query Server 2 ("Munib Server") from its independent munib_server.db
    munib_data = munib_rows(
        "SELECT type, server_name, provider, embed_url FROM streams WHERE episode_id = ?",
        (episode_id,),
    )
    if not munib_data:
        # Fallback match by (anime_id, episode_number) or (anilist_id, episode_number)
        ep_row = one(
            "SELECT anime_id, episode_number FROM episodes WHERE episode_id = ?" + (" AND anime_id = ?" if anime_id is not None else ""),
            (episode_id, anime_id) if anime_id is not None else (episode_id,),
        )
        if ep_row:
            a_id = ep_row["anime_id"]
            ep_no = ep_row["episode_number"]
            munib_data = munib_rows(
                "SELECT type, server_name, provider, embed_url FROM streams WHERE anime_id = ? AND episode_number = ?",
                (a_id, ep_no),
            )
            if not munib_data:
                al_row = one("SELECT anilist_id FROM anime WHERE anime_id = ?", (a_id,))
                al_id = al_row.get("anilist_id") if al_row else None
                if al_id:
                    munib_data = munib_rows(
                        "SELECT type, server_name, provider, embed_url FROM streams WHERE anilist_id = ? AND episode_number = ?",
                        (al_id, ep_no),
                    )
                    # Live fallback to reanime.to flix API whenever Munib Server DB has no entry for this episode
                    if not munib_data and ep_no is not None:
                        try:
                            fr = _STREAM_SESSION.get(
                                f"https://reanime.to/api/flix/{al_id}/{ep_no}",
                                headers={"User-Agent": _STREAM_HEADERS["User-Agent"], "Referer": "https://reanime.to/home"},
                                timeout=1.5,
                            )
                            if fr.status_code == 200:
                                fjson = fr.json()
                                if fjson.get("success") and fjson.get("servers"):
                                    sub_link, dub_link = None, None
                                    for srv in fjson["servers"]:
                                        dtype = (srv.get("dataType") or "").lower()
                                        dlink = (srv.get("dataLink") or "").strip()
                                        if not dlink:
                                            continue
                                        if dtype == "sub" and not sub_link:
                                            sub_link = dlink
                                        elif dtype in ("dub", "s-dub") and not dub_link:
                                            sep = "&" if "?" in dlink else "?"
                                            dub_link = f"{dlink}{sep}a=1" if "a=1" not in dlink else dlink
                                    if sub_link:
                                        munib_data.append({
                                            "type": "sub",
                                            "server_name": "Munib Server",
                                            "provider": "reanime.to",
                                            "embed_url": sub_link,
                                        })
                                    if dub_link:
                                        munib_data.append({
                                            "type": "dub",
                                            "server_name": "Munib Server",
                                            "provider": "reanime.to",
                                            "embed_url": dub_link,
                                        })
                        except Exception:
                            pass

    combined = data + munib_data
    if not combined:
        # Last-resort fallback to HD-1 / MegaPlay if neither ZokoAnime nor Munib Server has this episode
        fallback_sql = "SELECT type, server_name, provider, embed_url FROM streams WHERE episode_id = ?"
        fallback_params = [episode_id]
        if anime_id is not None:
            fallback_sql += " AND anime_id = ?"
            fallback_params.append(anime_id)
        combined = rows(fallback_sql + " ORDER BY type, server_name", fallback_params)
    return {
        "sub": [s for s in combined if s["type"] == "sub"],
        "dub": [s for s in combined if s["type"] == "dub"],
    }


@router.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = 30):
    query_str = q.strip()
    like = f"%{query_str}%"
    
    # 1. Direct or exact substring match
    res = rows(f"""
        SELECT {CARD_COLS} FROM anime
        WHERE title LIKE ? OR title_english LIKE ? OR title_romaji LIKE ? OR synonyms LIKE ?
        ORDER BY anilist_popularity DESC NULLS LAST
        LIMIT ?
    """, (like, like, like, like, min(limit, 60)))
    
    # 2. Tokenized multi-word search (handles missing colons, hyphens, and multi-word queries)
    clean_words = [w for w in re.sub(r'[:\-_,]+', ' ', query_str).split() if len(w) > 1]
    if len(res) < limit and len(clean_words) > 1:
        seen = {r["anime_id"] for r in res}
        conds, params = [], []
        for w in clean_words:
            w_like = f"%{w}%"
            conds.append("(title LIKE ? OR title_english LIKE ? OR title_romaji LIKE ? OR synonyms LIKE ?)")
            params.extend([w_like, w_like, w_like, w_like])
        
        multi_sql = f"""
            SELECT {CARD_COLS} FROM anime
            WHERE {" AND ".join(conds)}
            ORDER BY anilist_popularity DESC NULLS LAST
            LIMIT ?
        """
        more = rows(multi_sql, params + [min(limit, 60)])
        for r in more:
            if r["anime_id"] not in seen:
                res.append(r)
                seen.add(r["anime_id"])
                
    # 3. Also search Munib Server DB for exclusive titles (e.g. Hentai & extra catalog)
    seen = {r["anime_id"] for r in res}
    m_res = munib_rows(f"""
        SELECT {MUNIB_CARD_COLS} FROM anime
        WHERE title LIKE ? OR title_english LIKE ? OR title_romaji LIKE ? OR japanese_name LIKE ?
        ORDER BY anilist_popularity DESC NULLS LAST
        LIMIT ?
    """, (like, like, like, like, min(limit, 60)))
    for mr in m_res:
        if mr["anime_id"] not in seen:
            res.append(mr)
            seen.add(mr["anime_id"])

    return {"results": with_flags(res[:min(limit, 60)])}


_GENRES_CACHE: Optional[tuple[float, list[str]]] = None


@router.get("/browse")
def browse(
    genre: Optional[str] = None,
    type: Optional[str] = None,
    status: Optional[str] = None,
    audio: Optional[str] = None,
    sort: str = "popular",
    page: int = 1,
    per_page: int = 30,
):
    where, params = [], []
    m_where, m_params = [], []
    norm_genre = _normalize_genre_query(genre) if genre else ""
    if norm_genre:
        where.append("(genres LIKE ? OR al_genres LIKE ?)")
        params += [f"%{norm_genre}%", f"%{norm_genre}%"]
        m_where.append("genres LIKE ?")
        m_params.append(f"%{norm_genre}%")
    if type:
        where.append("type LIKE ?")
        params.append(f"%{type}%")
        m_where.append("type LIKE ?")
        m_params.append(f"%{type}%")
    if status:
        st = status.lower()
        if "air" in st or "releas" in st:
            where.append("(status LIKE '%Currently Airing%' OR anilist_status='RELEASING' OR category='airing') AND status NOT LIKE '%Finished%' AND (anilist_status IS NULL OR anilist_status != 'FINISHED')")
            m_where.append("(status LIKE '%Releasing%' OR status LIKE '%Airing%')")
        elif "comp" in st or "finish" in st:
            where.append("(status LIKE '%Finished%' OR anilist_status='FINISHED')")
            m_where.append("status LIKE '%Finished%'")
    if audio and audio.strip().lower() == "dub":
        dub_ids = _get_dub_anime_ids()
        if not dub_ids:
            return {"page": page, "results": []}
        dub_csv = ",".join(str(int(i)) for i in sorted(dub_ids))
        where.append(f"anime_id IN ({dub_csv})")
        m_where.append(f"anime_id IN ({dub_csv})")
    order = {
        "popular": "anilist_popularity DESC NULLS LAST",
        "score": "anilist_score DESC NULLS LAST",
        "az": "title COLLATE NOCASE ASC",
        "new": "season_year DESC NULLS LAST, anilist_popularity DESC NULLS LAST",
    }.get(sort, "anilist_popularity DESC NULLS LAST")
    per_page = max(1, min(per_page, 60))
    offset = (max(page, 1) - 1) * per_page

    # If filtering by Hentai (or if main DB has no matches), query Munib Server DB directly
    if norm_genre and norm_genre.strip().lower() == "hentai":
        m_sql = f"SELECT {MUNIB_CARD_COLS} FROM anime"
        if m_where:
            m_sql += " WHERE " + " AND ".join(m_where)
        m_sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        m_res = munib_rows(m_sql, m_params + [per_page, offset])
        return {"page": page, "results": with_flags(m_res)}

    sql = f"SELECT {CARD_COLS} FROM anime"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
    res = rows(sql, params + [per_page, offset])

    if len(res) < per_page:
        seen = {r["anime_id"] for r in res}
        m_sql = f"SELECT {MUNIB_CARD_COLS} FROM anime"
        m_conds = list(m_where) + ["anime_id >= 1000000"]
        m_sql += " WHERE " + " AND ".join(m_conds)
        m_sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        for mr in munib_rows(m_sql, m_params + [per_page - len(res), offset]):
            if mr["anime_id"] not in seen:
                res.append(mr)
                seen.add(mr["anime_id"])

    return {"page": page, "results": with_flags(res)}


@router.get("/genres")
def genres():
    global _GENRES_CACHE
    now = _time.time()
    if _GENRES_CACHE and (now - _GENRES_CACHE[0]) < 600:
        return {"genres": _GENRES_CACHE[1]}
    counts = {}
    for r in rows("SELECT genres FROM anime WHERE genres IS NOT NULL AND genres != ''"):
        for g in extract_clean_genres(r["genres"]):
            counts[g] = counts.get(g, 0) + 1
    for r in munib_rows("SELECT genres FROM anime WHERE genres IS NOT NULL AND genres != '' AND anime_id >= 1000000"):
        for g in extract_clean_genres(r["genres"]):
            counts[g] = counts.get(g, 0) + 1
    clean_sorted = sorted((g for g, c in counts.items() if c >= 2), key=lambda g: -counts[g])
    _GENRES_CACHE = (now, clean_sorted)
    return {"genres": clean_sorted}


# ─── Direct Ad-Free HLS Stream Resolver & Proxy ─────────────────────

def _deobfuscate_payload(blob: str, key: str = "otaku-embed-v1") -> dict:
    raw = base64.b64decode(blob).decode("latin1")
    xored = "".join(chr(ord(c) ^ ord(key[i % len(key)])) for i, c in enumerate(raw))
    return json.loads(urllib.parse.unquote(xored))


def _parse_master_variants(master_text: str, master_url: str):
    """Extract quality levels from master.m3u8 text."""
    lines = [ln.strip() for ln in master_text.splitlines() if ln.strip()]
    qualities = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("#EXT-X-STREAM-INF:"):
            bw_match = re.search(r"BANDWIDTH=(\d+)", ln)
            res_match = re.search(r"RESOLUTION=(\d+)x(\d+)", ln)
            bandwidth = int(bw_match.group(1)) if bw_match else 0
            width = int(res_match.group(1)) if res_match else 0
            height = int(res_match.group(2)) if res_match else 0
            if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                uri = lines[i + 1]
                full_url = urllib.parse.urljoin(master_url, uri)
                label = f"{height}p" if height else "Auto"
                if height >= 1080:
                    label = "1080p FHD"
                elif height >= 720:
                    label = "720p HD"
                elif height > 0:
                    label = f"{height}p SD"
                qualities.append({
                    "height": height,
                    "width": width,
                    "bandwidth": bandwidth,
                    "label": label,
                    "raw_url": full_url,
                    "url": f"/api/player/hls?url={urllib.parse.quote(full_url, safe='')}",
                })
                i += 2
                continue
        i += 1
    qualities.sort(key=lambda q: q["height"], reverse=True)
    return qualities


_RESOLVE_CACHE: dict = {}
_HLS_PLAYLIST_CACHE: dict = {}


def _rewrite_hls_playlist(raw_text: str, url: str) -> str:
    """Rewrite a master or media .m3u8 playlist so all sub-playlists and segments route via our fast proxy."""
    out_lines = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            out_lines.append("")
            continue

        if line.startswith("#"):
            if 'URI="' in line:
                def _repl_uri(match):
                    raw_uri = match.group(1)
                    u = urllib.parse.urljoin(url, raw_uri)
                    parsed_u = urllib.parse.urlparse(u)
                    if parsed_u.path.endswith(".m3u8"):
                        return f'URI="/api/player/hls?url={urllib.parse.quote(u, safe="")}"'
                    return f'URI="/api/player/segment?url={urllib.parse.quote(u, safe="")}"'
                line = re.sub(r'URI="([^"]+)"', _repl_uri, line)
            out_lines.append(line)
            continue

        full_url = urllib.parse.urljoin(url, line)
        parsed_target = urllib.parse.urlparse(full_url)
        if parsed_target.path.endswith(".m3u8"):
            out_lines.append(f"/api/player/hls?url={urllib.parse.quote(full_url, safe='')}")
        else:
            out_lines.append(f"/api/player/segment?url={urllib.parse.quote(full_url, safe='')}")

    return "\n".join(out_lines)


@router.get("/player/resolve")
def player_resolve(url: str = Query(...)):
    """
    Extract direct HLS master playlist, quality variants, multi-language VTT subtitles,
    and intro/outro skip timestamps from an embed URL, with in-memory caching and playlist pre-warming.
    """
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid embed URL")

    now = _time.time()
    cached_res = _RESOLVE_CACHE.get(url)
    if cached_res and (now - cached_res[0]) < 600:
        return cached_res[1]

    try:
        r = _STREAM_SESSION.get(url, headers={
            "User-Agent": _STREAM_HEADERS["User-Agent"],
            "Referer": "https://hianime.at/",
        }, timeout=10)
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Upstream embed returned {r.status_code}")

        m = re.search(r'window\.__P\s*=\s*["\']([^"\']+)["\']', r.text)
        if not m:
            raise HTTPException(status_code=422, detail="Stream payload not found in embed page")

        payload = _deobfuscate_payload(m.group(1))
        master_url = payload.get("src") or ""
        if not master_url:
            raise HTTPException(status_code=422, detail="No HLS source URL in payload")

        # Fetch master.m3u8 to discover quality variants & pre-warm rewritten playlists inline
        qualities = []
        master_playlist_text = ""
        try:
            mr = _STREAM_SESSION.get(master_url, headers=_STREAM_HEADERS, timeout=(2.5, 6.0))
            if mr.status_code == 200 and "#EXTM3U" in mr.text:
                master_playlist_text = _rewrite_hls_playlist(mr.text, master_url)
                _HLS_PLAYLIST_CACHE[master_url] = (_time.time(), master_playlist_text)
                qualities = _parse_master_variants(mr.text, master_url)
                if qualities:
                    from concurrent.futures import ThreadPoolExecutor

                    def _warm_variant(q_item):
                        raw_v = q_item["raw_url"]
                        try:
                            vr = _STREAM_SESSION.get(raw_v, headers=_STREAM_HEADERS, timeout=(2.5, 5.5))
                            if vr.status_code == 200 and "#EXTM3U" in vr.text:
                                rewritten = _rewrite_hls_playlist(vr.text, raw_v)
                                _HLS_PLAYLIST_CACHE[raw_v] = (_time.time(), rewritten)
                                return rewritten
                        except Exception:
                            pass
                        return ""

                    # Pre-fetch top 2 variants in parallel so client has 0ms playlist startup for Auto, 1080p, or 720p
                    targets = qualities[:2]
                    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
                        for q_item, pl_text in zip(targets, ex.map(_warm_variant, targets)):
                            if pl_text:
                                q_item["playlist_text"] = pl_text
        except Exception:
            pass

        raw_subs = payload.get("subtitles") or []
        subtitles = []
        for s in raw_subs:
            sub_src = s.get("src") or ""
            if not sub_src:
                continue
            subtitles.append({
                "lang": s.get("lang") or "und",
                "label": s.get("label") or s.get("lang") or "Subtitle",
                "default": bool(s.get("default")),
                "raw_url": sub_src,
                "url": f"/api/player/vtt?url={urllib.parse.quote(sub_src, safe='')}",
            })

        result_payload = {
            "ok": True,
            "embed_url": url,
            "raw_master_url": master_url,
            "master_url": f"/api/player/hls?url={urllib.parse.quote(master_url, safe='')}",
            "master_playlist": master_playlist_text,
            "qualities": qualities,
            "subtitles": subtitles,
            "skip": payload.get("skip") or {"intro": None, "outro": None},
            "poster": payload.get("poster") or "",
            "video_id": payload.get("video_id"),
            "download_url": payload.get("download_url") or "",
        }
        if len(_RESOLVE_CACHE) > 200:
            _RESOLVE_CACHE.clear()
        _RESOLVE_CACHE[url] = (_time.time(), result_payload)
        return result_payload
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stream resolve failed: {str(e)}")


def _normalize_segment_url(seg_url: str, origin: str) -> str:
    """Keep fast edge segment URLs intact; fallback hosts are tried in player_segment."""
    return seg_url


@router.get("/player/hls")
def player_hls(url: str = Query(...)):
    """Proxy and rewrite master or media .m3u8 playlists with in-memory caching."""
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid HLS URL")

    now = _time.time()
    cached_pl = _HLS_PLAYLIST_CACHE.get(url)
    if cached_pl and (now - cached_pl[0]) < 300:
        return Response(
            content=cached_pl[1],
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=60",
            },
        )

    try:
        r = _STREAM_SESSION.get(url, headers=_STREAM_HEADERS, timeout=10)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="Upstream HLS playlist error")

        content = _rewrite_hls_playlist(r.text, url)
        if len(_HLS_PLAYLIST_CACHE) > 300:
            _HLS_PLAYLIST_CACHE.clear()
        _HLS_PLAYLIST_CACHE[url] = (_time.time(), content)
        return Response(
            content=content,
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=60",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"HLS proxy error: {str(e)}")


@router.get("/player/segment")
def player_segment(request: Request, url: str = Query(...)):
    """Proxy an individual MPEG-TS video segment with fast chunked streaming, Range pass-through, and mirror failover."""
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid segment URL")

    parsed = urllib.parse.urlparse(url)
    candidate_urls = [url]
    if parsed.path.startswith("/s/"):
        for fallback_host in ("https://hls1.drama1.cfd", "https://hls1.zoko1.cfd", "https://hls1.proxy1.cfd", "https://hls.dramahot.top"):
            alt = f"{fallback_host}{parsed.path}{'?' + parsed.query if parsed.query else ''}"
            if alt not in candidate_urls:
                candidate_urls.append(alt)

    req_hdrs = dict(_STREAM_HEADERS)
    range_hdr = request.headers.get("range")
    if range_hdr:
        req_hdrs["Range"] = range_hdr

    def _build_stream_response(resp):
        resp_hdrs = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
            "Cache-Control": "public, max-age=86400, s-maxage=86400, immutable",
            "CDN-Cache-Control": "public, max-age=86400",
        }
        for hk, hv in (("Content-Length", "content-length"), ("Content-Range", "content-range"), ("Accept-Ranges", "accept-ranges")):
            val = resp.headers.get(hv)
            if val:
                resp_hdrs[hk] = val

        def _iter_chunks(upstream=resp):
            try:
                for chunk in upstream.iter_content(chunk_size=131072):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        return StreamingResponse(
            _iter_chunks(),
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type") or "video/mp2t",
            headers=resp_hdrs,
        )

    last_status = 502
    # Try primary host with fast TTFB timeout; only race mirrors if primary fails or stalls
    try:
        r0 = _STREAM_SESSION.get(candidate_urls[0], headers=req_hdrs, timeout=(2.0, 5.5), stream=True)
        last_status = r0.status_code
        if r0.status_code in (200, 206):
            return _build_stream_response(r0)
        r0.close()
    except Exception:
        pass

    mirrors = candidate_urls[1:]
    if mirrors:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _try_mirror(target):
            r = _STREAM_SESSION.get(target, headers=req_hdrs, timeout=(2.5, 6.0), stream=True)
            if r.status_code in (200, 206):
                return r
            code = r.status_code
            r.close()
            raise RuntimeError(f"HTTP {code}")

        winner = None
        winner_fut = None
        ex = ThreadPoolExecutor(max_workers=len(mirrors))
        futures = []
        try:
            futures = [ex.submit(_try_mirror, m) for m in mirrors]
            for fut in as_completed(futures):
                try:
                    winner = fut.result()
                    winner_fut = fut
                    break
                except Exception:
                    continue
            if winner is not None:
                for f in futures:
                    if f is not winner_fut:
                        f.add_done_callback(lambda done_f: done_f.result().close() if (not done_f.cancelled() and not done_f.exception()) else None)
                return _build_stream_response(winner)
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    raise HTTPException(status_code=last_status, detail="Failed to fetch video segment")


@router.get("/player/vtt")
def player_vtt(url: str = Query(...)):
    """Proxy WebVTT subtitle tracks with CORS headers so <track> and custom subtitle renderer work cleanly."""
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid VTT URL")

    try:
        r = _STREAM_SESSION.get(url, headers=_STREAM_HEADERS, timeout=10)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="Failed to fetch subtitle file")
        return Response(
            content=r.content,
            media_type="text/vtt; charset=utf-8",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=86400",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Subtitle proxy error: {str(e)}")


@router.get("/player/playlist-segments")
def player_playlist_segments(url: str = Query(...)):
    """
    Parse a specific resolution media playlist (e.g. 1080/index.m3u8) and return all proxied segment URLs
    and total duration for the in-player episode downloader.
    """
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid playlist URL")

    try:
        r = _STREAM_SESSION.get(url, headers=_STREAM_HEADERS, timeout=12)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="Upstream playlist fetch failed")

        parsed_base = urllib.parse.urlparse(url)
        origin = f"{parsed_base.scheme}://{parsed_base.netloc}"

        segments = []
        total_duration = 0.0
        for raw_line in r.text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#EXTINF:"):
                dur_str = line.split(":", 1)[1].split(",")[0].strip()
                try:
                    total_duration += float(dur_str)
                except ValueError:
                    pass
            elif not line.startswith("#"):
                full_url = urllib.parse.urljoin(url, line)
                norm_seg = _normalize_segment_url(full_url, origin)
                segments.append({
                    "raw_url": norm_seg,
                    "url": f"/api/player/segment?url={urllib.parse.quote(norm_seg, safe='')}",
                })

        return {
            "ok": True,
            "count": len(segments),
            "duration": round(total_duration, 2),
            "segments": segments,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Playlist parse failed: {str(e)}")


@router.get("/player/download")
def player_download(url: str = Query(...), filename: str = Query("KuroAnime_Episode.ts")):
    """Direct streaming download endpoint that concatenates MPEG-TS segments on the fly."""
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid playlist URL")

    safe_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', filename)
    if not safe_name.endswith(".ts") and not safe_name.endswith(".mp4"):
        safe_name += ".ts"

    r = _STREAM_SESSION.get(url, headers=_STREAM_HEADERS, timeout=12)
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail="Failed to fetch playlist for download")

    parsed_base = urllib.parse.urlparse(url)
    origin = f"{parsed_base.scheme}://{parsed_base.netloc}"

    seg_urls = []
    for raw_line in r.text.splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            full_url = urllib.parse.urljoin(url, line)
            seg_urls.append(_normalize_segment_url(full_url, origin))

    def _stream_generator():
        for seg_url in seg_urls:
            try:
                sr = _STREAM_SESSION.get(seg_url, headers=_STREAM_HEADERS, timeout=15)
                if sr.status_code == 200 and sr.content:
                    yield sr.content
            except Exception:
                continue

    return StreamingResponse(
        _stream_generator(),
        media_type="video/mp2t",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.get("/clean-stream", response_class=HTMLResponse)
def clean_stream(url: str = Query(...)):
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid stream URL")
    
    try:
        resp = _STREAM_SESSION.get(
            url,
            headers={
                "User-Agent": _STREAM_HEADERS["User-Agent"],
                "Referer": "https://hianime.at/",
            },
            timeout=10,
        )
        if resp.status_code != 200 or not resp.text:
            raise RuntimeError(f"Upstream embed returned HTTP {resp.status_code}")
        html = resp.text

        parsed = urllib.parse.urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        # 1. Strip the ad network container (<div id="adbar">...</div>)
        html = re.sub(r'<div id=["\']adbar["\'].*?</div>', '', html, flags=re.DOTALL | re.IGNORECASE)

        # 2. Strip any ad script tags (butchestusker, popads, etc.)
        html = re.sub(r'<script[^>]*src=["\'][^"\']*(butchestusker|popads|propeller|adsterra|monetag|onclick|syndication|click|banner)[^"\']*["\'][^>]*></script>', '', html, flags=re.IGNORECASE)

        # 3. Inject base href so relative assets resolve cleanly to host origin
        if "<head>" in html:
            html = html.replace("<head>", f'<head><base href="{origin}/">')
        elif "<html" in html:
            html = re.sub(r'(<html[^>]*>)', rf'\1<head><base href="{origin}/"></head>', html, flags=re.IGNORECASE)

        # 4. Inject bulletproof popup defuser script inside player DOM
        cleaner_script = """
        <script>
        (function() {
          var dummy = { closed: false, close: function(){}, focus: function(){}, blur: function(){}, postMessage: function(){} };
          try {
            Object.defineProperty(window, 'open', {
              value: function() { console.warn('[AdShield] Blocked in-player popup.'); return dummy; },
              writable: false,
              configurable: false
            });
          } catch(e) { window.open = function() { return dummy; }; }
          try { if (window.top && window.top !== window) window.top.open = window.open; } catch(e){}
          try { if (window.parent && window.parent !== window) window.parent.open = window.open; } catch(e){}
          document.addEventListener('click', function(e) {
            var a = e.target.closest ? e.target.closest('a') : null;
            if (a && (a.target === '_blank' || !a.href.startsWith(location.origin))) {
              e.preventDefault();
              e.stopPropagation();
              console.warn('[AdShield] Blocked popup anchor link.');
            }
          }, true);
        })();
        </script>
        """
        if "</head>" in html:
            html = html.replace("</head>", cleaner_script + "</head>")
        else:
            html = cleaner_script + html

        return HTMLResponse(content=html, media_type="text/html")
    except Exception as e:
        safe_err = _html.escape(str(e)[:180])
        fallback_html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  html,body{{margin:0;width:100%;height:100%;background:#0b0d14;color:#eef0f5;font-family:Inter,system-ui,sans-serif;display:flex;align-items:center;justify-content:center;text-align:center}}
  .box{{max-width:380px;padding:24px;border-radius:12px;background:#12151e;border:1px solid rgba(238,240,245,0.08)}}
  h3{{margin:0 0 8px;font-size:16px;font-weight:600}}
  p{{margin:0 0 16px;font-size:13px;line-height:1.5;color:#a3a9b8}}
  button{{background:#e50914;color:#fff;border:none;border-radius:8px;padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer}}
</style></head><body>
  <div class="box">
    <h3>Stream server unavailable</h3>
    <p>This video mirror did not respond cleanly ({safe_err}). Try another server below or retry.</p>
    <button onclick="location.reload()">Retry stream</button>
  </div>
  <script>try{{window.parent&&window.parent.postMessage({{type:'kuro-stream-error'}},'*');}}catch(e){{}}</script>
</body></html>"""
        return HTMLResponse(content=fallback_html, status_code=502)



# ─── Prince Vegeta High-Level AI Anime Brain (5,564 Catalog Knowledge) ───────

_VEGETA_KB = None

COMMON_ALIASES = {
    "aot": "attack on titan",
    "snk": "shingeki no kyojin",
    "jjk": "jujutsu kaisen",
    "op": "one piece",
    "mha": "my hero academia",
    "bnha": "boku no hero academia",
    "fmab": "fullmetal alchemist brotherhood",
    "fma": "fullmetal alchemist",
    "csm": "chainsaw man",
    "dbz": "dragon ball z",
    "dbs": "dragon ball super",
    "db": "dragon ball",
    "sao": "sword art online",
    "hxh": "hunter x hunter",
    "tybw": "bleach thousand year blood war",
    "opm": "one punch man",
    "kny": "demon slayer kimetsu no yaiba",
    "ds": "demon slayer kimetsu no yaiba",
    "demon slayer": "demon slayer kimetsu no yaiba",
    "kimetsu no yaiba": "demon slayer kimetsu no yaiba",
    "shingeki no kyojin": "attack on titan",
    "snk": "attack on titan",
    "fate stay night": "fate stay night",
    "monogatari": "bakemonogatari",
    "tg": "tokyo ghoul",
    "code geass": "code geass",
    "rezero": "re zero",
    "re zero": "re zero starting life in another world",
    "danmachi": "is it wrong to try to pick up girls in a dungeon",
    "tensura": "that time i got reincarnated as a slime",
    "slime": "that time i got reincarnated as a slime",
    "konosuba": "konosuba",
    "oregairu": "my teen romantic comedy snafu",
    "bunny girl": "rascal does not dream of bunny girl senpai",
    "jjba": "jojo's bizarre adventure",
    "jojo": "jojo's bizarre adventure",
    "eva": "neon genesis evangelion",
    "nge": "neon genesis evangelion",
    "bsd": "bungo stray dogs",
    "blue lock": "blue lock",
    "solo leveling": "solo leveling",
    "frieren": "frieren beyond journey's end",
    "apothecary diaries": "the apothecary diaries",
    "dandadan": "dandadan",
    "vinland": "vinland saga",
    "edgerunners": "cyberpunk edgerunners",
    "steins gate": "steins gate",
    "monster": "monster",
    "death note": "death note",
    "naruto": "naruto",
    "bleach": "bleach",
    "gintama": "gintama",
    "dr stone": "dr stone",
    "mob psycho": "mob psycho 100",
    "haikyuu": "haikyu",
    "haikyu": "haikyu",
    "black clover": "black clover",
    "hells paradise": "hell's paradise",
    "jigokuraku": "hell's paradise",
    "fate": "fate stay night",
    "clannad": "clannad",
    "your lie in april": "your lie in april",
    "cowboy bebop": "cowboy bebop",
    "berserk": "berserk",
    "violet evergarden": "violet evergarden",
}

GENRE_MAP = {
    "action": ["action"],
    "adventure": ["adventure"],
    "comedy": ["comedy", "funny", "humor", "gag", "hilarious"],
    "drama": ["drama", "dramatic", "emotional", "sad"],
    "fantasy": ["fantasy"],
    "dark fantasy": ["dark fantasy", "grimdark"],
    "urban fantasy": ["urban fantasy"],
    "romance": ["romance", "romantic", "love story"],
    "sci-fi": ["sci-fi", "scifi", "science fiction", "futuristic"],
    "cyberpunk": ["cyberpunk"],
    "slice of life": ["slice of life", "sol", "wholesome", "everyday", "relaxing", "chill"],
    "supernatural": ["supernatural", "paranormal"],
    "shounen": ["shounen", "shonen", "battle shounen"],
    "seinen": ["seinen", "mature"],
    "shoujo": ["shoujo", "shojo"],
    "josei": ["josei"],
    "school": ["school", "high school", "academy"],
    "mystery": ["mystery", "investigation"],
    "detective": ["detective", "police", "crime"],
    "sports": ["sports", "sport", "athletic", "basketball", "soccer", "volleyball", "boxing"],
    "psychological": ["psychological", "mind game", "mind games"],
    "suspense": ["suspense", "thriller"],
    "horror": ["horror", "scary", "creepy"],
    "mecha": ["mecha", "robot", "robots", "gundam"],
    "music": ["music", "band", "musical", "singing", "idol", "idols"],
    "historical": ["historical", "period", "ancient", "feudal", "history"],
    "martial arts": ["martial arts", "kung fu", "karate", "hand to hand"],
    "samurai": ["samurai", "sword", "swordsman", "katana"],
    "military": ["military", "war", "army", "soldier"],
    "super power": ["super power", "super powers", "superpower", "superpowers"],
    "isekai": ["isekai", "another world", "transported to another world", "reincarnated"],
    "reincarnation": ["reincarnation"],
    "magic": ["magic", "magical", "sorcery", "wizard", "witch"],
    "mahou shoujo": ["mahou shoujo", "magical girl", "magical girls"],
    "game": ["game", "gaming", "video game", "mmo", "mmorpg", "vrmmo"],
    "parody": ["parody", "satire", "spoof"],
    "gore": ["gore", "bloody", "violent", "violence"],
    "survival": ["survival", "death game"],
    "demons": ["demons", "demon", "devil"],
    "vampire": ["vampire", "vampires"],
    "space": ["space", "galaxy", "intergalactic"],
    "time travel": ["time travel", "time loop", "time leaping"],
    "ecchi": ["ecchi"],
    "harem": ["harem", "reverse harem"],
    "iyashikei": ["iyashikei", "healing"],
    "workplace": ["workplace", "office", "working"],
    "cgdct": ["cgdct", "cute girls"],
}

KNOWN_STUDIOS = [
    "mappa", "ufotable", "madhouse", "bones", "wit studio", "toei animation",
    "pierrot", "cloverworks", "a-1 pictures", "kyoto animation", "trigger",
    "sunrise", "production i.g", "david production", "studio bind", "white fox",
    "shaft", "j.c.staff", "science saru", "comix wave", "lerche", "tms entertainment",
    "kinema citrus", "p.a. works", "brain's base", "eight bit", "silver link"
]

STOP_WORDS = {
    "what", "which", "where", "when", "who", "whom", "whose", "why", "how",
    "anime", "show", "series", "watch", "good", "best", "top", "great", "peak",
    "recommend", "recommendation", "recommendations", "suggest", "suggestion",
    "tell", "about", "info", "information", "details", "explain", "summary",
    "synopsis", "plot", "story", "like", "similar", "some", "any", "give",
    "find", "search", "looking", "want", "need", "please", "vegeta", "prince",
    "have", "does", "website", "kuroanime", "site", "catalog", "with", "from",
    "that", "this", "there", "their", "they", "them", "worth", "watching",
    "episodes", "episode", "many", "much", "rating", "score", "tier", "most",
    "more", "very", "really", "super", "ultra", "list", "names", "title", "titles"
}


def _clean_html(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>", " ", raw, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\(Source:.*?\)", "", text, flags=re.I)
    text = re.sub(r"\[Written by.*?\]", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_str(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"[^a-z0-9\s]", " ", s.lower()).strip()



KNOWN_GENRES = list(GENRE_MAP.keys())

def get_vegeta_knowledge_base(conn_or_fn=None):
    global _VEGETA_KB
    if _VEGETA_KB is not None:
        return _VEGETA_KB

    should_close = False
    if hasattr(conn_or_fn, "cursor"):
        conn = conn_or_fn
    elif callable(conn_or_fn):
        conn = conn_or_fn()
        should_close = True
    else:
        conn = db()
        should_close = True

    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM episodes")
        total_episodes = c.fetchone()[0]

        # 1. Fetch all anime details
        c.execute("""
            SELECT anime_id, title, title_english, title_romaji, synonyms,
                   type, status, premiered, season, season_year,
                   mal_score, anilist_score, anilist_popularity,
                   studios, duration, episode_count, genres, al_genres,
                   synopsis, al_description, seasons_json, anilist_status,
                   anilist_id
            FROM anime
        """)
        kb_rows = c.fetchall()

        # 2. Fetch all season relations
        c.execute("""
            SELECT parent_anime_id, season_name, season_anime_id, is_current
            FROM anime_seasons
            ORDER BY parent_anime_id, id
        """)
        season_rows = c.fetchall()
    finally:
        if should_close:
            conn.close()

    # Build parent-to-seasons and child-to-parent indexes
    parent_to_seasons = {}
    child_to_parent = {}
    for pid, sname, sid, is_cur in season_rows:
        if pid not in parent_to_seasons:
            parent_to_seasons[pid] = []
        parent_to_seasons[pid].append({
            "season_name": sname,
            "anime_id": sid,
            "is_current": bool(is_cur),
        })
        child_to_parent[sid] = pid

    by_id = {}
    by_anilist_id = {}
    franchise_map = {}
    items = []

    for r in kb_rows:
        a_id = r[0]
        title = (r[1] or "").strip()
        title_eng = (r[2] or title).strip()
        title_rom = (r[3] or "").strip()
        synonyms = (r[4] or "").strip()
        a_type = (r[5] or "TV").strip()
        status = (r[6] or "Finished Airing").strip()
        _al_status = (r[21] or "").upper() if len(r) > 21 else ""
        if _al_status in ("FINISHED", "CANCELLED"):
            status = "Finished Airing"
        elif _al_status == "NOT_YET_RELEASED":
            status = "Not Yet Aired"
        premiered = (r[7] or "").strip()
        season = (r[8] or "").strip()
        season_year = r[9]

        mal_raw = r[10]
        al_raw = r[11]
        pop = r[12] or 0

        score = 0.0
        if al_raw:
            try:
                score = float(al_raw) / 10.0 if float(al_raw) > 10 else float(al_raw)
            except Exception:
                pass
        if score == 0.0 and mal_raw:
            try:
                score = float(mal_raw)
            except Exception:
                pass
        score = round(score, 1) if score > 0 else 7.5

        studios = (r[13] or "Animation Studio").strip()
        duration = (r[14] or "24m").strip()
        ep_count = r[15] or 1

        raw_genres = f"{r[16] or ''}, {r[17] or ''}"
        g_tokens = []
        for g in re.split(r"[,/]", raw_genres):
            g = g.strip()
            if g and g not in g_tokens:
                g_tokens.append(g)
        if len(g_tokens) <= 1 and r[16]:
            low_g = r[16].lower()
            for kg in KNOWN_GENRES:
                if kg in low_g and kg.title() not in g_tokens:
                    g_tokens.append(kg.title())

        genres_display = ", ".join(g_tokens[:5]) if g_tokens else "Action, Adventure"
        genres_lower = (" ".join(g_tokens) + " " + raw_genres).lower()

        syn_s1 = _clean_html(r[18] or "")
        syn_al = _clean_html(r[19] or "")
        syn_clean = syn_al if len(syn_al) > len(syn_s1) else (syn_s1 or syn_al)
        syn_combined_lower = f"{syn_s1} {syn_al}".strip().lower()
        year_str = str(season_year) if season_year else ""
        if not year_str and premiered:
            m = re.findall(r"\d{4}", premiered)
            if m:
                year_str = m[0]

        all_titles_norm = [
            _normalize_str(title),
            _normalize_str(title_eng),
            _normalize_str(title_rom),
        ]
        if synonyms:
            for s_part in synonyms.split(","):
                ns = _normalize_str(s_part)
                if ns:
                    all_titles_norm.append(ns)
        # Add delimiter-split and season-stripped base variants
        for raw_t in [title, title_eng, title_rom]:
            if not raw_t:
                continue
            for sep in [":", " - ", " – ", "—", "("]:
                if sep in raw_t:
                    base = raw_t.split(sep)[0].strip()
                    if len(base) >= 3:
                        nb = _normalize_str(base)
                        if nb and nb not in all_titles_norm:
                            all_titles_norm.append(nb)
            base_season = re.sub(r"\b(season\s*\d+|part\s*\d+|cour\s*\d+|arc|movie|\d+(st|nd|rd|th)\s*season)\b.*$", "", raw_t, flags=re.I).strip()
            if len(base_season) >= 3:
                nsb = _normalize_str(base_season)
                if nsb and nsb not in all_titles_norm:
                    all_titles_norm.append(nsb)
        all_titles_norm = [t for t in dict.fromkeys(all_titles_norm) if t]

        fkey = _franchise_key(title_eng or title)

        item = {
            "anime_id": a_id,
            "title": title,
            "title_english": title_eng,
            "title_romaji": title_rom,
            "synonyms": synonyms,
            "all_titles_norm": all_titles_norm,
            "franchise": fkey,
            "type": a_type,
            "status": status,
            "year": year_str,
            "season_year": int(year_str) if year_str.isdigit() else 0,
            "premiered": premiered,
            "score": score,
            "popularity": pop,
            "studios": studios,
            "studios_lower": studios.lower(),
            "duration": duration,
            "episode_count": ep_count,
            "genres": genres_display,
            "genres_lower": genres_lower,
            "synopsis": syn_clean,
            "synopsis_lower": syn_combined_lower,
            "seasons_json": r[20],
        }
        al_id = r[22] if len(r) > 22 else None
        item["anilist_id"] = al_id
        by_id[a_id] = item
        if al_id:
            try:
                by_anilist_id[int(al_id)] = item
            except Exception:
                pass
        if fkey not in franchise_map:
            franchise_map[fkey] = []
        franchise_map[fkey].append(item)
        items.append(item)

    # Resolve complete seasons for every single anime
    for it in items:
        aid = it["anime_id"]
        pid = child_to_parent.get(aid, aid)
        raw_seasons = parent_to_seasons.get(pid, [])
        resolved_seasons = []

        if raw_seasons:
            for s in raw_seasons:
                s_aid = s["anime_id"]
                target = by_id.get(s_aid)
                resolved_seasons.append({
                    "season_name": s["season_name"],
                    "anime_id": s_aid,
                    "title": (target["title_english"] if target else s["season_name"]) or s["season_name"],
                    "episodes": target["episode_count"] if target else 1,
                    "format": target["type"] if target else "TV",
                    "score": target["score"] if target else 8.0,
                    "status": target["status"] if target else "Finished Airing",
                    "year": target["year"] if target else "",
                    "is_current": s["is_current"] or (s_aid == aid),
                })
        elif it.get("seasons_json"):
            try:
                parsed = json.loads(it["seasons_json"])
                for s in parsed:
                    s_aid = s.get("season_anime_id")
                    if s_aid:
                        target = by_id.get(s_aid)
                        resolved_seasons.append({
                            "season_name": s.get("season_name", "Season"),
                            "anime_id": s_aid,
                            "title": (target["title_english"] if target else s.get("season_name", "Season")) or s.get("season_name", "Season"),
                            "episodes": target["episode_count"] if target else 1,
                            "format": target["type"] if target else "TV",
                            "score": target["score"] if target else 8.0,
                            "status": target["status"] if target else "Finished Airing",
                            "year": target["year"] if target else "",
                            "is_current": bool(s.get("is_current")) or (s_aid == aid),
                        })
            except Exception:
                pass

        if len(resolved_seasons) <= 1:
            peers = [p for p in franchise_map.get(it["franchise"], []) if p["anime_id"] != aid]
            it["related_franchise"] = peers
        else:
            it["related_franchise"] = []

        it["seasons"] = resolved_seasons

    # Sort items by score and popularity so top anime rank highest by default
    items.sort(key=lambda x: (x["score"], x["popularity"]), reverse=True)

    _VEGETA_KB = {
        "items": items,
        "by_id": by_id,
        "by_anilist_id": by_anilist_id,
        "parent_to_seasons": parent_to_seasons,
        "child_to_parent": child_to_parent,
        "total_anime": len(items),
        "total_episodes": total_episodes,
    }
    return _VEGETA_KB


def _franchise_key(title: str) -> str:
    t = title.lower()
    t = re.sub(r"\b(\\d+(st|nd|rd|th)?\\s+(season|part|cour)|season\\s*\\d+|part\\s*\\d+|final season|movie|film|ova|special)\b.*", "", t)
    t = re.sub(r":.*", "", t)
    t = re.sub(r"\s+-\\s+.*", "", t)
    t = re.sub(r"\b(season|part|cour|stage|final|act|chapter)\b.*", "", t)
    t = re.sub(r"[^a-z0-9]", "", t)
    return t[:16] or title.lower()[:16]


def _clean_base_title(t: str) -> str:
    t = re.sub(r"\b(\\d+(st|nd|rd|th)?\\s+(season|part|cour)|season\\s*\\d+|part\\s*\\d+|final season|movie|film|ova|special)\b.*", "", (t or "").lower())
    t = re.sub(r":.*", "", t)
    t = re.sub(r"\s+-\\s+.*", "", t)
    return re.sub(r"[^a-z0-9]", "", t)[:14]


def _clean_genres_str(raw_str: str) -> str:
    low = (raw_str or "").lower()
    found = []
    for g in GENRE_MAP.keys():
        if re.search(rf"\b{re.escape(g)}\b", low):
            found.append(g.title())
    return ", ".join(found[:5]) if found else "Action, Adventure"


def _detect_query_genres(msg: str):
    low = msg.lower()
    detected = []
    for g, syns in sorted(GENRE_MAP.items(), key=lambda x: -len(x[0])):
        for s in syns:
            if re.search(rf"\b{re.escape(s)}\b", low):
                if g not in detected:
                    detected.append(g)
                break
    return detected


GENRE_KEYWORDS = set(GENRE_MAP.keys()) | {
    "action", "romance", "comedy", "fantasy", "sports", "drama", "horror",
    "thriller", "mystery", "sci-fi", "scifi", "isekai", "shounen", "shoujo",
    "seinen", "mecha", "adventure", "supernatural", "music", "psychological",
    "slice of life", "dark fantasy", "martial arts"
}


def _is_pure_genre_query(q_str: str) -> bool:
    cleaned = re.sub(r"\b(i want to watch|recommend|give me|popular|best|good|anime|shows?|series|top|some|what|any)\b", "", q_str.lower()).strip()
    return cleaned in GENRE_KEYWORDS or cleaned in GENRE_MAP


_GENERIC_WORDS = {
    "it", "its", "he", "him", "his", "she", "her", "they", "them", "their",
    "this", "that", "these", "those", "anime", "show", "series", "who", "what",
    "which", "how", "why", "where", "when", "does", "is", "are", "have", "has",
    "had", "will", "can", "should", "could", "would", "worth", "watching", "good",
    "bad", "many", "much", "ep", "eps", "episode", "episodes", "season", "seasons",
    "sister", "brother", "mother", "father", "son", "daughter", "friend", "villain",
    "hero", "character", "characters", "story", "plot", "end", "ending", "start",
    "next", "new", "old", "top", "best", "popular", "main", "first", "second",
    "third", "last", "any", "some", "something", "anything", "please", "about",
    "tell", "think", "like", "love", "hate", "recommend", "suggest", "look", "search"
}


def _find_exact_or_fuzzy_title(user_msg: str, kb_items: list):
    """High-precision matching against all 10,700+ anime titles, aliases, and acronyms."""
    q = re.sub(r"[^\w\s]", " ", user_msg.lower()).strip()
    strip_patterns = [
        r"^(tell me about|tell about|can you tell me about|i want to know about|what is|what s|what about|how about|info on|information on|synopsis for|synopsis of|details about|details on|how many episodes in|how many episodes does|who made|who animated|is|are there|can you find|have you got|do you have|watch|stream|search|anime|explain|describe|review of)\s+",
        r"^(what\s+)?(is\s+)?(the\s+)?(watch\s+order|chronological\s+order|order\s+to\s+watch|viewing\s+order|reading\s+order|order|seasons?|episodes?|all\s+seasons?)\s+(of|for|to)?\s*",
        r"^(how\s+to\s+watch|where\s+to\s+start\s+with|how\s+should\s+i\s+watch|what\s+order\s+should\s+i\s+watch)\s*",
    ]
    suffix_patterns = [
        r"\s+(and\s+all\s+(its\s+)?seasons?|and\s+its\s+seasons?|all\s+seasons?|seasons?|watch\s+order|order)$",
        r"\s+(about|anime|show|series|season|have|website|kuroanime|all about|on this site|on here|good|worth watching|worth it|any good)$",
    ]
    for _ in range(3):
        for p in strip_patterns:
            q = re.sub(p, "", q).strip()
        for p in suffix_patterns:
            q = re.sub(p, "", q).strip()

    if not q or len(q) < 2 or _is_pure_genre_query(q):
        return None, []
    if q in _GENERIC_WORDS or all(w in _GENERIC_WORDS for w in q.split()):
        return None, []

    check_phrase = COMMON_ALIASES.get(q, q)
    for alias, full in COMMON_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", check_phrase) and len(alias) >= 3:
            check_phrase = re.sub(rf"\b{re.escape(alias)}\b", full, check_phrase)

    scored = []
    q_words = set(check_phrase.split())

    for it in kb_items:
        best_title_score = 0.0
        is_primary_exact = (_normalize_str(it["title"]) == check_phrase)
        is_eng_exact = (_normalize_str(it["title_english"]) == check_phrase)

        for t in it["all_titles_norm"]:
            if not t:
                continue
            if t == check_phrase:
                best_title_score = max(best_title_score, 100.0)
            elif len(check_phrase) >= 4 and check_phrase in t:
                ratio = len(check_phrase) / max(len(t), 1)
                best_title_score = max(best_title_score, 70.0 + ratio * 20.0)
            elif len(t) >= 4 and len(check_phrase) >= 4 and t in check_phrase:
                ratio = len(t) / max(len(check_phrase), 1)
                best_title_score = max(best_title_score, 65.0 + ratio * 20.0)
            else:
                t_words = set(t.split())
                if q_words and t_words and len(q_words & t_words) >= min(len(q_words), 3) and len(q_words) >= 2:
                    sim = difflib.SequenceMatcher(None, check_phrase, t).ratio()
                    if sim >= 0.65:
                        best_title_score = max(best_title_score, 55.0 + sim * 30.0)

        if best_title_score >= 60.0:
            primary_boost = 30.0 if is_primary_exact else 0.0
            eng_boost = 15.0 if is_eng_exact else 0.0
            t_low_cmp = (it.get("title_english") or it.get("title") or "").lower()
            movie_penalty = -35.0 if ("movie" in it["title"].lower() and "movie" not in check_phrase) else 0.0
            q_wants_sequel = bool(re.search(r"\b(season\s*[2-9]|part\s*[2-9]|2nd|3rd|4th|arc|shippuden|brotherhood|hashira|entertainment|swordsmith|arise from|shibuya|culling|final|cour)\b", check_phrase))
            sequel_penalty = 0.0
            if not q_wants_sequel and re.search(r"\b(season\s*[2-9]|part\s*[2-9]|cour\s*[2-9]|2nd season|3rd season|4th season|hashira training|entertainment district|swordsmith village|arise from the shadow|chronicle|recap|ova)\b", t_low_cmp):
                sequel_penalty = -18.0
            tv_boost = 10.0 if it["type"] == "TV" else 0.0
            pop_boost = min((it["popularity"] or 0) / 25000.0, 45.0)
            ep_boost = min((it["episode_count"] or 1) / 10.0, 12.0)
            score_boost = (it["score"] / 10.0) * 5.0
            total = best_title_score + primary_boost + eng_boost + movie_penalty + sequel_penalty + tv_boost + pop_boost + ep_boost + score_boost
            scored.append((total, it))

    if not scored:
        return None, []

    scored.sort(key=lambda x: x[0], reverse=True)
    best_item = scored[0][1]

    # Collect related adaptations / seasons
    related = []
    best_franchise = best_item["franchise"]
    for sc in scored[1:]:
        cand = sc[1]
        if cand["anime_id"] != best_item["anime_id"] and (cand["franchise"] == best_franchise or cand["title_english"].startswith(best_item["title_english"][:10])):
            if cand not in related:
                related.append(cand)
        if len(related) >= 4:
            break

    return best_item, related


_CHAR_VISUAL_CACHE: dict = {"loaded": False, "features": None, "meta": None}
_VEGETA_BRAIN_CACHE: dict = {"loaded": False, "ready": False}
_VG_HAIR_COLOR_ORDER = (
    "pink hair", "black hair", "brown hair", "blonde hair", "blue hair",
    "red hair", "green hair", "white hair", "silver hair", "grey hair",
    "purple hair", "orange hair", "aqua hair",
)
_VG_HAIR_BITS = {tag: (1 << idx) for idx, tag in enumerate(_VG_HAIR_COLOR_ORDER)}
_VG_HAIR_BRIDGE = {
    "pink hair": [("red hair", 0.40)],
    "red hair": [("pink hair", 0.35), ("orange hair", 0.25)],
    "white hair": [("silver hair", 0.48), ("grey hair", 0.40)],
    "silver hair": [("white hair", 0.48), ("grey hair", 0.42)],
    "grey hair": [("silver hair", 0.40), ("white hair", 0.35)],
    "blue hair": [("aqua hair", 0.45)],
    "aqua hair": [("blue hair", 0.45)],
    "brown eyes": [("black eyes", 0.28)],
    "black eyes": [("brown eyes", 0.25)],
}


def _vg_load_char_brain() -> dict | None:
    """Lazy-load Vegeta's trained sparse TF-IDF anime character appearance brain."""
    if _VEGETA_BRAIN_CACHE["loaded"]:
        return _VEGETA_BRAIN_CACHE if _VEGETA_BRAIN_CACHE["ready"] else None
    _VEGETA_BRAIN_CACHE["loaded"] = True
    try:
        import numpy as np
        base_dir = os.path.dirname(os.path.abspath(__file__))
        cand_paths = [
            os.path.join(base_dir, "vegeta_char_brain.npz"),
            os.path.join(os.getcwd(), "api", "vegeta_char_brain.npz"),
            os.path.join(os.getcwd(), "anime_app", "web", "api", "vegeta_char_brain.npz"),
        ]
        brain_path = next((p for p in cand_paths if os.path.exists(p)), None)
        _, meta = _load_char_visuals()
        if not brain_path or not meta:
            return None
        npz = np.load(brain_path, allow_pickle=False)
        indptr = npz["indptr"].astype(np.int32)
        indices = npz["indices"].astype(np.int32)
        data_f32 = npz["data"].astype(np.float32)
        idf = npz["idf"].astype(np.float32)
        vocab = [str(v) for v in npz["vocab"]]
        gender = npz["gender"].astype(np.int8)
        hair_mask = npz["hair_mask"].astype(np.uint32)
        fav = npz["favourites"].astype(np.float32)
        role = npz["role"].astype(np.float32)
        n_chars = len(indptr) - 1
        if n_chars != len(meta):
            return None
        nnz = np.diff(indptr).astype(np.float32)
        sparsity_scale = np.clip(np.sqrt(nnz / 14.0), 0.25, 1.0).astype(np.float32)
        row_idx = np.repeat(np.arange(n_chars, dtype=np.int32), np.diff(indptr))
        pop_bonus = np.clip(np.log1p(np.maximum(fav, 0.0)) / 10.6, 0.0, 1.0).astype(np.float32)
        _VEGETA_BRAIN_CACHE.update({
            "ready": True,
            "n_chars": n_chars,
            "row_idx": row_idx,
            "indices": indices,
            "data_f32": data_f32,
            "idf": idf,
            "vocab_map": {t: i for i, t in enumerate(vocab)},
            "gender": gender,
            "hair_mask": hair_mask,
            "pop_bonus": pop_bonus,
            "role": role,
            "sparsity_scale": sparsity_scale,
        })
        return _VEGETA_BRAIN_CACHE
    except Exception as err:
        print(f"Character brain load error: {err}")
        return None


def _load_char_visuals() -> tuple:
    """Lazy-load the prebuilt MAL/AniList character visual feature matrix & metadata."""
    if _CHAR_VISUAL_CACHE["loaded"]:
        return _CHAR_VISUAL_CACHE["features"], _CHAR_VISUAL_CACHE["meta"]
    _CHAR_VISUAL_CACHE["loaded"] = True
    try:
        import gzip
        import numpy as np
        base_dir = os.path.dirname(os.path.abspath(__file__))
        npz_path = os.path.join(base_dir, "vegeta_char_visuals.npz")
        meta_path = os.path.join(base_dir, "vegeta_char_visuals_meta.json.gz")
        if not os.path.exists(npz_path) or not os.path.exists(meta_path):
            return None, None
        data = np.load(npz_path)
        features = data["features"].astype(np.float32)
        with gzip.open(meta_path, "rb") as f:
            meta = json.loads(f.read().decode("utf-8"))
        _CHAR_VISUAL_CACHE["features"] = features
        _CHAR_VISUAL_CACHE["meta"] = meta
        return features, meta
    except Exception:
        return None, None


def _extract_artwork_crop(raw_bytes: bytes) -> bytes | None:
    """Strip white/dark browser/phone UI chrome to isolate the inner anime artwork rectangle."""
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(raw_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return None
        H, W = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # Case 1: Screenshot contains a white UI card (e.g. Google Images / Bored Panda / Pinterest)
        white = (gray > 235).astype(np.uint8) * 255
        contours, _ = cv2.findContours(white, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            card = max(contours, key=cv2.contourArea)
            cx, cy, cw, ch = cv2.boundingRect(card)
            card_area = cw * ch
            if 0.12 * W * H <= card_area <= 0.96 * W * H:
                card_img = img[cy:cy + ch, cx:cx + cw]
                card_gray = gray[cy:cy + ch, cx:cx + cw]
                non_white = (card_gray < 230).astype(np.uint8) * 255
                opened = cv2.morphologyEx(non_white, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9)))
                c2, _ = cv2.findContours(opened, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                if c2:
                    art = max(c2, key=cv2.contourArea)
                    ax, ay, aw, ah = cv2.boundingRect(art)
                    if aw >= 50 and ah >= 50 and (aw * ah) >= 0.15 * card_area:
                        crop = card_img[ay:ay + ah, ax:ax + aw]
                        ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 94])
                        if ok:
                            return buf.tobytes()

        # Case 2: Only trim truly uniform solid black/white letterbox or phone borders around edges
        row_std = gray.std(axis=1)
        row_mean = gray.mean(axis=1)
        col_std = gray.std(axis=0)
        col_mean = gray.mean(axis=0)
        is_border_row = (row_std < 6.0) & ((row_mean < 16.0) | (row_mean > 238.0))
        is_border_col = (col_std < 6.0) & ((col_mean < 16.0) | (col_mean > 238.0))
        valid_rows = np.where(~is_border_row)[0]
        valid_cols = np.where(~is_border_col)[0]
        if valid_rows.size > 0 and valid_cols.size > 0:
            y0, y1 = int(valid_rows[0]), int(valid_rows[-1]) + 1
            x0, x1 = int(valid_cols[0]), int(valid_cols[-1]) + 1
            aw, ah = x1 - x0, y1 - y0
            if aw >= 60 and ah >= 60 and (aw * ah) < 0.92 * W * H and (aw * ah) >= 0.18 * W * H:
                crop = img[y0:y1, x0:x1]
                ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 94])
                if ok:
                    return buf.tobytes()
    except Exception:
        pass
    return None


def _extract_visual_descriptor(img_bgr) -> object:
    """Compute the 312-dim L2-normalized visual descriptor matching build_char_visual_db.py."""
    import cv2
    import numpy as np
    if img_bgr is None or img_bgr.size == 0:
        return None
    h, w = img_bgr.shape[:2]
    if h < 20 or w < 20:
        return None
    resized = cv2.resize(img_bgr, (128, 128), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV)
    hist_parts = []
    h_glob = cv2.calcHist([hsv], [0, 1], None, [8, 3], [0, 180, 0, 256]).flatten()
    h_glob = h_glob / (np.linalg.norm(h_glob) + 1e-6)
    hist_parts.append(h_glob * 1.4)
    for y0 in (0, 64):
        for x0 in (0, 64):
            patch = hsv[y0:y0 + 64, x0:x0 + 64]
            hp = cv2.calcHist([patch], [0, 1], None, [8, 3], [0, 180, 0, 256]).flatten()
            hp = hp / (np.linalg.norm(hp) + 1e-6)
            hist_parts.append(hp)

    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.sqrt(gx * gx + gy * gy)
    ang = (np.arctan2(gy, gx) + np.pi) * (8.0 / (2.0 * np.pi))
    bins = np.clip(ang.astype(np.int32), 0, 7)
    grad_parts = []
    for cy in range(4):
        for cx in range(4):
            y0, y1 = cy * 32, (cy + 1) * 32
            x0, x1 = cx * 32, (cx + 1) * 32
            cell_hist = np.bincount(bins[y0:y1, x0:x1].ravel(), weights=mag[y0:y1, x0:x1].ravel(), minlength=8).astype(np.float32)
            grad_parts.append(cell_hist)
    grad_vec = np.concatenate(grad_parts)
    grad_vec = grad_vec / (np.linalg.norm(grad_vec) + 1e-6)

    small_gray = cv2.resize(gray, (8, 8), interpolation=cv2.INTER_AREA).flatten()
    small_gray = small_gray - small_gray.mean()
    small_gray = small_gray / (np.linalg.norm(small_gray) + 1e-6)

    color_vec = np.concatenate(hist_parts).astype(np.float32)
    color_vec = color_vec / (np.linalg.norm(color_vec) + 1e-6)
    full = np.concatenate([color_vec * 1.1, grad_vec * 1.0, small_gray * 0.7]).astype(np.float32)
    return full / (np.linalg.norm(full) + 1e-6)


def _match_character_visual(raw_bytes: bytes, kb: dict, top_k: int = 3) -> list[dict]:
    """Match an uploaded character image against the MAL/AniList character visual index."""
    features, meta = _load_char_visuals()
    if features is None or not meta:
        return []
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(raw_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return []

        h, w = img.shape[:2]
        crops = [img]
        # Also test upper-center portrait crop if image is full-body / wide
        if h >= 60 and w >= 60:
            crops.append(img[0:int(h * 0.65), int(w * 0.15):int(w * 0.85)])

        best_scores = np.zeros(len(meta), dtype=np.float32)
        for c in crops:
            vec = _extract_visual_descriptor(c)
            if vec is not None:
                scores = features @ vec
                best_scores = np.maximum(best_scores, scores)

        top_idx = np.argsort(best_scores)[::-1][:top_k]
        results = []
        for idx in top_idx:
            score = float(best_scores[idx])
            m = meta[int(idx)]
            matched_item = None
            al_id = m.get("al")
            mal_id = m.get("mal")
            if al_id and kb.get("by_anilist_id"):
                matched_item = kb["by_anilist_id"].get(al_id)
            if not matched_item and mal_id and kb.get("by_mal_id"):
                matched_item = kb["by_mal_id"].get(mal_id)
            if not matched_item and m.get("t"):
                matched_item = kb.get("by_title", {}).get(_normalize_str(m["t"]))
            results.append({
                "score": score,
                "character": m.get("n", ""),
                "role": m.get("r", "S"),
                "anime_title": m.get("t", ""),
                "anilist_id": al_id,
                "mal_id": mal_id,
                "matched_item": matched_item,
            })
        return results
    except Exception:
        return []


_VG_WD_TAGGER_BACKENDS = [
    ("https://smilingwolf-wd-tagger.hf.space", "SmilingWolf/wd-swinv2-tagger-v3", 0.12, False, 0.22, False),
    ("https://ashen-sensored-wd-eva02-tagger-2026-canary.hf.space", "ashen-sensored/wd-eva02-tagger-2026-canary", 0.14, False, 0.25, False),
    ("https://smilingwolf-wd-tagger.hf.space", "SmilingWolf/wd-vit-tagger-v3", 0.12, False, 0.22, False),
]

_VG_CHAR_SORTED_CACHE: dict = {"ready": False, "map": {}}
_VG_GENERIC_WD_TAGS = {
    "1girl", "1boy", "2girls", "2boys", "multiple girls", "multiple boys", "solo", "looking at viewer",
    "smile", "open mouth", "blush", "short hair", "long hair", "black hair", "brown hair", "blonde hair",
    "blue hair", "red hair", "white hair", "silver hair", "green hair", "pink hair", "purple hair",
    "blue eyes", "red eyes", "brown eyes", "green eyes", "yellow eyes", "purple eyes", "upper body",
    "portrait", "simple background", "white background", "black background", "blurry", "english text",
    "japanese text", "fake screenshot", "text focus", "speech bubble", "phone", "smartphone", "cellphone",
    "shirt", "jacket", "coat", "dress", "skirt", "pants", "gloves", "hat", "scar", "sword", "weapon",
    "fire", "smoke", "building", "sky", "cloud", "night", "dark", "outdoors", "indoors", "monochrome",
    "greyscale", "comic", "chibi", "full body", "cowboy shot", "close-up", "profile", "from behind",
    "male focus", "female focus", "breasts", "cleavage", "teeth", "holding", "standing", "sitting",
}


def _vg_prepare_vision_jpeg(raw_bytes: bytes, max_dim: int = 960) -> bytes:
    """Normalize arbitrary image bytes (PNG/WebP/large phone shots) into a compact JPEG for fast API upload."""
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(raw_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return raw_bytes
        h, w = img.shape[:2]
        if max(h, w) > max_dim:
            scale = max_dim / float(max(h, w))
            img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        if ok:
            return buf.tobytes()
    except Exception:
        pass
    return raw_bytes


def _vg_query_trace_moe(payload_bytes: bytes) -> list[dict]:
    """Query trace.moe with border auto-cropping enabled."""
    import urllib.request as _urllib_req
    req = _urllib_req.Request(
        "https://api.trace.moe/search?cutBorders",
        data=payload_bytes,
        headers={
            "Content-Type": "image/jpeg",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
    )
    with _urllib_req.urlopen(req, timeout=9) as res:
        data = json.loads(res.read().decode("utf-8"))
        return data.get("result") or []


def _vg_fold_romanization(token: str) -> str:
    """Fold Japanese macron variants (ou->o, uu->u) for character-name matching."""
    t = re.sub(r"[^a-z0-9]", "", token.lower())
    t = t.replace("ou", "o").replace("uu", "u")
    return t


def _vg_get_sorted_char_index(kb: dict) -> dict:
    """Build and cache token-sorted character lookup map to match Family-Given and Given-Family orders."""
    char_idx = kb.get("character_index") or {}
    if _VG_CHAR_SORTED_CACHE["ready"] and _VG_CHAR_SORTED_CACHE["map"]:
        return _VG_CHAR_SORTED_CACHE["map"]
    sorted_map: dict = {}
    for ck, entries in char_idx.items():
        toks = [t for t in ck.split() if len(t) >= 2] or ck.split()
        if len(toks) >= 2:
            k_norm = " ".join(sorted(toks))
            k_fold = " ".join(sorted(_vg_fold_romanization(t) for t in toks))
            sorted_map.setdefault(k_norm, []).extend(entries[:3])
            if k_fold != k_norm:
                sorted_map.setdefault(k_fold, []).extend(entries[:3])
        elif len(toks) == 1:
            k_fold = _vg_fold_romanization(toks[0])
            if k_fold != toks[0]:
                sorted_map.setdefault(k_fold, []).extend(entries[:3])
    for k in sorted_map:
        sorted_map[k].sort(key=lambda x: (1 if x[1] == "M" else 0, x[2].get("popularity") or 0), reverse=True)
    if char_idx:
        _VG_CHAR_SORTED_CACHE["ready"] = True
        _VG_CHAR_SORTED_CACHE["map"] = sorted_map
    return sorted_map


def _vg_call_gradio_tagger(base_url: str, model_repo: str, g_thresh: float, g_mcut: bool, c_thresh: float, c_mcut: bool, img_bytes: bytes, wall_timeout: float = 18.0) -> dict | None:
    """Invoke a CPU Gradio WD-Tagger Space via REST + SSE without any API key."""
    try:
        t0 = _time.time()
        up = _STREAM_SESSION.post(
            f"{base_url}/gradio_api/upload",
            files={"files": ("frame.jpg", img_bytes, "image/jpeg")},
            timeout=8,
        )
        if not up.ok:
            return None
        uploaded = up.json()
        if not uploaded:
            return None
        call_res = _STREAM_SESSION.post(
            f"{base_url}/gradio_api/call/predict",
            json={
                "data": [
                    {"path": uploaded[0], "meta": {"_type": "gradio.FileData"}},
                    model_repo,
                    g_thresh,
                    g_mcut,
                    c_thresh,
                    c_mcut,
                ]
            },
            timeout=8,
        )
        if not call_res.ok:
            return None
        eid = (call_res.json() or {}).get("event_id")
        if not eid:
            return None
        with _STREAM_SESSION.get(f"{base_url}/gradio_api/call/predict/{eid}", stream=True, timeout=max(6.0, wall_timeout)) as ev:
            if not ev.ok:
                return None
            last_event = ""
            for raw_ln in ev.iter_lines(decode_unicode=True):
                if _time.time() - t0 > wall_timeout:
                    break
                if not raw_ln:
                    continue
                if raw_ln.startswith("event:"):
                    last_event = raw_ln[6:].strip()
                    if last_event == "error":
                        return None
                elif raw_ln.startswith("data:") and last_event == "complete":
                    payload = json.loads(raw_ln[5:].strip())
                    if isinstance(payload, list) and len(payload) >= 4:
                        chars = []
                        if isinstance(payload[2], dict):
                            chars = payload[2].get("confidences") or []
                            if not chars and payload[2].get("label"):
                                chars = [{"label": payload[2]["label"], "confidence": 0.75}]
                        tag_scores = []
                        tags = []
                        if isinstance(payload[3], dict):
                            for x in (payload[3].get("confidences") or []):
                                lbl = str(x.get("label") or "").strip().lower()
                                conf_val = float(x.get("confidence") or 0.0)
                                if lbl:
                                    tag_scores.append((lbl, conf_val))
                                    if conf_val >= 0.28:
                                        tags.append(lbl)
                        return {
                            "characters": [
                                {"label": str(c.get("label") or "").strip(), "confidence": float(c.get("confidence") or 0.0)}
                                for c in chars if c.get("label")
                            ],
                            "tags": tags,
                            "tag_scores": tag_scores,
                            "engine": model_repo,
                        }
                    break
    except Exception as err:
        print(f"WD-Tagger error ({model_repo}): {err}")
    return None


def _vg_hf_wd_tagger_identify(img_bytes: bytes) -> dict | None:
    """Identify anime characters and visual tags using zero-key CPU HuggingFace WD-Tagger Spaces."""
    norm_jpg = _vg_prepare_vision_jpeg(img_bytes, max_dim=768)
    for base_url, model_repo, g_th, g_mc, c_th, c_mc in _VG_WD_TAGGER_BACKENDS:
        out = _vg_call_gradio_tagger(base_url, model_repo, g_th, g_mc, c_th, c_mc, norm_jpg, wall_timeout=18.0)
        if out and (out.get("characters") or out.get("tags")):
            return out
    return None


def _vg_anilist_char_lookup(char_name: str, kb: dict) -> tuple[str, object | None]:
    """Resolve a character name via free AniList GraphQL into a KuroAnime catalog item."""
    query = """
    query ($s: String) {
      Page(perPage: 4) {
        characters(search: $s, sort: FAVOURITES_DESC) {
          name { full }
          media(sort: POPULARITY_DESC, perPage: 3, type: ANIME) {
            nodes { id idMal title { romaji english } }
          }
        }
      }
    }
    """
    candidates = [char_name]
    parts = char_name.split()
    if len(parts) == 2:
        candidates.append(f"{parts[1]} {parts[0]}")
    by_al = kb.get("by_anilist_id") or {}
    by_mal = kb.get("by_mal_id") or {}
    for q_name in candidates:
        try:
            r = _STREAM_SESSION.post(
                "https://graphql.anilist.co",
                json={"query": query, "variables": {"s": q_name}},
                timeout=4,
            )
            if not r.ok:
                continue
            chars = (((r.json() or {}).get("data") or {}).get("Page") or {}).get("characters") or []
            for ch in chars:
                full_name = ((ch.get("name") or {}).get("full") or q_name).strip()
                nodes = ((ch.get("media") or {}).get("nodes") or [])
                for m in nodes:
                    al_id = m.get("id")
                    mal_id = m.get("idMal")
                    it = by_al.get(int(al_id)) if al_id and int(al_id) in by_al else None
                    if not it and mal_id and int(mal_id) in by_mal:
                        it = by_mal[int(mal_id)]
                    if not it:
                        m_titles = m.get("title") or {}
                        for mt in (m_titles.get("english"), m_titles.get("romaji")):
                            if mt:
                                it, _ = _find_exact_or_fuzzy_title(mt, kb["items"])
                                if it:
                                    break
                    if it:
                        return full_name, it
        except Exception:
            continue
    return char_name, None


def _vg_anime_entry_priority(it: dict) -> tuple:
    """Rank canonical base TV series above 1-episode stubs, OVAs, or sequels when multiple entries match."""
    t_low = ((it.get("title_english") or "") + " " + (it.get("title") or "")).lower()
    ep_cnt = int(it.get("episode_count") or 0)
    is_tv = 1 if (it.get("type") or "").upper() == "TV" else 0
    is_sequel = 1 if re.search(r"\b(season\s*[2-9]|part\s*[2-9]|2nd season|3rd season|4th season|cour\s*[2-9]|final season|film|movie|ova|special|recap)\b", t_low) else 0
    multi_ep = 1 if ep_cnt > 1 else 0
    return (is_tv, multi_ep, 1 - is_sequel, min(ep_cnt, 500), int(it.get("popularity") or 0))


def _vg_lookup_char_in_kb(char_label: str, kb: dict) -> tuple[str, str | None, object | None, float]:
    """Resolve a WD-Tagger character label (e.g. 'sanji (one piece)') into display name and KuroAnime item."""
    raw = char_label.strip().lower().replace("_", " ")
    m_par = re.match(r"^(.+?)\s*\(([^()]+)\)\s*$", raw)
    base_name = (m_par.group(1) if m_par else raw).strip()
    series_hint = (m_par.group(2) if m_par else "").strip()

    series_item = None
    if series_hint:
        clean_series = re.sub(r"[-_:]", " ", series_hint).strip()
        series_item, _ = _find_exact_or_fuzzy_title(clean_series, kb["items"])

    char_idx = kb.get("character_index") or {}
    sorted_idx = _vg_get_sorted_char_index(kb)

    clean_key = re.sub(r"[^a-z0-9\s]", " ", base_name).strip()
    clean_key = re.sub(r"\s+", " ", clean_key)
    toks = [t for t in clean_key.split() if len(t) >= 2] or clean_key.split()
    sorted_key = " ".join(sorted(toks))
    folded_sorted = " ".join(sorted(_vg_fold_romanization(t) for t in toks)) if toks else ""
    folded_single = _vg_fold_romanization(toks[0]) if len(toks) == 1 else ""

    hits = []
    for k in (clean_key, " ".join(toks), sorted_key, folded_sorted, folded_single):
        if not k:
            continue
        if k in char_idx:
            hits.extend(char_idx[k][:8])
        if k in sorted_idx:
            hits.extend(sorted_idx[k][:8])

    hits.sort(
        key=lambda x: (1 if x[1] == "M" else 0, *_vg_anime_entry_priority(x[2])),
        reverse=True,
    )

    if series_item and hits:
        s_fran = series_item.get("franchise")
        for cn, role, it in hits:
            if it["anime_id"] == series_item["anime_id"] or (s_fran and it.get("franchise") == s_fran):
                return cn, series_hint, it, 1.0

    if series_item:
        return base_name.title(), series_hint, series_item, 0.95

    if hits:
        return hits[0][0], None, hits[0][2], 0.9

    return base_name.title(), series_hint or None, None, 0.0


def _vg_predict_character_from_brain(wd_out: dict, kb: dict, img_bytes: bytes | None = None) -> dict | None:
    """Match an uploaded anime artwork/fanart/closeup against the trained semantic+visual character brain."""
    brain = _vg_load_char_brain()
    feats, meta = _load_char_visuals()
    if not brain or not meta:
        return None
    tag_scores = wd_out.get("tag_scores") or [(t, 0.65) for t in (wd_out.get("tags") or [])]
    if not tag_scores:
        return None

    import numpy as np
    vmap = brain["vocab_map"]
    idf = brain["idf"]
    n_chars = brain["n_chars"]
    q_map: dict[int, float] = {}
    raw_tag_dict: dict[str, float] = {}

    for lbl, conf in tag_scores:
        raw_tag_dict[lbl] = max(raw_tag_dict.get(lbl, 0.0), float(conf))
        if lbl in vmap:
            idx = vmap[lbl]
            q_map[idx] = max(q_map.get(idx, 0.0), float(conf))
        for alt_lbl, mult in _VG_HAIR_BRIDGE.get(lbl, ()):
            if alt_lbl in vmap and raw_tag_dict.get(alt_lbl, 0.0) < conf * mult:
                alt_idx = vmap[alt_lbl]
                q_map[alt_idx] = max(q_map.get(alt_idx, 0.0), float(conf) * mult)

    if len(q_map) < 2:
        return None

    q_vec = np.zeros(len(idf), dtype=np.float32)
    for ti, val in q_map.items():
        q_vec[ti] = val * idf[ti]
    q_norm = float(np.linalg.norm(q_vec))
    if q_norm < 1e-6:
        return None
    q_vec /= q_norm

    cos_sem = np.bincount(
        brain["row_idx"],
        weights=brain["data_f32"] * q_vec[brain["indices"]],
        minlength=n_chars,
    ).astype(np.float32)
    cos_sem = cos_sem * brain["sparsity_scale"]

    score = cos_sem.copy()

    # Gender consistency gate
    p_boy = raw_tag_dict.get("1boy", 0.0) + raw_tag_dict.get("male focus", 0.0)
    p_girl = raw_tag_dict.get("1girl", 0.0) + raw_tag_dict.get("female focus", 0.0)
    if p_boy >= 1.25 and raw_tag_dict.get("1girl", 0.0) < 0.25:
        score = np.where(brain["gender"] == 2, score - 0.35, score)
        score = np.where(brain["gender"] == 1, score + 0.04, score)
    elif p_girl >= 1.25 and raw_tag_dict.get("1boy", 0.0) < 0.25:
        score = np.where(brain["gender"] == 1, score - 0.35, score)
        score = np.where(brain["gender"] == 2, score + 0.04, score)

    # Hair-color affinity with warm/cool family tolerance
    q_hair_exact = 0
    q_hair_compat = 0
    max_hair_p = 0.0
    for htag, hbit in _VG_HAIR_BITS.items():
        hp = raw_tag_dict.get(htag, 0.0)
        if hp >= 0.40:
            max_hair_p = max(max_hair_p, hp)
            q_hair_exact |= hbit
            q_hair_compat |= hbit
            for alt_h, _ in _VG_HAIR_BRIDGE.get(htag, ()):
                q_hair_compat |= _VG_HAIR_BITS.get(alt_h, 0)
    if q_hair_exact:
        hm = brain["hair_mask"]
        has_exact = (hm & q_hair_exact) != 0
        has_compat = (hm & q_hair_compat) != 0
        score = np.where(has_exact, score + 0.11, np.where(has_compat, score + 0.05, score))
        if max_hair_p >= 0.65:
            score = np.where(~has_compat, score - 0.16, score)

    # Visual portrait affinity tie-breaker
    if img_bytes and feats is not None and len(feats) == n_chars:
        try:
            import cv2
            im = cv2.imdecode(np.frombuffer(img_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
            dv = _extract_visual_descriptor(im)
            if dv is not None:
                cos_vis = feats @ dv
                score = score + 0.06 * np.clip(cos_vis, 0.0, 1.0)
        except Exception:
            pass

    score = score + 0.06 * brain["pop_bonus"] + 0.025 * brain["role"]

    top_idxs = np.argsort(score)[::-1][:10]
    by_al = kb.get("by_anilist_id") or {}
    by_mal = kb.get("by_mal_id") or {}
    best_i = int(top_idxs[0])
    if float(cos_sem[best_i]) < 0.24 or float(score[best_i]) < 0.34:
        return None

    def _resolve_meta_item(m_row: dict):
        cands = []
        if m_row.get("al") and int(m_row["al"]) in by_al:
            cands.append(by_al[int(m_row["al"])])
        if m_row.get("mal") and int(m_row["mal"]) in by_mal:
            cands.append(by_mal[int(m_row["mal"])])
        if m_row.get("t"):
            t_it, _ = _find_exact_or_fuzzy_title(m_row["t"], kb["items"])
            if t_it:
                cands.append(t_it)
        if not cands:
            return None
        cands.sort(key=_vg_anime_entry_priority, reverse=True)
        return cands[0]

    m_best = meta[best_i]
    matched_item = _resolve_meta_item(m_best)
    if not matched_item:
        for alt_idx in top_idxs[1:4]:
            cand_m = meta[int(alt_idx)]
            cand_it = _resolve_meta_item(cand_m)
            if cand_it and float(cos_sem[int(alt_idx)]) >= 0.25:
                m_best = cand_m
                matched_item = cand_it
                best_i = int(alt_idx)
                break
    if not matched_item:
        return None

    conf_pct = min(0.96, max(0.72, 0.45 + float(score[best_i]) * 0.65))
    return {
        "score": conf_pct,
        "character": m_best.get("n") or "Identified Character",
        "characters_list": [m_best.get("n") or "Identified Character"],
        "anime_title": matched_item.get("title_english") or matched_item.get("title") or m_best.get("t"),
        "anilist_id": m_best.get("al"),
        "mal_id": m_best.get("mal"),
        "matched_item": matched_item,
        "source": "vegeta-neural-brain",
    }


def _vg_resolve_wd_results(wd_out: dict, kb: dict, img_bytes: bytes | None = None) -> dict | None:
    """Resolve WD-Tagger character & tag predictions into a structured KuroAnime visual match."""
    if not wd_out:
        return None
    raw_chars = wd_out.get("characters") or []
    tags = wd_out.get("tags") or []

    votes: dict[int, dict] = {}
    detected_names: list[tuple[str, float]] = []
    unresolved: list[tuple[str, float]] = []

    for c in raw_chars[:8]:
        lbl = c.get("label") or ""
        conf = float(c.get("confidence") or 0.0)
        if not lbl or conf < 0.25:
            continue
        disp_name, s_hint, it, rel_w = _vg_lookup_char_in_kb(lbl, kb)
        if it is not None:
            aid = it["anime_id"]
            bucket = votes.setdefault(aid, {"item": it, "score": 0.0, "max_conf": 0.0, "chars": []})
            bucket["score"] += conf * max(rel_w, 0.8)
            bucket["max_conf"] = max(bucket["max_conf"], conf)
            if disp_name not in bucket["chars"]:
                bucket["chars"].append(disp_name)
            detected_names.append((disp_name, conf))
        elif conf >= 0.45 and len(unresolved) < 2:
            unresolved.append((disp_name, conf))

    # AniList GraphQL fallback ONLY if no strong local match locked yet
    best_local = max((v["max_conf"] for v in votes.values()), default=0.0)
    if best_local < 0.70 and unresolved:
        for ch_name, conf in unresolved:
            al_name, it = _vg_anilist_char_lookup(ch_name, kb)
            if it is not None:
                aid = it["anime_id"]
                bucket = votes.setdefault(aid, {"item": it, "score": 0.0, "max_conf": 0.0, "chars": []})
                bucket["score"] += conf
                bucket["max_conf"] = max(bucket["max_conf"], conf)
                if al_name not in bucket["chars"]:
                    bucket["chars"].append(al_name)
                detected_names.append((al_name, conf))

    best_char_match = None
    if votes:
        ranked = sorted(
            votes.values(),
            key=lambda v: (v["score"], v["max_conf"], v["item"].get("popularity") or 0),
            reverse=True,
        )
        top = ranked[0]
        if top["score"] >= 0.45 or top["max_conf"] >= 0.45:
            it = top["item"]
            best_char_match = {
                "score": min(0.99, max(top["max_conf"], min(0.98, top["score"]))),
                "character": top["chars"][0] if top["chars"] else "Identified Character",
                "characters_list": top["chars"][:6],
                "anime_title": it.get("title_english") or it.get("title"),
                "anilist_id": it.get("anilist_id"),
                "mal_id": it.get("mal_id"),
                "matched_item": it,
                "source": wd_out.get("engine") or "wd-tagger",
            }
    if not best_char_match:
        brain_hit = _vg_predict_character_from_brain(wd_out, kb, img_bytes=img_bytes)
        if brain_hit:
            best_char_match = brain_hit
            detected_names = [(brain_hit["character"], brain_hit["score"])]

    if not best_char_match and tags:
        # Check distinctive franchise-specific tags if characters missed threshold
        sig_tags = [t for t in tags if t not in _VG_GENERIC_WD_TAGS and not re.match(r"^\d+(?:boy|girl)s?$", t)]
        tag_cands = _vg_identify_scene_candidates(" ".join(sig_tags[:10]), kb["items"], kb=kb, limit=2) if len(sig_tags) >= 3 else []
        if tag_cands and tag_cands[0][0] >= 35.0:
            t_it = tag_cands[0][1]
            best_char_match = {
                "score": min(0.84, 0.55 + tag_cands[0][0] / 120.0),
                "character": f"Visual elements ({', '.join(sig_tags[:3])})",
                "characters_list": [],
                "anime_title": t_it.get("title_english") or t_it.get("title"),
                "matched_item": t_it,
                "source": "wd-tags-scene",
            }

    return {
        "best_char_match": best_char_match,
        "vision_characters": detected_names[:6],
        "vision_tags": [t for t in tags[:14] if t not in {"english text", "fake screenshot"}],
        "vision_engine": wd_out.get("engine"),
    }


def _vegeta_reverse_image_search(image_base64: str, kb: dict, use_remote_vision: bool = True) -> dict:
    """Reverse search anime screenshots/portraits via trace.moe (?cutBorders), local MAL index, and zero-key WD-Tagger."""
    if not image_base64:
        return None
    clean_b64 = image_base64
    if "," in clean_b64:
        clean_b64 = clean_b64.split(",", 1)[1]
    try:
        img_bytes = base64.b64decode(clean_b64)
    except Exception as e:
        print(f"Base64 decode error: {e}")
        return None

    try:
        # Reject solid blank / nearly blank frames to avoid trace.moe fade-to-black false positives
        try:
            import cv2 as _cv2
            import numpy as _np
            _arr = _np.frombuffer(img_bytes, dtype=_np.uint8)
            _im = _cv2.imdecode(_arr, _cv2.IMREAD_GRAYSCALE)
            if _im is not None and float(_np.std(_im)) < 4.0:
                return {"similarity": 0.0, "confident": False, "matched_item": None, "best_char_match": None, "vision_tags": []}
        except Exception:
            pass

        raw_jpg = _vg_prepare_vision_jpeg(img_bytes, max_dim=960)
        cropped_bytes = _extract_artwork_crop(raw_jpg)

        # Query trace.moe concurrently with raw + artwork-cropped frames for maximum lock accuracy
        probe_list = [cropped_bytes, raw_jpg] if cropped_bytes else [raw_jpg]
        all_results: list[dict] = []
        if len(probe_list) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as pool:
                futs = [pool.submit(_vg_query_trace_moe, b) for b in probe_list]
                for f in futs:
                    try:
                        all_results.extend(f.result() or [])
                    except Exception:
                        pass
        else:
            try:
                all_results.extend(_vg_query_trace_moe(probe_list[0]) or [])
            except Exception as err:
                print(f"trace.moe search error: {err}")

        all_results.sort(key=lambda x: float(x.get("similarity") or 0.0), reverse=True)
        by_al = kb.get("by_anilist_id", {})
        best_sim = float(all_results[0].get("similarity") or 0.0) if all_results else 0.0

        def _resolve_trace_item(cand: dict):
            al_id = cand.get("anilist")
            filename = cand.get("filename") or ""
            m_it = by_al.get(int(al_id)) if al_id and int(al_id) in by_al else None
            if not m_it and filename:
                clean_fn = re.sub(r"\[.*?\]|\(.*?\)|_|\.mp4|\.mkv|\d{3,4}p|x264|AAC", " ", filename, flags=re.I)
                clean_fn = re.sub(r"-\s*\d+.*$", "", clean_fn).strip()
                if clean_fn:
                    m_it, _ = _find_exact_or_fuzzy_title(clean_fn, kb["items"])
            return m_it

        # High-confidence (>= 0.87) trace.moe episode frame lock
        if best_sim >= 0.87:
            for cand in all_results[:4]:
                similarity = float(cand.get("similarity") or 0.0)
                if similarity < 0.87:
                    continue
                matched_item = _resolve_trace_item(cand)
                if matched_item:
                    return {
                        "similarity": similarity,
                        "confident": True,
                        "anilist_id": cand.get("anilist"),
                        "episode": cand.get("episode") or 1,
                        "from_seconds": float(cand.get("from") or 0.0),
                        "filename": cand.get("filename") or "",
                        "matched_item": matched_item,
                        "char_matches": [],
                        "cropped": bool(cropped_bytes),
                    }

        # Tier 2: Fast local MAL/AniList Character Portrait Visual Index
        search_bytes = cropped_bytes if cropped_bytes else raw_jpg
        char_matches = _match_character_visual(search_bytes, kb, top_k=3)
        top_char = char_matches[0] if char_matches else None
        local_char_lock = top_char if (top_char and top_char["score"] >= 0.90) else None

        # Tier 3: Zero-key AI Anime Vision (always scan full raw_jpg first so dark/edge characters aren't lost)
        vision_res = None
        if not local_char_lock and use_remote_vision:
            wd_raw = _vg_hf_wd_tagger_identify(raw_jpg)
            vision_res = _vg_resolve_wd_results(wd_raw, kb, img_bytes=raw_jpg)
            if (not vision_res or not vision_res.get("best_char_match")) and cropped_bytes:
                wd_crop = _vg_hf_wd_tagger_identify(cropped_bytes)
                crop_res = _vg_resolve_wd_results(wd_crop, kb, img_bytes=cropped_bytes)
                if crop_res and (crop_res.get("best_char_match") or not vision_res):
                    vision_res = crop_res

        best_char_match = local_char_lock
        if not best_char_match and vision_res and vision_res.get("best_char_match"):
            best_char_match = vision_res["best_char_match"]
        elif not best_char_match and top_char and top_char["score"] >= 0.86:
            best_char_match = top_char

        # Cross-verify mid-confidence trace.moe (0.78 - 0.87) with vision tags/anime agreement
        weak_trace_item = _resolve_trace_item(all_results[0]) if all_results else None
        if best_char_match and weak_trace_item and best_sim >= 0.78:
            bcm_item = best_char_match.get("matched_item")
            if bcm_item and bcm_item["anime_id"] == weak_trace_item["anime_id"]:
                return {
                    "similarity": best_sim,
                    "confident": True,
                    "anilist_id": all_results[0].get("anilist"),
                    "episode": all_results[0].get("episode") or 1,
                    "from_seconds": float(all_results[0].get("from") or 0.0),
                    "filename": all_results[0].get("filename") or "",
                    "matched_item": weak_trace_item,
                    "best_char_match": best_char_match,
                    "char_matches": char_matches,
                    "vision_characters": (vision_res or {}).get("vision_characters") or [],
                    "vision_tags": (vision_res or {}).get("vision_tags") or [],
                    "vision_engine": (vision_res or {}).get("vision_engine"),
                    "cropped": bool(cropped_bytes),
                }

        return {
            "similarity": best_sim,
            "confident": False,
            "anilist_id": all_results[0].get("anilist") if all_results else None,
            "episode": (all_results[0].get("episode") or 1) if all_results else 1,
            "from_seconds": float(all_results[0].get("from") or 0.0) if all_results else 0.0,
            "filename": (all_results[0].get("filename") or "") if all_results else "",
            "matched_item": None,
            "char_matches": char_matches,
            "best_char_match": best_char_match,
            "vision_characters": (vision_res or {}).get("vision_characters") or [],
            "vision_tags": (vision_res or {}).get("vision_tags") or [],
            "vision_engine": (vision_res or {}).get("vision_engine"),
            "cropped": bool(cropped_bytes),
        }
    except Exception as err:
        print(f"Reverse image search error: {err}")
        return None


_WORD_NUMS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'first': 1, 'second': 2, 'third': 3, 'fourth': 4, 'fifth': 5,
    'sixth': 6, 'seventh': 7, 'eighth': 8, 'ninth': 9, 'tenth': 10,
    'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5
}


def _parse_play_query(rest):
    r = re.sub(r"\b(epiosde|epsiode|episod|epsidoe|epidoes|epside)\b", "episode", rest, flags=re.I)
    
    season_num = None
    sm = re.search(r"\b(?:season|s)\s*(\d{1,2})\b|\b(\d{1,2})(?:st|nd|rd|th)\s+season\b", r, flags=re.I)
    if sm:
        season_num = int(sm.group(1) or sm.group(2))
    else:
        for w, n in _WORD_NUMS.items():
            if re.search(rf"\b(?:season|s)\s+{w}\b|\b{w}\s+season\b", r, flags=re.I):
                season_num = n
                break

    ep_num = None
    em = re.search(r"\b(?:episode|ep|e)\s*(\d{1,4})\b|\b(\d{1,4})(?:st|nd|rd|th)\s+episode\b", r, flags=re.I)
    if em:
        ep_num = int(em.group(1) or em.group(2))
    else:
        for w, n in _WORD_NUMS.items():
            if re.search(rf"\b(?:episode|ep|e)\s+{w}\b|\b{w}\s+episode\b", r, flags=re.I):
                ep_num = n
                break
        if not ep_num:
            em2 = re.search(r"\b(\d{1,4})\s*(?:of\s+season)?\b", r, flags=re.I)
            if em2:
                ep_num = int(em2.group(1))

    title_part = r
    title_part = re.sub(r"\b(?:the\s+)?(?:latest|newest|last|final|most recent)\s+(?:episode|ep)\s*(?:of)?\b", " ", title_part, flags=re.I)
    title_part = re.sub(r"\b(?:episode|ep|e)\s*\d{1,4}\b|\b\d{1,4}(?:st|nd|rd|th)\s+episode\b", " ", title_part, flags=re.I)
    title_part = re.sub(r"\b(?:season\s*\d{1,2}|\d{1,2}(?:st|nd|rd|th)\s+season)\b", " ", title_part, flags=re.I)
    for w in _WORD_NUMS.keys():
        title_part = re.sub(rf"\b(?:season|s)\s+{w}\b|\b{w}\s+season\b", " ", title_part, flags=re.I)
        title_part = re.sub(rf"\b(?:episode|ep|e)\s+{w}\b|\b{w}\s+episode\b", " ", title_part, flags=re.I)
    title_part = re.sub(r"\b(?:from the (?:beginning|start)|from episode one|of|the anime)\b", " ", title_part, flags=re.I)
    if ep_num:
        title_part = re.sub(rf"\b{ep_num}\b", " ", title_part)
    title_part = re.sub(r"\s+", " ", title_part).strip(": -").strip()

    return title_part, season_num, ep_num


def _apply_season_target(it, season_num, kb):
    if not it or not season_num:
        return it
    seasons = it.get("seasons") or []
    if not seasons:
        return it
    by_id = kb.get("by_id") or {x["anime_id"]: x for x in kb.get("items", [])}
    for s in seasons:
        nm = (s.get("season_name") or "").lower()
        if re.search(rf"\bseason\s*{season_num}\b", nm):
            return by_id.get(s["anime_id"]) or it
    tv = [s for s in seasons if not re.search(r"movie|special|ova|oad|ona|recap|film", (s.get("season_name") or "").lower())]
    if 0 < season_num <= len(tv):
        return by_id.get(tv[season_num - 1]["anime_id"]) or it
    return it


def _detect_play_intent(user_msg: str, items: list, user_context: dict = None) -> dict:
    """Detect agentic play commands: 'Play Solo Leveling Ep 3', 'Start Naruto', 'Resume', 'Watch Attack on Titan'."""
    msg_low = user_msg.lower().strip()
    # Only treat it as a command when the message STARTS with a play verb
    # ("what should i watch" / "watch order of naruto" must not auto-play anything)
    if re.match(r"^(?:\W*)(?:please\s+|can you\s+|could you\s+|vegeta\W*|let'?s\s+|i want to\s+|i wanna\s+)*watch\s+order\b", msg_low):
        return None
    if not re.match(r"^(?:\W*)(?:please\s+|can you\s+|could you\s+|vegeta\W*|let'?s\s+|i want to\s+|i wanna\s+|agentic\s+)*(auto[- ]?play:?|auto[- ]?launch:?|autoplay:?|play|start|watch|stream|put on|launch|resume|continue)\b", msg_low):
        return None

    # Check for direct resume command
    if any(k in msg_low for k in ["resume", "continue watching", "keep watching", "where i left off", "where was i"]):
        if user_context and user_context.get("continue_watching"):
            top = user_context["continue_watching"][0]
            return {
                "type": "play",
                "anime_id": top.get("anime_id"),
                "episode": top.get("episode") or 1,
                "time": top.get("progress_sec") or 0,
                "title": top.get("title") or "Anime",
                "is_resume": True,
            }
        elif user_context and user_context.get("current_anime"):
            ca = user_context["current_anime"]
            return {
                "type": "play",
                "anime_id": ca.get("anime_id"),
                "episode": ca.get("current_episode") or 1,
                "time": 0,
                "title": ca.get("title") or "Anime",
                "is_resume": True,
            }

    clean_rest = re.sub(r"^(?:\W*)(?:please\s+|can you\s+|could you\s+|vegeta\W*|let'?s\s+|i want to\s+|i wanna\s+|agentic\s+)*(auto[- ]?play:?|auto[- ]?launch:?|autoplay:?|play|start|watch|stream|put on|launch|resume|continue)\b\s*", "", msg_low).strip(": -")
    title_part, season_num, ep_num = _parse_play_query(clean_rest)
    target_ep = ep_num or 1

    if not title_part:
        return None

    specific, _ = _find_exact_or_fuzzy_title(title_part, items)
    if specific:
        if season_num:
            kb_obj = {"by_id": {x["anime_id"]: x for x in items}}
            specific = _apply_season_target(specific, season_num, kb_obj)
        return {
            "type": "play",
            "anime_id": specific["anime_id"],
            "episode": target_ep,
            "time": 0,
            "title": specific["title_english"] or specific["title"],
            "is_resume": False,
        }
    return None


_VG_SHORT_SCENE_WORDS = {
    "box", "dog", "cat", "arm", "leg", "eye", "fox", "god", "war", "lab", "gun",
    "ice", "ant", "orb", "elf", "orc", "pig", "sao", "odm", "nen", "eva", "red",
    "son", "mom", "dad", "boy", "man", "nun", "spy", "ink", "key", "fog", "bow",
    "axe", "bat", "rat", "owl", "ape", "bug", "bee", "mud", "ash", "sao", "jjk",
    "aot", "fma", "hxh", "opm", "mha", "dbz", "dbs",
}

_VG_PLOT_STOPWORDS = {
    "anime", "show", "series", "story", "background", "character", "characters",
    "main", "protagonist", "hero", "guy", "boy", "girl", "man", "woman", "person",
    "people", "world", "power", "powers", "gets", "become", "becomes", "became",
    "wants", "finds", "found", "lives", "live", "where", "about", "forgot", "name",
    "remember", "called", "which", "there", "scene", "episode", "season", "part",
    "movie", "film", "watch", "watched", "seen", "looking", "search", "find",
    "tell", "know", "explain", "explained", "happens", "happened", "starts",
    "begins", "start", "begin", "someone", "somebody", "everyone", "everybody",
    "thing", "things", "into", "from", "with", "without", "after", "before",
    "while", "when", "that", "this", "what", "whats", "who", "whom", "whose",
    "have", "has", "had", "does", "did", "done", "make", "makes", "made", "take",
    "takes", "took", "give", "gives", "gave", "goes", "went", "gone", "come",
    "comes", "came", "like", "just", "very", "really", "only", "also", "even",
    "back", "down", "over", "under", "again", "then", "than", "other", "another",
    "first", "second", "third", "last", "next", "some", "many", "much", "more",
    "most", "good", "best", "great", "little", "young", "high", "year", "years",
    "time", "days", "life", "friend", "friends", "fight", "fights", "fighting",
}


def _vg_stem_word(w: str) -> str:
    w = w.strip("'")
    if len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("ves") and len(w) > 4:
        return w[:-3] + "f"
    for suf in ("ation", "ition", "ment", "ness", "ingly", "edly", "ting", "ning", "ring", "ling", "ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            stem = w[:-len(suf)]
            if len(stem) >= 3 and stem[-1] == stem[-2] and stem[-1] not in ("s", "l", "z"):
                stem = stem[:-1]
            return stem
    return w


def _vg_stem_tokens(text: str, keep_stopwords: bool = False) -> list:
    if not text:
        return []
    raw = re.findall(r"[a-z0-9']{3,}", text.lower())
    out = []
    for w in raw:
        w_clean = w.strip("'")
        if len(w_clean) < 3:
            continue
        if len(w_clean) == 3 and w_clean not in _VG_SHORT_SCENE_WORDS and not w_clean.isdigit():
            continue
        if not keep_stopwords and w_clean in _VG_PLOT_STOPWORDS:
            continue
        st = _vg_stem_word(w_clean)
        if not keep_stopwords and st in _VG_PLOT_STOPWORDS:
            continue
        out.append(st)
    return out


_VG_SCENE_CONCEPT_EXPANSIONS = [
    (r"\b(wooden box|carries? (?:his )?(?:demon )?sister|sister in a box|bamboo muzzle|sun breathing|water breathing|hinokami)\b", "nezuko tanjiro kamado kimetsu demon slayer"),
    (r"\b(writes? (?:their )?names?|book that kills|death book|kills? criminals|apple shinigami|god of death notebook)\b", "notebook shinigami ryuk kira light yagami lawliet death note"),
    (r"\b(eats? (?:a )?(?:cursed|rotted)?\s*finger|twenty fingers|white hair blindfold|hollow purple|infinite void|malevolent shrine)\b", "sukuna yuji itadori gojo satoru megumi jujutsu kaisen cursed"),
    (r"\b(loses? (?:his )?arm and leg|brother(?:'s)? soul in armor|suit of armor brother|bring (?:their )?(?:dead )?(?:mom|mother) back|human transmutation)\b", "edward alphonse elric automail philosopher stone fullmetal alchemist"),
    (r"\b(three (?:giant )?walls|giant naked|eats? his (?:mom|mother)|bites? (?:his )?hand to transform|omni[- ]?directional|3d maneuver|rumbling)\b", "eren yeager mikasa levi titan wall maria scout regiment shingeki"),
    (r"\b(weakest (?:e[- ]?rank )?hunter|double dungeon|statue smiles?|shadow army|says? arise|system player level up)\b", "sung jinwoo shadow monarch igris arise solo leveling"),
    (r"\b(chainsaw dog|pulls? (?:a )?cord on (?:his )?chest|heart replaced by (?:his )?dog|gun devil|control devil)\b", "denji pochita makima power aki chainsaw man"),
    (r"\b(bald (?:hero|guy)|defeats? (?:everyone|enemies|monsters) in one punch|100 push[- ]?ups|cyborg disciple)\b", "saitama genos hero association one punch man"),
    (r"\b(dies? and (?:resets?|loops?|comes? back|restarts?)|time loop when he dies|return by death|blue haired maid)\b", "subaru natsuki emilia rem return by death re zero"),
    (r"\b(microwave time|texts? to the past|gel banana|mad scientist lab coat|worldline divergence)\b", "okabe rintaro kurisu makise mayuri steins gate"),
    (r"\b(trapped in (?:a )?(?:vr|virtual reality|mmorpg|video)[\w\s]*|die in (?:the )?game|dying in (?:the )?game|100 floors|dual blades|nervegear)\b", "kirito asuna aincrad nervegear sword art online"),
    (r"\b(brain surgeon|saves? a (?:young )?boy instead of (?:the )?mayor|boy grows up to be a (?:serial killer|monster|psychopath))\b", "tenma johan liebert anna monster surgeon"),
    (r"\b(alien (?:worm|parasite) in (?:his )?(?:right )?hand|hand talks|hand turns into blades)\b", "shinichi izumi migi parasyte kiseijuu"),
    (r"\b(everyone turned to stone|petrified for 3700 years|nitric acid|revival fluid|count seconds)\b", "senku ishigami taiju stone world dr stone"),
    (r"\b(pink hair.*horns|two small horns|invisible.*(?:hands|arms|vectors)|escapes? (?:a )?(?:research )?lab|diclonius)\b", "lucy nyu kouta diclonius vectors elfen lied"),
    (r"\b(piano prodigy|can'?t hear (?:his )?piano|blonde violinist|dies in spring|surgery letter)\b", "kousei arima kaori miyazono piano violin your lie in april"),
    (r"\b(deaf girl|bullied in elementary|hearing aids?|learns? sign language|redeem himself)\b", "shoya ishida shouko nishimiya deaf silent voice koe no katachi"),
    (r"\b(swap(?:ping)? bodies|boy in tokyo girl in countryside|comet hits town|red braided cord)\b", "taki tachibana mitsuha miyamizu comet body swap your name kimi no na wa"),
    (r"\b(orphanage.*(?:demons|livestock|farm|food|neck|tattoo)|numbers? tattooed on.*neck|grace field|mama isabella)\b", "emma norman ray isabella grace field promised neverland"),
    (r"\b(yellow octopus teacher|blow up the moon|mach 20|class 3[- ]e|assassinate their teacher)\b", "korosensei nagisa karma assassination classroom ansatsu"),
    (r"\b(cooking battles|clothes explode when eating|culinary academy|elite ten)\b", "soma yukihira erina totsuki shokugeki food wars"),
    (r"\b(gambling school|housepet system|compulsive gambler girl|student council president)\b", "yumeko jabami mary kirari kakegurui"),
    (r"\b(bar after death|play bar games to judge souls|mannequin bodies|arbiter bartender)\b", "decim chiyuki quindecim death parade"),
    (r"\b(giant hole|curse of the abyss|ascending causes bleeding|robot boy|white whistle)\b", "riko reg nanachi ozen made in abyss"),
    (r"\b(immortal orb|becomes a rock then a wolf then a boy|nameless boy in snow)\b", "fushi march gugu parona to your eternity fumetsu"),
    (r"\b(fake family|spy father|assassin mother|telepath daughter|mind reader girl|forger)\b", "loid yor anya bond forger spy x family"),
    (r"\b(no magic|muscles instead of magic|creampuffs|hogwarts parody|punches spells)\b", "mash burnedead finn lance easton mashle"),
    (r"\b(aliens and ghosts|lose his balls|golden ball|turbo granny|occult boy gyaru girl)\b", "momo ayase okarun ken takakura turbo granny dan da dan"),
    (r"\b(cleanup crew|eats a small kaiju|32 year old|transforms into a kaiju|defense force)\b", "kafka hibino mina ashiro reno kaiju no 8"),
    (r"\b(viking boy|father killed by mercenaries|travels with his father's killer|i have no enemies)\b", "thorfinn askeladd thors canute vinland saga"),
    (r"\b(giant slabs? of iron|black swordsman|band of the hawk|brand of sacrifice|eclipse betrayal)\b", "guts griffith casca puck berserk"),
    (r"\b(blind swordsman|prosthetic limbs|demons stole his body parts|gets his body back when killing demons)\b", "hyakkimaru dororo tahomaru daigo"),
    (r"\b(prosthetic metal arms|writes? letters for people|what does i love you mean|major gilbert)\b", "violet evergarden gilbert hodgins auto memory doll"),
    (r"\b(apothecary girl|kidnapped into imperial palace|tastes? poison|freckles|eunuch jinshi)\b", "maomao jinshi apothecary diaries kusuriya"),
    (r"\b(sister eaten by a red dragon|cook and eat dungeon monsters|dwarf chef|elf magemarcille)\b", "laios marcille chilchuk senshi falin delicious in dungeon"),
    (r"\b(soccer prison|300 strikers|egoist striker|eliminated from national team)\b", "yoichi isagi bachira nagi rin ego blue lock"),
    (r"\b(goes back to elementary school|1988|save classmate from kidnapper|revival butterfly)\b", "satoru fujinuma kayo hinazuki airi erased boku dake"),
    (r"\b(trapped in skeleton body|guild base nazarick|undead overlord|npc guardians)\b", "ainz ooal gown momonga albedo shalltear overlord"),
    (r"\b(elf mage lives for thousands of years|after defeating the demon king|hero himmel dies of old age)\b", "frieren fern stark himmel heiter beyond journey"),
    (r"\b(eye power to command anyone|britannia empire|masked rebel zero|mecha knightmare)\b", "lelouch vi britannia suzaku ccallen code geass"),
    (r"\b(false accusation|shield hero|cannot use weapons|raccoon girl slave|four cardinal heroes)\b", "naofumi iwatani raphtalia filo shield hero"),
    (r"\b(pretends to be a background mob|secret cult turns out to be real|i am atomic)\b", "cid kagenou shadow alpha delta eminence in shadow"),
    (r"\b(boy sold to a demon by his parents|becomes demon grandson|demon school babyls)\b", "iruma suzuki sullivan opera clara welcome to demon school"),
    (r"\b(white room genius|manipulates from the shadows|class d|island exam points)\b", "kiyotaka ayanokoji suzune horikita kei classroom of the elite"),
    (r"\b(boy born without magic|five leaf grimoire|anti magic sword|rival yuno|wizard king)\b", "asta yuno noelle yami black clover"),
    (r"\b(coffee shop ghouls|date tries to eat him|steel beams organ transplant|one eyed ghoul)\b", "ken kaneki touka rize anteiku kagune tokyo ghoul"),
    (r"\b(military spine implant|sandevistan|moon promise|arasaka night city)\b", "david martinez lucy rebecca maine cyberpunk edgerunners"),
    (r"\b(doctor reincarnated as his favorite idol's twin baby|idol murdered at door|star eyes revenge)\b", "aqua ruby ai hoshino kana akane oshi no ko"),
    (r"\b(stabbed on the street|reincarnated as a blind slime|predator skill|goblin village|veldora)\b", "rimuru tempest veldora shion benimaru slime datta ken"),
    (r"\b(shut[- ]?in dies saving teenagers|reincarnated as a baby|roxy water magic tutor|greyrat)\b", "rudeus greyrat roxy sylphiette eris mushoku tensei"),
    (r"\b(rubber pirate|straw hat|eats gum gum fruit|devil fruit|pirate king)\b", "luffy zoro nami sanji straw hat one piece"),
    (r"\b(fox demon sealed inside|outcast ninja boy|hokage|sharingan clan massacre)\b", "naruto uzumaki sasuke uchiha kakashi itachi"),
    (r"\b(sees ghosts|shinigami gives him her powers|hollow mask|soul society rescue|zanpakuto)\b", "ichigo kurosaki rukia aizen byakuya bleach"),
]


def _vg_is_scene_or_plot_query(msg_low: str) -> bool:
    if not msg_low:
        return False
    # Never hijack explicit voice-actor/seiyuu or studio queries as plot descriptions
    if re.search(r"\b(voice\s+actor|seiyuu|who\s+voice[ds]?|voiced?\s*(?:by|in|for|of)?|voices|voicing|studio|animated\s+by)\b", msg_low):
        return False
    # Explicit identification triggers
    if any(k in msg_low for k in [
        "what is that anime", "what's that anime", "whats that anime", "which anime", "forgot the name",
        "anime where", "show where", "the one where", "name of the anime", "what anime", "anime about",
        "what is the anime", "what's the anime", "cant remember", "can't remember", "story where",
        "background story", "plot is", "about a guy", "about a boy", "about a girl", "about a man",
        "about a woman", "there is a scene", "there's a scene", "theres a scene", "scene where",
        "i remember a scene", "i saw a clip", "saw a reel", "saw a tiktok", "saw a short",
        "help me find", "find this anime", "identify this anime", "what show is", "which show is",
        "in this anime", "the main character is", "mc is a", "mc gets", "mc has", "boy who", "girl who",
        "guy who", "man who", "woman who", "starts with a", "episode where", "scene in which",
    ]):
        return True
    # Also detect natural narrative descriptions (e.g. "a boy carries his demon sister in a box")
    if re.search(r"\b(?:(?:a|the|an|two|three|ten thousand)\s+)?(boy|girl|guy|man|woman|kid|kids|children|players|brothers|siblings|orphans|hunters|student|high schooler|doctor|surgeon|prince|princess|knight|mage|hunter|assassin|spy|pirate|ninja|swordsman|orphan|detective|scientist)\b", msg_low):
        if len(re.findall(r"[a-z0-9']{3,}", msg_low)) >= 5:
            return True
    # Or if any concept expansion matches
    for pat, _ in _VG_SCENE_CONCEPT_EXPANSIONS:
        if re.search(pat, msg_low):
            return True
    return False


def _vg_identify_scene_candidates(msg_low: str, items: list, kb: dict = None, limit: int = 4) -> list:
    """Rank anime across both servers by scene/plot/character/tag/episode description using concept expansion + IDF-weighted stemmed BM25."""
    if not msg_low or not items:
        return []

    expanded_parts = [msg_low]
    concept_boost_terms = set()
    for pat, exp_str in _VG_SCENE_CONCEPT_EXPANSIONS:
        if re.search(pat, msg_low):
            expanded_parts.append(exp_str)
            for t in _vg_stem_tokens(exp_str):
                concept_boost_terms.add(t)

    # Check explicit plot signatures first
    plot_signatures = [
        ("slime", ["slime", "reincarnated as a slime", "rimuru", "tempest", "veldora"], "That Time I Got Reincarnated as a Slime"),
        ("death note", ["notebook", "write names", "writes names", "shinigami", "ryuk", "kira", "lawliet", "light yagami"], "Death Note"),
        ("attack on titan", ["titans", "giant humanoid", "walls", "scout regiment", "eren", "wall maria", "odm gear", "rumbling"], "Attack on Titan"),
        ("demon slayer", ["bamboo muzzle", "wooden box", "demon sister", "nezuko", "tanjiro", "breathing technique", "demon slayer corps", "hashira", "hinokami"], "Demon Slayer: Kimetsu no Yaiba"),
        ("jujutsu kaisen", ["eats a finger", "cursed finger", "cursed spirit", "sukuna", "gojo", "domain expansion", "yuji itadori", "hollow purple"], "Jujutsu Kaisen"),
        ("one punch man", ["one punch", "bald hero", "defeats everyone", "saitama", "genos", "hero association"], "One Punch Man"),
        ("sword art online", ["virtual reality", "trapped in a game", "trapped in a virtual", "mmorpg", "die in game", "dying in the game", "real life", "kirito", "nervegear", "aincrad"], "Sword Art Online"),
        ("fullmetal alchemist", ["human transmutation", "two brothers", "arm and leg", "suit of armor", "alchemist", "philosopher stone", "elric", "automail"], "Fullmetal Alchemist: Brotherhood"),
        ("steins gate", ["microwave", "time travel", "banana", "divergence meter", "okabe", "kurisu", "mad scientist", "el psy kongroo"], "Steins;Gate"),
        ("monster", ["brain surgeon", "saves a young boy", "johan liebert", "charismatic psychopath", "tenma"], "Monster"),
        ("tokyo ghoul", ["half ghoul", "coffee", "eats humans", "kaneki", "organ transplant", "kagune", "anteiku"], "Tokyo Ghoul"),
        ("parasyte", ["alien parasite", "right hand", "migi", "shinichi"], "Parasyte -the maxim-"),
        ("dr stone", ["turned to stone", "petrified", "senku", "rebuild civilization", "stone world", "revival fluid"], "Dr. STONE"),
        ("vinland saga", ["vikings", "thorfinn", "askeladd", "no enemies", "vinland"], "Vinland Saga"),
        ("hunter x hunter", ["hunter exam", "killua", "gon", "nen", "kurapika", "phantom troupe", "chimera ant"], "Hunter x Hunter"),
        ("code geass", ["geass", "absolute obedience", "lelouch", "britannia", "knightmare"], "Code Geass"),
        ("chainsaw man", ["chainsaw", "chainsaw dog", "denji", "pochita", "makima", "public safety"], "Chainsaw Man"),
        ("solo leveling", ["shadow monarch", "weakest hunter", "sung jinwoo", "double dungeon", "arise", "shadow army"], "Solo Leveling"),
        ("re zero", ["return by death", "subaru", "emilia", "rem", "dies and resets", "time loop"], "Re:ZERO -Starting Life in Another World-"),
        ("frieren", ["elf mage", "defeated the demon king", "defeating the demon king", "after the hero dies", "frieren", "fern", "himmel"], "Frieren: Beyond Journey's End"),
        ("mushoku tensei", ["jobless", "rudeus", "reincarnated as a baby", "magic prodigy", "greyrat", "roxy"], "Mushoku Tensei: Jobless Reincarnation"),
        ("spy x family", ["fake family", "telepath", "anya", "assassin mother", "spy father", "loid", "yor", "forger"], "SPY x FAMILY"),
        ("my hero academia", ["quirkless", "all might", "deku", "midoriya", "ua high", "one for all"], "My Hero Academia"),
        ("bleach", ["soul reaper", "ichigo", "hollow", "zanpakuto", "soul society", "bankai"], "Bleach"),
        ("naruto", ["nine tailed fox", "hokage", "naruto", "sasuke", "hidden leaf", "rasengan", "sharingan"], "Naruto"),
        ("one piece", ["rubber body", "devil fruit", "pirate king", "luffy", "straw hat", "grand line", "zoro"], "One Piece"),
        ("dragon ball", ["saiyan", "seven dragon balls", "goku", "vegeta", "kamehameha", "super saiyan"], "Dragon Ball Z"),
        ("blue lock", ["egoist", "soccer prison", "300 strikers", "isagi", "blue lock"], "BLUE LOCK"),
        ("haikyuu", ["short volleyball", "hinata", "karasuno", "kageyama", "volleyball"], "Haikyu!!"),
        ("dandadan", ["ghosts and aliens", "turbo granny", "okarun", "momo ayase", "golden ball"], "DAN DA DAN"),
        ("kaiju no 8", ["cleanup crew", "kafka hibino", "turns into a kaiju", "defense force"], "Kaiju No. 8"),
        ("overlord", ["skeleton", "ainz ooal gown", "nazarick", "undead overlord"], "Overlord"),
        ("mob psycho", ["esper", "psychic", "reigen", "100 percent", "shigeo", "mob", "bowl cut", "con artist"], "Mob Psycho 100"),
        ("cyberpunk edgerunners", ["night city", "sandevistan", "david martinez", "lucy", "cyberpsychosis"], "Cyberpunk: Edgerunners"),
        ("your name", ["body swap", "swap bodies", "comet", "mitsuha", "taki", "red braided"], "Your Name."),
        ("oshi no ko", ["reincarnated as idol", "ai hoshino", "ruby", "aqua", "star eyes"], "Oshi No Ko"),
        ("classroom of the elite", ["ayanokoji", "white room", "class d", "meritocracy", "points system"], "Classroom of the Elite"),
        ("black clover", ["no magic", "anti magic", "asta", "yuno", "wizard king", "five leaf grimoire"], "Black Clover"),
        ("cowboy bebop", ["bounty hunter", "spaceship", "spike spiegel", "bebop", "jazz"], "Cowboy Bebop"),
        ("evangelion", ["eva unit", "angels", "shinji", "nerv", "third impact", "get in the robot"], "Neon Genesis Evangelion"),
        ("erased", ["revival", "goes back in time", "elementary school", "kayo", "satoru"], "ERASED"),
        ("shield hero", ["rising of the shield", "naofumi", "falsely accused", "raphtalia", "four cardinal heroes"], "The Rising of the Shield Hero"),
        ("eminence in shadow", ["atomic", "shadow garden", "cid kagenou", "chunnibyou", "i am atomic"], "The Eminence in Shadow"),
        ("elfen lied", ["diclonius", "vectors", "pink hair", "small horns", "research lab", "invisible", "lucy", "kouta"], "Elfen Lied"),
        ("your lie in april", ["piano prodigy", "blonde violinist", "kousei", "kaori"], "Your Lie in April"),
        ("silent voice", ["deaf girl", "sign language", "shouko", "shoya", "hearing aid"], "A Silent Voice"),
        ("promised neverland", ["orphanage", "grace field", "emma", "norman", "numbers", "tattooed", "livestock"], "The Promised Neverland"),
        ("assassination classroom", ["yellow octopus", "korosensei", "class 3-e", "mach 20"], "Assassination Classroom"),
        ("made in abyss", ["curse of the abyss", "white whistle", "riko", "reg", "nanachi"], "Made in Abyss"),
        ("to your eternity", ["immortal orb", "fushi", "nameless boy", "turns into a wolf"], "To Your Eternity"),
        ("mashle", ["muscles instead of magic", "creampuffs", "mash burnedead", "easton"], "MASHLE: MAGIC AND MUSCLES"),
        ("dororo", ["prosthetic limbs", "hyakkimaru", "dororo", "demons stole his body"], "Dororo"),
        ("violet evergarden", ["auto memory doll", "violet evergarden", "major gilbert", "metal arms"], "Violet Evergarden"),
        ("apothecary diaries", ["poison taster", "maomao", "jinshi", "imperial palace"], "The Apothecary Diaries"),
        ("delicious in dungeon", ["eat dungeon monsters", "laios", "marcille", "senshi", "red dragon ate"], "Delicious in Dungeon"),
    ]

    sig_matched_item = None
    best_sig_hits = 0
    is_scene_q = _vg_is_scene_or_plot_query(msg_low)
    for _, triggers, target_title in plot_signatures:
        matches = sum(1 for t in triggers if t in msg_low)
        if matches >= (1 if is_scene_q else 2) and matches > best_sig_hits:
            specific, _ = _find_exact_or_fuzzy_title(target_title, items)
            if specific:
                sig_matched_item = specific
                best_sig_hits = matches

    q_stems = list(dict.fromkeys(_vg_stem_tokens(" ".join(expanded_parts))))
    raw_words = [w for w in re.findall(r"[a-z0-9']{3,}", msg_low) if w not in _VG_PLOT_STOPWORDS]
    bigrams = [f"{raw_words[i]} {raw_words[i+1]}" for i in range(len(raw_words) - 1)]

    idf_map = (kb or {}).get("idf") or {}
    default_idf = 3.2

    scored_by_franchise = {}
    for it in items:
        if _vg_is_adult(it) and "hentai" not in msg_low:
            continue
        syn_h = it.get("synopsis_lower") or ""
        tag_h = it.get("tags_lower") or ""
        chr_h = it.get("characters_lower") or ""
        ep_h = it.get("ep_titles_lower") or ""
        tit_h = f"{(it.get('title_english') or '').lower()} {(it.get('title') or '').lower()} {(it.get('synonyms') or '').lower()}"
        stems_set = it.get("stemmed_plot_tokens")

        score = 0.0
        matched_clues = []

        if sig_matched_item and it["anime_id"] == sig_matched_item["anime_id"]:
            score += 90.0
            matched_clues.append("plot signature")

        for st in q_stems:
            if stems_set is not None and st not in stems_set:
                continue
            w_idf = idf_map.get(st, default_idf)
            boost = 1.6 if st in concept_boost_terms else 1.0
            hit_any = False
            if st in chr_h:
                score += w_idf * 2.8 * boost
                hit_any = True
            if st in tag_h:
                score += w_idf * 2.2 * boost
                hit_any = True
            if st in ep_h:
                score += w_idf * 2.4 * boost
                hit_any = True
            if st in syn_h:
                score += w_idf * 1.8 * boost
                hit_any = True
            if st in tit_h:
                score += w_idf * 2.0 * boost
                hit_any = True
            if hit_any and len(matched_clues) < 6 and st not in matched_clues:
                matched_clues.append(st)

        for bg in bigrams:
            if bg in syn_h or bg in ep_h or bg in tag_h:
                score += 6.5

        if len(matched_clues) >= (1 if sig_matched_item and it["anime_id"] == sig_matched_item["anime_id"] else 2):
            pop_bonus = min(4.5, ((it.get("popularity") or 0) / 220000.0))
            tv_bonus = 1.2 if (it.get("type") or "").upper() == "TV" else 0.0
            t_low_cmp = (it.get("title_english") or it.get("title") or "").lower()
            seq_pen = -35.0 if re.search(r"\b(season\s*[2-9]|part\s*[2-9]|cour\s*[2-9]|2nd season|3rd season|hashira training|entertainment district|swordsmith village|arise from the shadow|chronicle|recap)\b", t_low_cmp) and not re.search(r"\b(season\s*[2-9]|part\s*[2-9]|2nd|3rd|arc|hashira|entertainment|swordsmith)\b", msg_low) else 0.0
            total_sc = score + pop_bonus + tv_bonus + seq_pen
            fkey = it.get("franchise") or _vg_base(it) or str(it["anime_id"])
            prev = scored_by_franchise.get(fkey)
            if not prev or total_sc > prev[0]:
                # Check if a specific episode title matched
                matched_ep = 1
                if it.get("ep_titles") and raw_words:
                    best_ep_hits = 0
                    for ep_num, ep_t in it["ep_titles"]:
                        ep_low = ep_t.lower()
                        eh = sum(1 for rw in raw_words if len(rw) >= 4 and rw in ep_low)
                        if eh > best_ep_hits:
                            best_ep_hits = eh
                            matched_ep = ep_num
                scored_by_franchise[fkey] = (total_sc, it, matched_ep, matched_clues)

    ranked = sorted(scored_by_franchise.values(), key=lambda x: x[0], reverse=True)
    if sig_matched_item and not ranked:
        return [(50.0, sig_matched_item, 1, ["plot match"])]
    return [r for r in ranked[:limit] if r[0] >= 5.5]


def _find_by_plot_description(msg_low: str, items: list, kb: dict = None) -> tuple:
    """Identify an anime from a story background / scene / plot description across both servers."""
    cands = _vg_identify_scene_candidates(msg_low, items, kb=kb, limit=1)
    if cands:
        _, best_item, _, _ = cands[0]
        return best_item, best_item.get("title_english") or best_item.get("title")
    return None, None


# ─── Vegeta Agent Brain v2: planner, tools, similarity, taste, Gemini tool loop ─────
import math as _vg_math
import random as _vg_random
import datetime as _vg_dt

_VG_CMD_VERBS = (
    "play", "start", "watch", "stream", "resume", "continue", "add", "save", "bookmark", "remove",
    "delete", "drop", "open", "show", "go", "take", "navigate", "search", "look up", "switch", "change",
    "next", "skip", "previous", "prev", "put", "launch", "browse", "explore", "surprise", "pull up",
    "bring up", "set", "turn", "dub", "sub", "boost", "increase", "pause", "fullscreen", "mute", "unmute",
)
_VG_LIST_WORDS = r"(?:my\s+)?(?:list|watchlist|watch\s+list|library|collection|favorites|favourites|saved)"


def _vg_title(it):
    return ((it or {}).get("title_english") or (it or {}).get("title") or "Anime").strip()


def _vg_year(it):
    y = it.get("season_year") or 0
    if not y:
        ys = str(it.get("year") or "")
        y = int(ys) if ys.isdigit() else 0
    return y


def _vg_card(it, syn_len=0):
    d = {
        "anime_id": it["anime_id"], "title": _vg_title(it), "type": it.get("type"),
        "episodes": it.get("episode_count"), "score": it.get("score"), "year": _vg_year(it) or None,
        "status": it.get("status"), "genres": (it.get("genres") or "")[:140],
    }
    if syn_len:
        d["synopsis"] = (it.get("synopsis") or "")[:syn_len]
    return d


def _vg_genre_set(it):
    gs = it.get("_vg_gset")
    if gs is None:
        raw = it.get("genres_lower") or ""
        gs = frozenset(g for g in GENRE_MAP if g in raw)
        it["_vg_gset"] = gs
    return gs


def _vg_is_adult(it):
    gl = it.get("genres_lower") or ""
    return "hentai" in gl or "erotica" in gl


def _vg_base(it):
    return _clean_base_title(_vg_title(it)) or str(it["anime_id"])


def _vg_dedupe(seq, limit, exclude_bases=()):
    out, seen = [], set(exclude_bases)
    for it in seq:
        b = _vg_base(it)
        if b in seen:
            continue
        seen.add(b)
        out.append(it)
        if len(out) >= limit:
            break
    return out


def _vg_genre_display(kb, g):
    for name in (kb.get("stats", {}).get("genre_counts") or {}):
        if name.lower() == (g or "").lower():
            return name
    return " ".join(w.capitalize() for w in (g or "").split())


# ── Similarity engine ("anime like X") ──
def _vg_find_similar(seed, items, limit=6, exclude_ids=None):
    exclude_ids = set(exclude_ids or [])
    sg = _vg_genre_set(seed)
    if not sg:
        return []
    seed_adult = _vg_is_adult(seed)
    seed_base = _vg_base(seed)
    seed_words = []
    for w in re.findall(r"[a-z]{6,}", seed.get("synopsis_lower") or ""):
        if w not in _VEGETA_STOPWORDS and w not in seed_words:
            seed_words.append(w)
    seed_words = seed_words[:40]
    scored = []
    for it in items:
        if it["anime_id"] == seed["anime_id"] or it["anime_id"] in exclude_ids:
            continue
        if it.get("franchise") and it.get("franchise") == seed.get("franchise"):
            continue
        if (it.get("score") or 0) < 6.8 or _vg_is_adult(it) != seed_adult:
            continue
        g = _vg_genre_set(it)
        inter = len(sg & g)
        if not inter:
            continue
        jac = inter / len(sg | g)
        syn = it.get("synopsis_lower") or ""
        kw = sum(1 for w in seed_words if w in syn) if seed_words else 0
        pop = _vg_math.log10((it.get("popularity") or 0) + 1) / 6.0
        s = jac * 5.0 + min(kw, 8) * 0.3 + (it.get("score") or 0) / 10.0 * 2.0 + pop
        scored.append((s, it))
    scored.sort(key=lambda x: x[0], reverse=True)
    return _vg_dedupe((it for _, it in scored), limit, exclude_bases={seed_base})


# ── Structured filtered search ──
def _vg_parse_filters(msg_low):
    f = {}
    m = re.search(r"\b(?:after|since|from)\s+((?:19|20)\d{2})\b", msg_low)
    if m:
        f["year_min"] = int(m.group(1))
    m = re.search(r"\b(?:before|until|older than)\s+((?:19|20)\d{2})\b", msg_low)
    if m:
        f["year_max"] = int(m.group(1))
    if "year_min" not in f and "year_max" not in f:
        m = re.search(r"\b(?:(19|20)?(\d))0s\b", msg_low)
        if m:
            century = m.group(1) or ("19" if int(m.group(2)) >= 4 else "20")
            y = int(century + m.group(2) + "0")
            f["year_min"], f["year_max"] = y, y + 9
        else:
            m = re.search(r"\b((?:19[6-9]|20[0-3])\d)\b", msg_low)
            if m:
                f["year_min"] = f["year_max"] = int(m.group(1))
            elif re.search(r"\bthis year\b", msg_low):
                f["year_min"] = f["year_max"] = _vg_dt.date.today().year
    m = (re.search(r"\b(?:score|scored|rated|rating)\s*(?:of\s*)?(?:above|over|at least|>=?|more than|higher than)?\s*(\d(?:\.\d)?)\b(?!\s*(?:eps?|episodes))", msg_low)
         or re.search(r"\b(?:above|over|at least|more than|higher than)\s+(\d(?:\.\d)?)\b(?!\s*(?:eps?|episodes|minutes|mins))", msg_low))
    if m:
        v = float(m.group(1))
        if 1 <= v <= 10:
            f["min_score"] = v
    if re.search(r"\b(?:movies?|films?)\b", msg_low):
        f["type"] = "MOVIE"
    elif re.search(r"\bovas?\b", msg_low):
        f["type"] = "OVA"
    elif re.search(r"\bonas?\b", msg_low):
        f["type"] = "ONA"
    m = re.search(r"\b(?:under|less than|fewer than|below|max|at most)\s+(\d{1,4})\s*(?:eps?|episodes)\b", msg_low)
    if m:
        f["max_eps"] = int(m.group(1))
    m = re.search(r"\b(?:over|more than|at least|above)\s+(\d{1,4})\s*(?:eps?|episodes)\b", msg_low)
    if m:
        f["min_eps"] = int(m.group(1))
    if "max_eps" not in f and re.search(r"\b(?:short|quick|one[- ]cour|1[- ]cour|binge in a day)\b", msg_low):
        f["max_eps"] = 13
    if "min_eps" not in f and re.search(r"\b(?:long running|long-running|long series|lots of episodes)\b", msg_low):
        f["min_eps"] = 50
    if re.search(r"\b(?:currently airing|airing|ongoing|this season|simulcast)\b", msg_low):
        f["status"] = "airing"
    elif re.search(r"\b(?:finished|completed|complete series)\b", msg_low):
        f["status"] = "finished"
    m = re.search(r"\btop\s+(\d{1,2})\b", msg_low) or re.search(r"\b(\d{1,2})\s+(?:anime|shows|series|movies|titles|recommendations|picks)\b", msg_low)
    if m:
        f["limit"] = max(1, min(int(m.group(1)), 10))
    if re.search(r"\b(?:best|top|highest rated|highest-rated|greatest|masterpiece|masterpieces|goated)\b", msg_low):
        f["sort"] = "score"
    elif re.search(r"\b(?:new|newest|latest|recent)\b", msg_low):
        f["sort"] = "newest"
    genres = _detect_query_genres(msg_low)
    if "hentai" in msg_low and "hentai" not in genres:
        genres = list(genres) + ["hentai"]
    if genres:
        f["genres"] = genres
    return f


def _vg_search(items, genres=None, query=None, year_min=None, year_max=None, min_score=None, type=None,
               max_eps=None, min_eps=None, status=None, sort="popularity", limit=8, exclude_ids=None):
    genres = [str(g).lower().strip() for g in (genres or []) if g]
    q_words = [w for w in re.findall(r"[a-z0-9']{3,}", (query or "").lower()) if w not in _VEGETA_STOPWORDS]
    exclude_ids = set(exclude_ids or [])
    want_adult = "hentai" in genres
    try:
        limit = max(1, min(int(limit or 8), 15))
    except Exception:
        limit = 8
    res = []
    for it in items:
        if it["anime_id"] in exclude_ids:
            continue
        if _vg_is_adult(it) and not want_adult:
            continue
        gl = it.get("genres_lower") or ""
        if genres and not all((g in gl) or any(s in gl for s in GENRE_MAP.get(g, [g])) for g in genres):
            continue
        y = _vg_year(it)
        if year_min and (not y or y < int(year_min)):
            continue
        if year_max and (not y or y > int(year_max)):
            continue
        sc = it.get("score") or 0
        if min_score and sc < float(min_score):
            continue
        if type:
            req_t = str(type).upper()
            it_t = (it.get("type") or "").upper()
            if req_t == "MOVIE":
                is_mov = (it_t == "MOVIE") or (it.get("episode_count") == 1 and any(k in (it.get("title") or "").lower() for k in ["movie", "film", "gekijouban"]))
                if not is_mov:
                    continue
            elif it_t != req_t:
                continue
        ep = it.get("episode_count") or 0
        if max_eps and (not ep or ep > int(max_eps)):
            continue
        if min_eps and ep < int(min_eps):
            continue
        st = (it.get("status") or "").lower()
        if status == "airing" and not (("airing" in st and "finish" not in st) or "releas" in st):
            continue
        if status == "finished" and "finish" not in st:
            continue
        rel = 0
        if q_words:
            hay = " ".join(it.get("all_titles_norm") or []) + " " + (it.get("synopsis_lower") or "")
            rel = sum(1 for w in q_words if w in hay)
            if not rel:
                continue
        res.append((rel, it))
    if sort == "score":
        popular = [x for x in res if (x[1].get("popularity") or 0) >= 5000]
        if len(popular) >= min(limit, 3):
            res = popular
        res.sort(key=lambda x: (x[0], x[1].get("score") or 0, x[1].get("popularity") or 0), reverse=True)
    elif sort == "newest":
        res.sort(key=lambda x: (x[0], _vg_year(x[1]), x[1].get("popularity") or 0), reverse=True)
    else:
        res.sort(key=lambda x: (x[0], x[1].get("popularity") or 0, x[1].get("score") or 0), reverse=True)
    return _vg_dedupe((it for _, it in res), limit)


# ── Personal taste profile ──
def _vg_taste(kb, ctx):
    by_id = kb["by_id"]
    ctx = ctx or {}
    ids = [s.get("anime_id") for s in (ctx.get("continue_watching") or [])]
    ids += [s.get("anime_id") for s in (ctx.get("my_list") or [])]
    if ctx.get("current_anime"):
        ids.append(ctx["current_anime"].get("anime_id"))
    seen, counts, titles = set(), {}, []
    for i in ids:
        try:
            i = int(i)
        except Exception:
            continue
        if i in seen:
            continue
        seen.add(i)
        it = by_id.get(i)
        if not it:
            continue
        titles.append(_vg_title(it))
        for g in _vg_genre_set(it):
            counts[g] = counts.get(g, 0) + 1
    top = sorted(counts, key=lambda g: -counts[g])[:4]
    return {"watched_ids": seen, "top_genres": top, "titles": titles[:10], "genre_counts": counts}


def _vg_personal_recs(kb, ctx, limit=5, genres=None):
    t = _vg_taste(kb, ctx)
    weights = dict(t["genre_counts"])
    for g in (genres or []):
        weights[g] = weights.get(g, 0) + 5
    if not weights:
        return t, []
    by_id = kb["by_id"]
    watched_bases = {_vg_base(by_id[i]) for i in t["watched_ids"] if i in by_id}
    scored = []
    for it in kb["items"]:
        if it["anime_id"] in t["watched_ids"] or _vg_is_adult(it) or (it.get("score") or 0) < 7.6:
            continue
        g = _vg_genre_set(it)
        if genres and not any(x in g for x in genres):
            continue
        w = sum(weights.get(x, 0) for x in g)
        if not w:
            continue
        s = w / (len(g) + 1) * 3.0 + (it.get("score") or 0) / 10.0 * 2.0 + _vg_math.log10((it.get("popularity") or 0) + 1) / 6.0
        scored.append((s, it))
    scored.sort(key=lambda x: x[0], reverse=True)
    return t, _vg_dedupe((it for _, it in scored), limit, exclude_bases=watched_bases)


# ── Target resolution (titles, pronouns, ordinals) ──
_VG_PRONOUN_RE = re.compile(
    r"^(?:it|this|that|this one|that one|this anime|that anime|this show|that show|the anime|"
    r"(?:the\s+)?(?:first|second|third|fourth|fifth|last)(?:\s+one)?)$"
)
_VG_ORDINALS = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4, "last": -1}


def _vg_last_mentioned_ids(history):
    for h in reversed(history or []):
        if h.get("role") == "model":
            ids = [int(x) for x in re.findall(r"#anime/(\d+)", h.get("text") or "")]
            if ids:
                return list(dict.fromkeys(ids))
    return []


def _vg_resolve_target(phrase, kb, ctx, history):
    by_id = kb["by_id"]
    ctx = ctx or {}
    p = re.sub(r"[^\w\s'-]", " ", (phrase or "").lower())
    p = re.sub(r"\b(please|for me|now|right now|vegeta|again|on kuroanime)\b", " ", p)
    p = re.sub(r"\s+", " ", p).strip()
    on_screen = ctx.get("current_view") in ("view-watch", "view-detail") and ctx.get("current_anime")
    if not p or _VG_PRONOUN_RE.match(p):
        ordinal = next((v for k, v in _VG_ORDINALS.items() if re.search(rf"\b{k}\b", p)), None)
        if ordinal is None and on_screen:
            try:
                return by_id.get(int(ctx["current_anime"].get("anime_id")))
            except Exception:
                pass
        ids = _vg_last_mentioned_ids(history)
        if ids:
            idx = ordinal if ordinal is not None else 0
            try:
                return by_id.get(ids[idx])
            except IndexError:
                return by_id.get(ids[-1])
        if ctx.get("current_anime"):
            try:
                return by_id.get(int(ctx["current_anime"].get("anime_id")))
            except Exception:
                return None
        return None
    if _vg_is_scene_or_plot_query(p):
        it_scene, _ = _find_by_plot_description(p, kb["items"], kb=kb)
        if it_scene:
            return it_scene
    it, _ = _find_exact_or_fuzzy_title(p, kb["items"])
    if not it and len(p.split()) >= 3:
        it, _ = _find_by_plot_description(p, kb["items"], kb=kb)
    return it


def _vg_apply_season(it, clause_or_num, kb):
    if not it or not it.get("seasons"):
        return it
    n = None
    if isinstance(clause_or_num, int):
        n = clause_or_num
    else:
        clause_low = str(clause_or_num or "").lower()
        m = re.search(r"\bseason\s*(\d{1,2})\b|\b(\d{1,2})(?:st|nd|rd|th)\s+season\b", clause_low)
        if m:
            n = int(m.group(1) or m.group(2))
        else:
            for w, num_val in _WORD_NUMS.items():
                if re.search(rf"\b(?:season|s)\s+{w}\b|\b{w}\s+season\b", clause_low):
                    n = num_val
                    break
    if not n:
        return it
    by_id = kb["by_id"]
    for s in it["seasons"]:
        nm = (s.get("season_name") or "").lower()
        if re.search(rf"\bseason\s*{n}\b", nm):
            return by_id.get(s["anime_id"]) or it
    tv = [s for s in it["seasons"]
          if not re.search(r"movie|special|ova|oad|ona|recap|film", (s.get("season_name") or "").lower())]
    if 0 < n <= len(tv):
        return by_id.get(tv[n - 1]["anime_id"]) or it
    return it


# ── Local agentic command planner (works with no API key) ──
def _vg_split_clauses(msg):
    parts = re.split(r"\s*(?:,|;|\band then\b|\bthen\b|\band also\b|\band\b|&)\s*", msg, flags=re.I)
    verb_re = r"^(?:please\s+|can you\s+|could you\s+|also\s+|now\s+|let'?s\s+|vegeta\W*)*(?:" + "|".join(re.escape(v) for v in _VG_CMD_VERBS) + r")\b"
    out = []
    for p in parts:
        p = p.strip(" .!?")
        if not p:
            continue
        if out and not re.match(verb_re, p.lower()):
            out[-1] = out[-1] + " and " + p
        else:
            out.append(p)
    return out or [msg]


def _vg_parse_time(text):
    m = re.search(r"\bat\s+(\d{1,2}):(\d{2})\b", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"\b(?:at|from)\s+(\d{1,3})\s*(?:min|mins|minute|minutes|m)\b", text)
    if m:
        return int(m.group(1)) * 60
    return 0


def _vg_parse_clause(clause, kb, ctx, history):
    ctx = ctx or {}
    c = re.sub(r"^(?:\W*)(?:please\s+|can you\s+|could you\s+|also\s+|now\s+|vegeta\W*|yo\s+|hey\s+)+", "", clause.lower()).strip(" .!?")
    by_id = kb["by_id"]
    on_watch = ctx.get("current_view") == "view-watch"
    cw = ctx.get("continue_watching") or []

    # 1. Player: next / previous episode
    if re.match(r"^(?:play\s+|go to\s+|skip to\s+|start\s+)?(?:the\s+)?next(?:\s+(?:episode|ep))?(?:\s+please)?$|^skip(?:\s+(?:this|episode|this episode|it))?$", c):
        if not on_watch and cw:
            top = cw[0]
            return {"type": "play", "anime_id": top.get("anime_id"), "episode": int(top.get("episode") or 0) + 1,
                    "time": 0, "title": top.get("title")}
        return {"type": "player", "command": "next"}
    if re.match(r"^(?:play\s+|go to\s+|go back to\s+)?(?:the\s+)?(?:previous|prev|last)\s+(?:episode|ep)$|^go back an episode$", c):
        return {"type": "player", "command": "previous"}

    # 2. Player: audio language
    m = re.match(r"^(?:switch|change|put|set|turn)\b.*?\b(dub|dubbed|english|sub|subbed|subtitles|japanese)\b", c) \
        or re.match(r"^(?:in\s+)?(?:english\s+)?(dub|sub)(?:bed)?(?:\s+(?:it|please|version))?$", c) \
        or re.match(r"^(dub|sub)\s+it$", c)
    if m:
        lang = "dub" if m.group(1) in ("dub", "dubbed", "english") else "sub"
        return {"type": "player", "command": lang}

    # 2.5. Player: sound boost / pause / resume / fullscreen
    m_boost = re.match(r"^(?:boost|increase|set|turn)\s+(?:the\s+)?(?:sound|volume|audio)(?:\s+boost(?:er)?)?(?:\s+to)?\s*(200|150|100)?%?$", c) \
        or re.match(r"^(?:turn\s+on|enable)\s+(?:sound|volume|audio)\s+boost(?:er)?$", c)
    if m_boost:
        lvl = m_boost.group(1) if m_boost.lastindex and m_boost.group(1) else "200"
        return {"type": "player", "command": f"boost_{lvl}"}
    if c in ("pause", "pause video", "pause player", "pause it"):
        return {"type": "player", "command": "pause"}
    if c in ("fullscreen", "full screen", "go fullscreen"):
        return {"type": "player", "command": "fullscreen"}

    # 2.8. Watch Trailer command
    m_tr = re.match(r"^(?:watch|play|show|open|launch)\s+(?:the\s+)?(?:official\s+)?trailer\s+(?:for|of)\s+(.+)$", c) \
        or re.match(r"^(?:official\s+)?trailer\s+(?:for|of)\s+(.+)$", c) \
        or re.match(r"^(?:watch|play|show)\s+(.+?)\s+trailer$", c)
    if m_tr:
        it = _vg_resolve_target(m_tr.group(1), kb, ctx, history)
        if it:
            return {"type": "trailer", "anime_id": it["anime_id"], "title": _vg_title(it)}

    # 3. My List: remove / add
    m = re.match(rf"^(?:remove|delete|drop|take)\s+(.*?)\s*(?:from|off|out of)\s+{_VG_LIST_WORDS}$", c) \
        or re.match(r"^(?:remove|delete|unsave)\s+(.+)$", c)
    if m:
        it = _vg_resolve_target(m.group(1), kb, ctx, history)
        if not it:
            return {"error": "not_found", "query": m.group(1)}
        return {"type": "remove_from_list", "anime_id": it["anime_id"], "title": _vg_title(it)}
    m = re.match(rf"^(?:add|save|bookmark|put)\s+(.*?)\s*(?:to|in|into|on)\s+{_VG_LIST_WORDS}$", c) \
        or re.match(r"^(?:add|save|bookmark)\s+(.+)$", c)
    if m:
        it = _vg_resolve_target(m.group(1), kb, ctx, history)
        if not it:
            return {"error": "not_found", "query": m.group(1)}
        return {"type": "add_to_list", "anime_id": it["anime_id"], "title": _vg_title(it)}

    # 4. Surprise me / random pick
    if re.search(r"\b(surprise me|random anime|something random|anything random|pick (?:something|one|an anime|anything)(?: for me)?|i'?m bored|roll the dice|play (?:something|anything|a random))\b", c):
        genres = _detect_query_genres(c)
        _, picks = _vg_personal_recs(kb, ctx, limit=12, genres=genres or None)
        if len(picks) < 4:
            picks = _vg_search(kb["items"], genres=genres, min_score=7.9, sort="popularity", limit=25,
                               exclude_ids=_vg_taste(kb, ctx)["watched_ids"])
        if picks:
            it = _vg_random.choice(picks[:12])
            return {"type": "play", "anime_id": it["anime_id"], "episode": 1, "time": 0,
                    "title": _vg_title(it), "surprise": True}

    # 5. Navigation
    m = re.match(r"^(?:search|look up|search for|find me the anime|google)\s+(?:for\s+)?(.+?)(?:\s+on\s+kuroanime)?$", c)
    if m:
        return {"type": "navigate", "view": "search", "query": m.group(1).strip()}
    if re.match(r"^(?:go|take me|bring me|navigate|open|show)\b.*\b(?:home|homepage|home page|main page)$", c) or c in ("home", "go home"):
        return {"type": "navigate", "view": "home"}
    if re.match(rf"^(?:open|show|go to|take me to|navigate to|check|view)\s+{_VG_LIST_WORDS}$", c) or c in ("my list", "watchlist"):
        return {"type": "navigate", "view": "watchlist"}
    m = re.match(r"^(?:browse|explore)\b\s*(.*)$", c) \
        or re.match(r"^(?:open|go to|take me to|show)\s+(?:the\s+)?(.*?)\s*(?:genre|category|section|browse page|explore page)$", c)
    if m:
        genres = _detect_query_genres(m.group(1) or "")
        return {"type": "navigate", "view": "browse",
                "genre": _vg_genre_display(kb, genres[0]) if genres else ""}

    # 6. Open an anime page
    m = re.match(r"^(?:open|show me|show|go to|take me to|pull up|bring up)\s+(?:the\s+)?(?:page|details?|info)?\s*(?:for|of|on|about)?\s*(.+?)(?:\s+(?:page|details?|info))?$", c)
    if m and not re.search(r"\b(like|similar|recommend|some|something|best|top|anime with|list of|genres?|movies|shows)\b", m.group(1)):
        it = _vg_resolve_target(m.group(1), kb, ctx, history)
        if it:
            return {"type": "open", "anime_id": it["anime_id"], "title": _vg_title(it)}

    # 7. Play / resume / agentic auto-play
    m = re.match(r"^(?:let'?s\s+|i want to\s+|i wanna\s+|agentic\s+)?(auto[- ]?play:?|auto[- ]?launch:?|autoplay:?|play|start|watch|stream|put on|launch|begin|resume|continue)\b\s*(.*)$", c)
    if not m or re.match(r"^watch\s+order", c):
        return None
    soft = bool(re.match(r"^(?:let'?s|i want to|i wanna)\b", c)) or m.group(1) == "watch"
    rest = m.group(2).lstrip(": -")
    audio = None
    if re.search(r"\b(?:in\s+)?(?:english\s+)?dub(?:bed)?\b|\bin english\b", rest):
        audio = "dub"
    elif re.search(r"\b(?:in\s+)?(?:japanese|subbed|subtitled|subs?)\b", rest):
        audio = "sub"
    rest = re.sub(r"\b(?:in\s+)?(?:english\s+)?dub(?:bed)?\b|\bin english\b|\b(?:in\s+)?(?:japanese|subbed|subtitled|subs?)\b", " ", rest)
    seek = _vg_parse_time(rest)
    rest = re.sub(r"\b(?:at|from)\s+\d{1,2}:\d{2}\b|\b(?:at|from)\s+\d{1,3}\s*(?:min|mins|minute|minutes|m)\b", " ", rest)

    resume_words = re.match(r"^(?:watching|my show|my anime|where i left off|where i left|from where i left(?: off)?|my last show|last show|it|)\s*$", rest.strip())
    if m.group(1) in ("resume", "continue") and resume_words:
        if cw:
            top = cw[0]
            return {"type": "play", "anime_id": top.get("anime_id"), "episode": top.get("episode") or 1,
                    "time": top.get("progress_sec") or 0, "title": top.get("title"), "audio": audio, "is_resume": True}
        if ctx.get("current_anime"):
            ca = ctx["current_anime"]
            return {"type": "play", "anime_id": ca.get("anime_id"), "episode": ca.get("current_episode") or 1,
                    "time": 0, "title": ca.get("title"), "audio": audio, "is_resume": True}
        return {"error": "nothing_to_resume"}

    latest = bool(re.search(r"\b(?:latest|newest|last|final|most recent)\s+(?:episode|ep)\b", rest))
    clean_title_part, parsed_s_num, parsed_ep_num = _parse_play_query(rest)
    it = _vg_resolve_target(clean_title_part, kb, ctx, history)
    if not it:
        if any(k in clean_title_part for k in ["some anime", "an anime", "any anime", "random", "agentic", "something", "whatever", "some title"]):
            picks = [x for x in kb["items"] if float(x.get("score") or 0) >= 8.4 and (x.get("episode_count") or 0) > 0]
            if picks:
                it = _vg_random.choice(picks[:15])
        if not it:
            return None if soft else {"error": "not_found", "query": clean_title_part or rest}
    if parsed_s_num:
        it = _apply_season_target(it, parsed_s_num, kb)
    else:
        it = _vg_apply_season(it, rest, kb)
    total = it.get("episode_count") or 0
    ep = parsed_ep_num if parsed_ep_num is not None else (total if latest and total else 1)
    if total and ep > total:
        ep = total
    return {"type": "play", "anime_id": it["anime_id"], "episode": max(1, ep), "time": seek,
            "title": _vg_title(it), "audio": audio}


def _vegeta_plan_actions(user_msg, kb, ctx, history):
    if not user_msg:
        return None
    actions, failed = [], []
    for clause in _vg_split_clauses(user_msg.strip()):
        try:
            r = _vg_parse_clause(clause, kb, ctx, history)
        except Exception as e:
            print(f"Vegeta planner error on '{clause}': {e}")
            r = None
        if not r:
            continue
        (failed if r.get("error") else actions).append(r)
    if not actions and not failed:
        return None
    return {"actions": actions, "failed": failed}


def _vg_fmt_time(sec):
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


def _vg_describe_action(a):
    t = a.get("title") or "it"
    typ = a.get("type")
    if typ == "play":
        extra = f" from {_vg_fmt_time(a['time'])}" if a.get("time") else ""
        lang = " in English Dub" if a.get("audio") == "dub" else (" with subs" if a.get("audio") == "sub" else "")
        verb = "Resuming" if a.get("is_resume") else ("Rolling the dice: launching" if a.get("surprise") else "Launching")
        ep_val = a.get("episode", 1)
        return f"{verb} **{t}**, Episode {ep_val}{extra}{lang} [Watch on KuroAnime](#anime/{a.get('anime_id')}?ep={ep_val})"
    if typ == "open":
        return f"Opening the **{t}** page [Watch on KuroAnime](#anime/{a.get('anime_id')})"
    if typ == "trailer":
        return f"Launching the official trailer for **{t}** [Watch on KuroAnime](#anime/{a.get('anime_id')})"
    if typ == "add_to_list":
        return f"Adding **{t}** to your list"
    if typ == "remove_from_list":
        return f"Removing **{t}** from your list"
    if typ == "player":
        return {
            "next": "Skipping to the next episode",
            "previous": "Going back one episode",
            "dub": "Switching audio to English Dub",
            "sub": "Switching audio to Japanese with subs",
            "boost_200": "Boosting player audio to 200%",
            "boost_150": "Setting player audio boost to 150%",
            "boost_100": "Resetting player audio to 100%",
            "pause": "Pausing playback",
            "fullscreen": "Toggling fullscreen mode",
        }.get(a.get("command"), "Adjusting the player")
    if typ == "navigate":
        v = a.get("view")
        if v == "search":
            return f"Searching the archives for **{a.get('query')}**"
        if v == "browse":
            return f"Opening Browse{(' → **' + a['genre'] + '**') if a.get('genre') else ''}"
        return {"home": "Taking you home", "watchlist": "Opening your list"}.get(v, "Navigating")
    return "Done"


def _vg_compose_plan_reply(plan):
    acts, failed = plan.get("actions") or [], plan.get("failed") or []
    lines = []
    if acts:
        lines.append(_vg_random.choice([
            "Hmph. Consider it done, warrior.",
            "As you command. Don't make me repeat myself.",
            "Tch, fine. The Prince handles it.",
            "Done before you could blink, low-class clown.",
        ]))
        lines.append("")
        lines += [f"- {_vg_describe_action(a)}" for a in acts]
    for f in failed:
        if f.get("error") == "nothing_to_resume":
            lines.append("Tch! There's nothing in your Continue Watching to resume. Pick a show first, weakling.")
        else:
            q = (f.get("query") or "").strip() or "that"
            lines.append(f"I searched every corner of the archives and found nothing called **{q}**. Check the spelling, warrior.")
    return "\n".join(lines).strip()


def _vg_validate_actions(actions, kb):
    by_id = kb["by_id"]
    out = []
    for a in actions or []:
        if not isinstance(a, dict) or not a.get("type"):
            continue
        if a["type"] in ("play", "open", "trailer", "add_to_list", "remove_from_list"):
            try:
                aid = int(a.get("anime_id"))
            except Exception:
                continue
            it = by_id.get(aid)
            if not it and a["type"] != "remove_from_list":
                continue
            a["anime_id"] = aid
            if it:
                a["title"] = a.get("title") or _vg_title(it)
            if a["type"] == "play":
                try:
                    a["episode"] = max(1, int(a.get("episode") or 1))
                except Exception:
                    a["episode"] = 1
                try:
                    a["time"] = max(0, int(float(a.get("time") or 0)))
                except Exception:
                    a["time"] = 0
        out.append(a)
    return out[:6]


# ── Smart non-command answers (local brain, no API key needed) ──
def _vg_format_list(header, items, outro=None):
    lines = [header, ""]
    for i, it in enumerate(items, 1):
        bits = [f"★ {it.get('score')}", it.get("type") or "TV"]
        if it.get("episode_count"):
            bits.append(f"{it['episode_count']} eps")
        if _vg_year(it):
            bits.append(str(_vg_year(it)))
        syn = (it.get("synopsis") or "").strip()
        syn = (syn[:120].rsplit(" ", 1)[0] + "...") if len(syn) > 120 else syn
        lines.append(f"**{i}. {_vg_title(it)}** · {' · '.join(bits)} [Watch on KuroAnime](#anime/{it['anime_id']})")
        if syn:
            lines.append(f"   {syn}")
    if outro:
        lines += ["", outro]
    return "\n".join(lines)


def _vg_smart_answer(user_msg, kb, ctx, history):
    msg = (user_msg or "").strip()
    low = msg.lower().strip(" ?!.")
    items = kb["items"]
    ctx = ctx or {}
    cw = ctx.get("continue_watching") or []

    # Greetings / small talk
    if len(low.split()) <= 6 and re.match(r"^(hi|hey|hello|yo|sup|salam|assalam\w*|aoa|hola|wassup|what'?s up|kya haal|how are you|good (?:morning|evening|night))\b", low):
        if cw:
            top = cw[0]
            return (f"Hmph. Back again? You still haven't finished **{top.get('title')}** (Ep {top.get('episode')}). "
                    f"Say \"resume\" and I'll drop you right back in. [Watch on KuroAnime](#anime/{top.get('anime_id')})"), []
        return ("Hmph. The Prince of all Saiyans acknowledges you. Tell me what you're in the mood for, "
                "describe an anime you forgot, drop a screenshot, or just say \"surprise me\"."), []
    if re.match(r"^(thanks|thank you|thx|ty|shukriya|jazakallah|appreciate it)\b", low):
        return "Hmph. Don't get used to my generosity, warrior.", []

    # Compare X vs Y
    m = re.search(r"(?:compare\s+)?(.+?)\s+(?:vs\.?|versus|or|and)\s+(.+?)$", re.sub(r"^(?:compare|which is better[,:]?|what'?s better[,:]?|should i watch)\s+", "", low)) \
        if re.search(r"\b(vs\.?|versus|compare|which is better|what'?s better|should i watch .+ or)\b", low) else None
    if m:
        a = _vg_resolve_target(m.group(1), kb, ctx, history)
        b = _vg_resolve_target(m.group(2), kb, ctx, history)
        if a and b and a["anime_id"] != b["anime_id"]:
            def row(it):
                return (f"- **{_vg_title(it)}**: ★ {it.get('score')} · {it.get('episode_count') or '?'} eps · "
                        f"{_vg_year(it) or '?'} · {it.get('studios') or 'Unknown studio'} · "
                        f"{', '.join(sorted(_vg_genre_set(it))[:4]).title()} [Watch on KuroAnime](#anime/{it['anime_id']})")
            win, lose = (a, b) if (a.get("score") or 0) >= (b.get("score") or 0) else (b, a)
            shared = sorted(_vg_genre_set(a) & _vg_genre_set(b))
            verdict = (f"**{_vg_title(win)}** edges it on raw power (★ {win.get('score')} vs {lose.get('score')}). "
                       + (f"Both hit {', '.join(shared[:3])}, so you can't go wrong. " if shared else "They're completely different beasts, so pick your mood. ")
                       + ("Short on time? " + (f"**{_vg_title(a)}** is the quicker watch." if (a.get('episode_count') or 999) < (b.get('episode_count') or 999) else f"**{_vg_title(b)}** is the quicker watch.")))
            return "\n".join(["## Saiyan Scouter: Head to Head", "", row(a), row(b), "", f"**Verdict:** {verdict}"]), []

    # "Anime like X"
    m = re.search(r"\b(?:like|similar to|same vibe as|vibes? like|if i (?:liked|loved|enjoyed))\s+(.+?)$", low)
    if m and not re.match(r"^(?:i|do you|would you)\s+like\b", low):
        seed = _vg_resolve_target(m.group(1), kb, ctx, history)
        if seed:
            sims = _vg_find_similar(seed, items, limit=6, exclude_ids=_vg_taste(kb, ctx)["watched_ids"])
            if sims:
                return _vg_format_list(
                    f"Hmph. If **{_vg_title(seed)}** got your blood pumping, these share its DNA:", sims,
                    "Say \"play the first one\" or \"add the second one to my list\" and I'll handle it."), []

    if _vg_is_scene_or_plot_query(low):
        cands = _vg_identify_scene_candidates(low, items, kb=kb, limit=3)
        if cands:
            _, p_item, p_ep, _ = cands[0]
            p_ep = p_ep or 1
            p_title = _vg_title(p_item)
            p_aid = p_item["anime_id"]
            p_syn = re.sub(r"<[^>]+>", "", (p_item.get("synopsis") or "A legendary battle chronicled in the archives.")).strip()
            if len(p_syn) > 280:
                p_syn = p_syn[:277].rsplit(" ", 1)[0] + "..."
            p_genres = p_item.get("genres") or "Action, Fantasy"
            p_score = p_item.get("score") or "8.6+"
            reply = (
                f"Hmph! You thought a vague memory of a scene could stump the Prince of all Saiyans? My Royal Scouter locked onto that signature in a microsecond!\n\n"
                f"🔥 **Primary Scouter Lock: [{p_title}](#anime/{p_aid}?ep={p_ep})**\n"
                f"- **Power Rating:** ⭐ `{p_score}` | **Format:** `{p_item.get('type') or 'TV'}` (`{p_item.get('episode_count') or '?'} eps`)\n"
                f"- **Genres:** {p_genres}\n"
                f"- **Archive Synopsis:** {p_syn}\n"
                f"- **Direct Command:** **[Watch {p_title} (Ep {p_ep}) Now](#anime/{p_aid}?ep={p_ep})**"
            )
            if len(cands) > 1:
                runners = []
                for _, r_it, r_ep, _ in cands[1:3]:
                    r_ep = r_ep or 1
                    r_sc = r_it.get("score") or "8.0+"
                    r_tit = _vg_title(r_it)
                    runners.append(f"- **[{r_tit}](#anime/{r_it['anime_id']}?ep={r_ep})** (⭐ `{r_sc}`, {r_it.get('genres') or 'Anime'})")
                reply += "\n\n⚡ **Secondary Scouter Matches (if you meant another timeline):**\n" + "\n".join(runners)
            wants_play = bool(re.search(r"\b(play|watch|start|stream|open|launch|put on)\b", low)) and not bool(re.search(r"\b(what is|which anime|what anime|tell me|name of|identify|find the anime)\b", low))
            acts = []
            if wants_play:
                acts.append({
                    "type": "play",
                    "anime_id": p_aid,
                    "title": p_title,
                    "episode": p_ep,
                    "audio": "dub" if "dub" in low else "sub"
                })
            return reply, acts

    filters = _vg_parse_filters(low)
    is_request = re.search(r"\b(recommend|suggest|give me|show me|list|top|best|any|some|find|what are|which|good|need|want|looking for)\b", low)

    # Personalised recommendations
    if re.search(r"\b(what should i watch|watch next|what next|recommend me|suggest me|for me|based on my|my taste|something new|i haven'?t seen)\b", low) \
            and not any(k in filters for k in ("year_min", "year_max", "min_score", "type", "max_eps", "min_eps", "status")):
        t, picks = _vg_personal_recs(kb, ctx, limit=5, genres=filters.get("genres"))
        if picks:
            basis = ", ".join(f"**{x}**" for x in t["titles"][:3])
            return _vg_format_list(
                f"I've studied your battle record ({basis}). You clearly crave {', '.join(t['top_genres'][:3]) or 'real power'}. Watch these next:",
                picks, "Say \"play the first one\" and I'll launch it."), []

    # Filtered catalog search (year / score / format / length / status / top-N)
    hard = [k for k in ("year_min", "year_max", "min_score", "type", "max_eps", "min_eps", "status", "limit") if k in filters]
    if hard and (is_request or filters.get("genres") or filters.get("type")):
        res = _vg_search(items, genres=filters.get("genres"), year_min=filters.get("year_min"), year_max=filters.get("year_max"),
                         min_score=filters.get("min_score"), type=filters.get("type"), max_eps=filters.get("max_eps"),
                         min_eps=filters.get("min_eps"), status=filters.get("status"),
                         sort=filters.get("sort", "popularity"), limit=filters.get("limit", 6))
        desc = []
        if filters.get("genres"):
            desc.append(", ".join(g.title() for g in filters["genres"]))
        if filters.get("type") == "MOVIE":
            desc.append("movies")
        if filters.get("year_min") and filters.get("year_min") == filters.get("year_max"):
            desc.append(f"from {filters['year_min']}")
        elif filters.get("year_min") or filters.get("year_max"):
            desc.append(f"{filters.get('year_min') or '...'}–{filters.get('year_max') or 'now'}")
        if filters.get("min_score"):
            desc.append(f"rated {filters['min_score']}+")
        if filters.get("max_eps"):
            desc.append(f"under {filters['max_eps']} eps")
        if filters.get("status") == "airing":
            desc.append("currently airing")
        label = " · ".join(desc) or "your filters"
        if not res:
            return f"Tch. Nothing in the archives matches **{label}**. Loosen your demands, warrior.", []
        return _vg_format_list(f"Scouter locked on **{label}**. Here's what survives my standards:", res,
                               "Want me to launch one? Just say \"play the first one\"."), []
    return None


def _vg_execute_tool(name, args, kb, ctx):
    items = kb["items"]
    by_id = kb["by_id"]
    args = args or {}
    if name == "search_catalog":
        res = _vg_search(
            items,
            genres=args.get("genres"),
            query=args.get("query"),
            year_min=args.get("year_min"),
            year_max=args.get("year_max"),
            min_score=args.get("min_score"),
            type=args.get("type"),
            max_eps=args.get("max_eps"),
            min_eps=args.get("min_eps"),
            status=args.get("status"),
            sort=args.get("sort", "popularity"),
            limit=args.get("limit", 6)
        )
        return {"results": [_vg_card(it, syn_len=180) for it in res], "count": len(res)}
    elif name == "get_anime_details":
        aid = args.get("anime_id")
        t = args.get("title")
        it = by_id.get(int(aid)) if aid else None
        if not it and t:
            it, _ = _find_exact_or_fuzzy_title(t, items)
        if not it:
            return {"error": "Anime not found in KuroAnime archives"}
        card = _vg_card(it, syn_len=400)
        card["seasons"] = it.get("seasons") or []
        card["studios"] = it.get("studios")
        return {"anime": card}
    elif name == "find_similar_anime":
        aid = args.get("anime_id")
        t = args.get("title")
        seed = by_id.get(int(aid)) if aid else None
        if not seed and t:
            seed, _ = _find_exact_or_fuzzy_title(t, items)
        if not seed:
            return {"error": "Seed anime not found"}
        sims = _vg_find_similar(seed, items, limit=args.get("limit", 5))
        return {"seed": _vg_title(seed), "results": [_vg_card(it, syn_len=140) for it in sims]}
    elif name == "identify_anime_from_description":
        desc = str(args.get("description") or "").strip().lower()
        cands = _vg_identify_scene_candidates(desc, items, kb=kb, limit=5)
        out = []
        for _, it, matched_ep, _ in cands:
            out.append({
                "anime_id": it["anime_id"],
                "title": _vg_title(it),
                "matched_episode": matched_ep or 1,
                "score": it.get("score"),
                "genres": it.get("genres"),
                "synopsis": (it.get("synopsis") or "")[:360]
            })
        return {"identified_candidates": out}
    elif name == "watch_trailer":
        q = str(args.get("title") or "").strip()
        it = _vg_resolve_target(q, kb, ctx)
        if not it:
            return {"error": f"Anime '{q}' not found in database."}
        return {
            "action_queued": True,
            "type": "trailer",
            "anime_id": int(it["anime_id"]),
            "title": _vg_title(it)
        }
    elif name == "get_user_profile":
        t = _vg_taste(kb, ctx)
        return {"watched_titles": t["titles"], "top_genres": t["top_genres"]}
    elif name == "play_anime":
        aid = args.get("anime_id")
        it = by_id.get(int(aid)) if aid else None
        return {
            "action_queued": True,
            "type": "play",
            "anime_id": int(aid),
            "episode": int(args.get("episode") or 1),
            "time": int(args.get("time") or 0),
            "audio": args.get("audio"),
            "title": _vg_title(it) if it else "Anime"
        }
    elif name == "open_anime_page":
        aid = args.get("anime_id")
        it = by_id.get(int(aid)) if aid else None
        return {
            "action_queued": True,
            "type": "open",
            "anime_id": int(aid),
            "title": _vg_title(it) if it else "Anime"
        }
    elif name == "add_to_my_list":
        aid = args.get("anime_id")
        it = by_id.get(int(aid)) if aid else None
        return {
            "action_queued": True,
            "type": "add_to_list",
            "anime_id": int(aid),
            "title": _vg_title(it) if it else "Anime"
        }
    elif name == "remove_from_my_list":
        aid = args.get("anime_id")
        it = by_id.get(int(aid)) if aid else None
        return {
            "action_queued": True,
            "type": "remove_from_list",
            "anime_id": int(aid),
            "title": _vg_title(it) if it else "Anime"
        }
    elif name == "control_player":
        return {
            "action_queued": True,
            "type": "player",
            "command": args.get("command")
        }
    elif name == "navigate":
        return {
            "action_queued": True,
            "type": "navigate",
            "view": args.get("view"),
            "genre": args.get("genre") or "",
            "query": args.get("query") or ""
        }
    return {"error": f"Unknown tool {name}"}


_GEMINI_TOOLS_DECLARATIONS = [
    {
        "name": "search_catalog",
        "description": "Search KuroAnime's database of 10,700+ anime titles by query, genres, year range, minimum score, format, or max episodes.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "Title or keyword keywords"},
                "genres": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Genre names e.g. ['Action', 'Fantasy']"},
                "year_min": {"type": "INTEGER", "description": "Earliest year"},
                "year_max": {"type": "INTEGER", "description": "Latest year"},
                "min_score": {"type": "NUMBER", "description": "Minimum score out of 10"},
                "type": {"type": "STRING", "description": "TV, MOVIE, OVA, ONA"},
                "max_eps": {"type": "INTEGER", "description": "Maximum episode count"},
                "sort": {"type": "STRING", "description": "popularity, score, or newest"},
                "limit": {"type": "INTEGER", "description": "Max results to return (default 6)"}
            }
        }
    },
    {
        "name": "get_anime_details",
        "description": "Fetch full facts, score, episodes, seasons and synopsis for an anime by its numeric ID or title.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "anime_id": {"type": "INTEGER", "description": "Numeric KuroAnime ID"},
                "title": {"type": "STRING", "description": "Title to look up if ID unknown"}
            }
        }
    },
    {
        "name": "find_similar_anime",
        "description": "Find anime with similar themes, genres, and feel to a given anime.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "anime_id": {"type": "INTEGER", "description": "Source anime ID"},
                "title": {"type": "STRING", "description": "Source anime title if ID unknown"},
                "limit": {"type": "INTEGER", "description": "Max results (default 5)"}
            }
        }
    },
    {
        "name": "identify_anime_from_description",
        "description": "Identify an anime (and specific episode if applicable) from a user's scene description, plot summary, character traits, or vague memory.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description": {"type": "STRING", "description": "The full scene, plot, or character description from the user."}
            },
            "required": ["description"]
        }
    },
    {
        "name": "watch_trailer",
        "description": "Open an anime's detail page and immediately play its official trailer in the browser.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "title": {"type": "STRING", "description": "Exact or approximate title or description of the anime."}
            },
            "required": ["title"]
        }
    },
    {
        "name": "get_user_profile",
        "description": "Get the user's continue watching history, saved watchlist, and taste preferences.",
        "parameters": {
            "type": "OBJECT",
            "properties": {}
        }
    },
    {
        "name": "play_anime",
        "description": "Autonomously start streaming an anime episode at an optional timestamp in Japanese Sub or English Dub.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "anime_id": {"type": "INTEGER", "description": "Numeric KuroAnime ID to play"},
                "episode": {"type": "INTEGER", "description": "Episode number (defaults to 1)"},
                "time": {"type": "INTEGER", "description": "Timestamp in seconds to seek to (optional)"},
                "audio": {"type": "STRING", "description": "'sub' or 'dub'"}
            },
            "required": ["anime_id"]
        }
    },
    {
        "name": "open_anime_page",
        "description": "Open an anime's detail page on the user's screen without starting playback.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "anime_id": {"type": "INTEGER", "description": "Numeric KuroAnime ID"}
            },
            "required": ["anime_id"]
        }
    },
    {
        "name": "add_to_my_list",
        "description": "Add an anime to the user's saved watchlist / library.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "anime_id": {"type": "INTEGER", "description": "Numeric KuroAnime ID"}
            },
            "required": ["anime_id"]
        }
    },
    {
        "name": "remove_from_my_list",
        "description": "Remove an anime from the user's saved watchlist / library.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "anime_id": {"type": "INTEGER", "description": "Numeric KuroAnime ID"}
            },
            "required": ["anime_id"]
        }
    },
    {
        "name": "control_player",
        "description": "Control the in-browser video player: next episode, previous episode, or switch audio.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "command": {"type": "STRING", "description": "'next', 'previous', 'dub', or 'sub'"}
            },
            "required": ["command"]
        }
    },
    {
        "name": "navigate",
        "description": "Navigate the web app to a screen or search query.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "view": {"type": "STRING", "description": "'home', 'browse', 'search', or 'watchlist'"},
                "genre": {"type": "STRING", "description": "Genre name for browse view"},
                "query": {"type": "STRING", "description": "Search query"}
            },
            "required": ["view"]
        }
    }
]


ANIME_CHARACTER_LORE = {
    # Demon Slayer
    "tanjiro": {
        "name": "Tanjiro Kamado",
        "aliases": ["tanjiro", "kamado tanjiro"],
        "anime_title": "Demon Slayer: Kimetsu no Yaiba",
        "anime_id": 843,
        "summary": "The courageous Sun Breathing swordsman and protagonist of Demon Slayer. After Muzan Kibutsuji slaughtered his family and transformed his sister Nezuko into a demon, he joined the Demon Slayer Corps with an unbreakable vow to cure her.",
        "vegeta_take": "What I respect about the boy is his tenacity. Even with shattered ribs and lungs burning from oxygen deprivation against Upper Rank demons, he refuses to surrender. A warrior with that level of resolve earns my royal respect."
    },
    "nezuko": {
        "name": "Nezuko Kamado",
        "aliases": ["nezuko", "tanjiro sister", "his sister"],
        "anime_title": "Demon Slayer: Kimetsu no Yaiba",
        "anime_id": 843,
        "summary": "Tanjiro's younger sister who was turned into a demon by Muzan Kibutsuji. Unlike wild bloodthirsty demons, she retained her human compassion through sheer willpower. She sleeps in a wooden box to restore her stamina rather than consuming human flesh.",
        "vegeta_take": "A demon who conquers her feral bloodlust through sheer discipline, wielding Blood Demon Art pyrokinesis to burn enemy demons while shielding allies. Her spirit is fiercer than most full-grown warriors."
    },
    "zenitsu": {
        "name": "Zenitsu Agatsuma",
        "aliases": ["zenitsu"],
        "anime_title": "Demon Slayer: Kimetsu no Yaiba",
        "anime_id": 843,
        "summary": "Thunder Breathing swordsman who mastered the First Form: Thunderclap and Flash to sheer supersonic perfection. Despite his constant whining and cowardice when awake, he enters a lightning-fast trance when unconscious or pushed to the brink.",
        "vegeta_take": "Tch! The boy whines like a crybaby when awake, but the instant his subconscious takes command, his supersonic lightning speed is undeniable. If he conquered his fear permanently, he would be a lethal weapon."
    },
    "inosuke": {
        "name": "Inosuke Hashibira",
        "aliases": ["inosuke"],
        "anime_title": "Demon Slayer: Kimetsu no Yaiba",
        "anime_id": 843,
        "summary": "The wild boar-masked warrior who invented Beast Breathing living in the mountains. Dual-wielding serrated blades, his spatial awareness and brute battle instincts are untamed.",
        "vegeta_take": "A reckless, loudmouthed brawler whose feral instinct and battle hunger remind me of a lower-class Saiyan in the wilderness. He rushes headfirst into danger without a second thought!"
    },
    "rengoku": {
        "name": "Kyojuro Rengoku",
        "aliases": ["rengoku", "flame hashira"],
        "anime_title": "Demon Slayer: Kimetsu no Yaiba",
        "anime_id": 843,
        "summary": "The Flame Hashira who lived by the code: 'Set your heart ablaze!' (Kokoro o moyase). In the Mugen Train clash against Upper Rank Three Akaza, he fought to his dying breath, ensuring all 200 passengers survived.",
        "vegeta_take": "A true warrior's death! Rengoku refused Akaza's offer of demon immortality, choosing to burn his mortal soul to ash to protect the weak. Even a Saiyan Prince bows his head in tribute to an honorable sacrifice like that."
    },
    "muzan": {
        "name": "Muzan Kibutsuji",
        "aliases": ["muzan", "demon king"],
        "anime_title": "Demon Slayer: Kimetsu no Yaiba",
        "anime_id": 843,
        "summary": "The Demon King and progenitor of all demons in Japan. Terrified of the Sun Breathing sun earrings and death, he commands the Twelve Kizuki from his shape-shifting Infinity Castle.",
        "vegeta_take": "An arrogant, pathetic coward obsessed with biological immortality. For all his biological horrors, the moment he faced Yoriichi Tsugikuni, he wet his boots in terror. True power comes from discipline and pride, not hiding in shadows!"
    },

    # Jujutsu Kaisen
    "gojo": {
        "name": "Satoru Gojo",
        "aliases": ["gojo", "satoru", "satoru gojo", "the honored one"],
        "anime_title": "JUJUTSU KAISEN",
        "anime_id": 237,
        "summary": "The Honored One of the modern Jujutsu world. Born with both the Six Eyes and the Limitless cursed technique, he commands Infinity, Blue (attraction), Red (repulsion), Hollow Purple (matter erasure), and the domain Unlimited Void.",
        "vegeta_take": "Hmph! Satoru Gojo walks with immense arrogance, boasting he alone is honored throughout heaven and earth. Normally I despise cocky posturing, but with the Six Eyes and Unlimited Void, he actually has the godly power to back up his words."
    },
    "sukuna": {
        "name": "Ryomen Sukuna",
        "aliases": ["sukuna", "king of curses", "ryomen sukuna"],
        "anime_title": "JUJUTSU KAISEN",
        "anime_id": 237,
        "summary": "The King of Curses, an ancient four-armed catastrophe from the Heian era. His domain Malevolent Shrine shreds everything in its radius with Cleave and Dismantle, culminating in his World-Cutting Slash.",
        "vegeta_take": "Pure, unapologetic malice. Sukuna lives solely for blood and destruction, treating sorcerers like mere insects beneath his sandals. A terrifying, peerless monster who commands respect through overwhelming lethality."
    },
    "yuji": {
        "name": "Yuji Itadori",
        "aliases": ["yuji", "itadori"],
        "anime_title": "JUJUTSU KAISEN",
        "anime_id": 237,
        "summary": "The boy with superhuman physical strength who ingested Sukuna's cursed finger to protect his friends. Master of Divergent Fist and unprecedented consecutive Black Flash strikes.",
        "vegeta_take": "A young vessel who stares down death without flinching. His physical conditioning and raw brawling grit are impressive, but his greatest asset is his unbreakable soul."
    },
    "toji": {
        "name": "Toji Fushiguro",
        "aliases": ["toji", "sorcerer killer"],
        "anime_title": "JUJUTSU KAISEN",
        "anime_id": 237,
        "summary": "The Sorcerer Killer born under Heavenly Restriction. Possessing zero cursed energy in exchange for peak superhuman physical prowess, five senses dialed to supernatural levels, and the Inverted Spear of Heaven.",
        "vegeta_take": "A warrior who cast aside cursed energy to hone flesh and instinct to peak lethality! Even Gojo was nearly gutted by his tactical ambush. He fought with the viciousness of an assassin who bowed to no clan."
    },

    # Attack on Titan
    "eren": {
        "name": "Eren Yeager",
        "aliases": ["eren", "yeager", "attack titan"],
        "anime_title": "Attack on Titan",
        "anime_id": 240,
        "summary": "The boy from Shiganshina who witnessed his mother's death and swore to eradicate every titan. Bearing the Attack Titan, Founding Titan, and War Hammer Titan, his dark pursuit of freedom led to the devastating global Rumbling.",
        "vegeta_take": "Eren Yeager traded his humanity and morality for unbridled power to protect his people. That dark, unyielding resolve to trample the entire world under the Rumbling... a ruthless, tragic warrior who refused to wear a collar."
    },
    "levi": {
        "name": "Levi Ackerman",
        "aliases": ["levi", "captain levi", "humanity's strongest"],
        "anime_title": "Attack on Titan",
        "anime_id": 240,
        "summary": "Humanity's Strongest Soldier and Scout Regiment Captain. Master of rotational omni-directional mobility gear and dual blades, famously carving the Beast Titan into ribbons.",
        "vegeta_take": "Cold, calculated, and terrifyingly efficient. Levi Ackerman doesn't waste energy on grand speeches—he just slices titans to pieces with blistering rotational momentum. A true soldier."
    },

    # Naruto & Shippuden
    "naruto": {
        "name": "Naruto Uzumaki",
        "aliases": ["naruto", "uzumaki naruto"],
        "anime_title": "Naruto",
        "anime_id": 1335,
        "summary": "The Nine-Tails Jinchuriki who went from the Hidden Leaf's hated pariah to the Seventh Hokage. Master of Shadow Clones, Rasengan, Toad Sage Mode, and Six Paths Kurama Link Mode.",
        "vegeta_take": "A stubborn fool whose relentless 'never give up' ninja way mirrors Kakarot's irritating optimism. But I cannot deny his growth: turning a village of hatred into allies through sweat and battle is a feat worthy of a warrior."
    },
    "sasuke": {
        "name": "Sasuke Uchiha",
        "aliases": ["sasuke", "uchiha sasuke"],
        "anime_title": "Naruto",
        "anime_id": 1335,
        "summary": "The last survivor of the Uchiha massacre. Wielding the Sharingan, Mangekyo, Amaterasu, and Susanoo, his life was consumed by an obsession to avenge his clan against his brother Itachi.",
        "vegeta_take": "Sasuke let wounded pride and vengeance drive him into darkness, defecting from the Hidden Leaf to seek forbidden strength under Orochimaru. As a prince who understands the torment of wounded pride, I recognize his hunger for power—even if his methods were reckless."
    },
    "itachi": {
        "name": "Itachi Uchiha",
        "aliases": ["itachi", "itachi uchiha"],
        "anime_title": "Naruto: Shippuden",
        "anime_id": 600,
        "summary": "The tragic genius who slaughtered his own clan under secret orders from Danzo to prevent a catastrophic civil war, bearing the label of traitor to keep his little brother Sasuke alive.",
        "vegeta_take": "A shinobi who lived in absolute self-sacrifice and shadow. To execute your own clan to save the realm and accept the world's eternal hatred... that burden takes mental fortitude greater than most warriors possess."
    },

    # Solo Leveling
    "jinwoo": {
        "name": "Sung Jinwoo",
        "aliases": ["jinwoo", "sung jinwoo", "shadow monarch"],
        "anime_title": "Solo Leveling",
        "anime_id": 235,
        "summary": "The 'Weakest Hunter of All Mankind' who survived the double dungeon Cartenon Temple and unlocked the System. Endless leveling transformed him into Ashborn's successor: the dread Shadow Monarch commanding an immortal shadow army with the decree 'ARISE'.",
        "vegeta_take": "Hmph! Sung Jinwoo started at rock bottom, weaker than a Saibaman. But instead of crying over his fate, he grinded through dungeons with blood and iron until he commanded an immortal army of shadows. That kind of relentless self-evolution is worthy of royal praise!"
    },

    # One Punch Man
    "saitama": {
        "name": "Saitama",
        "aliases": ["saitama", "caped baldy"],
        "anime_title": "One-Punch Man",
        "anime_id": 1386,
        "summary": "The hero for fun who did 100 push-ups, sit-ups, squats, and a 10km run daily until his hair fell out and he shattered his mortal limiter. Capable of obliterating any cosmic foe with a casual punch.",
        "vegeta_take": "Hmph! A parody anomaly who broke his limiter through basic calisthenics! His physical impact and nonchalant strength are absurd, but facing divine God Ki and Ultra Instinct would test his bored attitude. Still, I admit... his punch is terrifying."
    },

    # Dragon Ball & Saiyans
    "goku": {
        "name": "Son Goku (Kakarot)",
        "aliases": ["goku", "kakarot"],
        "anime_title": "Dragon Ball Z",
        "anime_id": 1380,
        "summary": "Vegeta's eternal rival and the Earth-raised Saiyan who unlocked Super Saiyan, Super Saiyan God, Blue, and Mastered Ultra Instinct.",
        "vegeta_take": "Tch! Kakarot is an infuriating simpleton with an insatiable appetite for battle! But he pushes me beyond every mortal ceiling. Our rivalry is carved into the very stars of Universe 7!"
    },
    "vegeta": {
        "name": "Prince Vegeta",
        "aliases": ["vegeta", "prince vegeta"],
        "anime_title": "Dragon Ball Super",
        "anime_id": 1101,
        "summary": "The Prince of all Saiyans, master of the Final Flash, Big Bang Attack, Super Saiyan Blue Evolved, and God of Destruction power: Ultra Ego.",
        "vegeta_take": "I am Prince Vegeta! The proud warrior who bows to no god and forges his own destiny through pure royal blood and relentless discipline!"
    },
    "beerus": {
        "name": "Lord Beerus",
        "aliases": ["beerus", "lord beerus", "god of destruction"],
        "anime_title": "Dragon Ball Super",
        "anime_id": 1101,
        "summary": "The God of Destruction of Universe 7, commanding millions of years of destruction mastery and Hakai.",
        "vegeta_take": "Beerus is a pinnacle of divine destruction. While he pushed me around on Earth, my training with him unlocked Ultra Ego. One day, the God of Destruction will bow to the Prince of all Saiyans!"
    }
}


def _vg_extract_active_context(history, user_context, kb):
    active_anime = None
    by_id = kb.get("by_id", {})
    items = kb.get("items", [])

    # 1. Check history in reverse (most recent first)
    for h in reversed(history or []):
        t = h.get("text") or ""
        # Look for #anime/(\d+)
        m = re.findall(r"#anime/(\d+)", t)
        if m:
            aid = int(m[0])
            if aid in by_id:
                active_anime = by_id[aid]
                break
        # Look for ## [Title] or **[Title]**
        m2 = re.findall(r"##\s*([^\n*]+)", t) or re.findall(r"\*\*([^*]+)\*\*", t)
        for cand_title in m2:
            cand_title = cand_title.strip()
            if len(cand_title) >= 3 and not any(w in cand_title.lower() for w in ["scouting", "verdict", "dossier", "command", "report"]):
                it = next((x for x in items if (x.get("title_english") or "").lower() == cand_title.lower() or (x.get("title") or "").lower() == cand_title.lower()), None)
                if it:
                    active_anime = it
                    break
        if active_anime:
            break

    # 2. Check user_context
    if not active_anime and user_context:
        ca = user_context.get("current_anime")
        if ca and ca.get("anime_id"):
            try:
                active_anime = by_id.get(int(ca["anime_id"]))
            except Exception:
                pass
        if not active_anime and user_context.get("continue_watching"):
            try:
                active_anime = by_id.get(int(user_context["continue_watching"][0].get("anime_id")))
            except Exception:
                pass

    return active_anime


def _vg_handle_saiyan_banter(low):
    if re.search(r"\b(who is stronger|who'?d win|vs|versus)\b.*\b(you|vegeta)\b.*\b(goku|kakarot)\b", low) or \
       re.search(r"\b(who is stronger|who'?d win|vs|versus)\b.*\b(goku|kakarot)\b.*\b(you|vegeta)\b", low):
        return (
            "Tch! Don't push your luck, warrior!\n\n"
            "Kakarot may have unlocked Ultra Instinct with Whis's angelic evasion, but I forged my own path through pure divine destruction—**Ultra Ego**! "
            "The more damage and battle thirst I absorb, the hotter my destruction energy flares! In the Gravity Chamber, Kakarot and I push each other beyond mortal boundaries every single day.\n\n"
            "He may get the cheers from Earthlings, but as Prince of all Saiyans, my royal pride and Saiyan blood will never remain in second place!"
        ), None

    if re.search(r"\b(can you beat|beat|vs|versus|fight)\b.*\b(beerus|lord beerus)\b", low):
        return (
            "Hmph! Lord Beerus possesses millions of years of destruction mastery and Hakai. "
            "Back during his arrival on Earth when he dared strike Bulma, I landed a furious barrage on him (\"MY BULMA!\"), but even then he was barely tapping a fraction of his true divine power.\n\n"
            "While training on Beerus's planet, I learned to wield Hakai and attained Ultra Ego. I haven't surpassed him yet, but mark my words: a Saiyan knows no ceiling. "
            "One day, the God of Destruction will yield to the Prince of all Saiyans!"
        ), None

    if re.search(r"\b(goku vs saitama|saitama vs goku|can saitama beat goku|can goku beat saitama|who is stronger goku or saitama)\b", low):
        return (
            "Hmph! That ridiculous yellow-suited bald clown from City Z?!\n\n"
            "Saitama is a parody anomaly who broke his mortal limiter with basic push-ups and squats! His physical impact and Serious Punch are absurd, "
            "but Kakarot commands divine God Ki, Super Saiyan forms, and Mastered Ultra Instinct—allowing him to dodge attacks autonomously without relying on human reflexes, and shake entire universes with his blows.\n\n"
            "If they were to clash in a serious arena, Saitama's nonchalant attitude would be pulverized once God Ki enters the equation. "
            "Though I must admit... watching those two trade planet-shattering punches would be one hell of a spectacle!"
        ), None

    if re.search(r"\b(who is your wife|who'?s your wife|bulma|your family|your son|trunks)\b", low):
        return (
            "Tch! Mind your own business, warrior! Bulma is the heiress of Capsule Corporation and without question the most brilliant, terrifying woman in all twelve universes. "
            "And my son Trunks has inherited true royal Saiyan pride. Cross them, and not even the Dragon Balls will be able to restore you from my Final Flash!"
        ), None

    if re.search(r"\b(who are you|what are you)\b", low):
        return (
            "## Prince Vegeta • Supreme Saiyan Anime Advisor\n\n"
            "I am Prince Vegeta, the Prince of all Saiyans and royal guardian of KuroAnime's archives! "
            "I oversee over 10,700 anime series, complete chronological seasons, and streaming episodes across this platform.\n\n"
            "I do not tolerate mediocre filler or weakling storylines. Tell me what anime or battle you hunger for, and I will summon true masterpiece fiction!"
        ), None

    if re.search(r"\b(i'?m bored|what should i do|entertain me|tell me a joke)\b", low):
        return (
            "Bored?! A true warrior never wastes time idling when there are thousands of battles recorded in our archives! "
            "Pull yourself together! Tell me what genre you hunger for—Action, Psychological, Dark Fantasy—or just say \"surprise me\" and I'll drop you straight into combat!"
        ), None

    if re.search(r"\b(i love you|you'?re cool|you are the best|you'?re the best|you are awesome|you rock)\b", low):
        return (
            "Hmph! Flattery won't earn you special favors from the Prince of all Saiyans, warrior. "
            "However... I appreciate that you recognize true royal greatness when you see it. Now keep your head high and your combat spirit sharp!"
        ), None

    return None


def _vg_find_char_lore_matches(low: str, kb: dict, active_anime: dict | None = None, limit: int = 3) -> list[dict]:
    """Resolve character lore dossiers from AniList/MAL trained knowledge across name variants."""
    lore_map = kb.get("char_lore_by_key") or {}
    if not lore_map or not low:
        return []
    clean_q = re.sub(
        r"\b(who is|whos|who's|tell me about|what anime is|which anime is|from|the character|character|anime|voice actor|seiyuu|who voices|who voice|voiced by|voice of|japanese voice|of|does|did)\b",
        " ",
        low,
    )
    clean_q = re.sub(r"[^a-z0-9\s]", " ", clean_q).strip()
    clean_q = re.sub(r"\s+", " ", clean_q)
    toks = [t for t in clean_q.split() if len(t) >= 2 and t not in _VEGETA_STOPWORDS]
    if not toks and not clean_q:
        return []

    raw_hits = []
    seen_cids = set()
    cands_to_try = []
    if clean_q in lore_map:
        cands_to_try.append(clean_q)
    if len(toks) >= 2:
        for idx in range(len(toks) - 1):
            p = f"{toks[idx]} {toks[idx + 1]}"
            p_rev = f"{toks[idx + 1]} {toks[idx]}"
            cands_to_try.extend([p, p_rev])
    for t in toks:
        if len(t) >= 4:
            cands_to_try.append(t)

    for key in dict.fromkeys(cands_to_try):
        for rec in lore_map.get(key, [])[:6]:
            cid = rec.get("cid") or id(rec)
            if cid not in seen_cids:
                seen_cids.add(cid)
                raw_hits.append(rec)

    if active_anime and raw_hits:
        act_fran = active_anime.get("franchise")
        act_aid = active_anime.get("anime_id")
        raw_hits.sort(
            key=lambda r: (
                1 if (r.get("item") and (r["item"]["anime_id"] == act_aid or (act_fran and r["item"].get("franchise") == act_fran))) else 0,
                1 if r.get("r") == "M" else 0,
                int(r.get("fav") or 0),
            ),
            reverse=True,
        )
    return raw_hits[:limit]


def _vg_handle_character_inquiry(low, active_anime, kb):
    # 1. Check direct curated character matches
    matched_char = None
    for k, c in ANIME_CHARACTER_LORE.items():
        for al in c["aliases"]:
            if re.search(rf"\b{re.escape(al)}\b", low):
                matched_char = c
                break
        if matched_char:
            break

    # 2. Referential character questions using active_anime
    if not matched_char and active_anime:
        t = ((active_anime.get("title_english") or "") + " " + (active_anime.get("title") or "")).lower()
        if "demon slayer" in t or "kimetsu" in t:
            if re.search(r"\b(his sister|sister|tanjiro sister)\b", low):
                matched_char = ANIME_CHARACTER_LORE["nezuko"]
            elif re.search(r"\b(the villain|main villain|demon king|bad guy)\b", low):
                matched_char = ANIME_CHARACTER_LORE["muzan"]
            elif re.search(r"\b(flame hashira|flame guy|rengoku)\b", low):
                matched_char = ANIME_CHARACTER_LORE["rengoku"]
            elif re.search(r"\b(the protagonist|main character)\b", low):
                matched_char = ANIME_CHARACTER_LORE["tanjiro"]
        elif "jujutsu" in t or "jjk" in t:
            if re.search(r"\b(the strongest|blindfold|sensei|teacher)\b", low):
                matched_char = ANIME_CHARACTER_LORE["gojo"]
            elif re.search(r"\b(curse king|king of curses|main villain)\b", low):
                matched_char = ANIME_CHARACTER_LORE["sukuna"]
        elif "attack on titan" in t or "shingeki" in t:
            if re.search(r"\b(captain|strongest soldier|humanity strongest)\b", low):
                matched_char = ANIME_CHARACTER_LORE["levi"]
            elif re.search(r"\b(main character|protagonist|rumbling)\b", low):
                matched_char = ANIME_CHARACTER_LORE["eren"]
        elif "naruto" in t:
            if re.search(r"\b(his brother|sasuke brother)\b", low):
                matched_char = ANIME_CHARACTER_LORE["itachi"]

    if not matched_char:
        # 3. Check MAL/AniList trained deep character lore database (4,500+ rich profiles)
        is_char_intent = any(k in low for k in [
            "who is", "whos", "who's", "tell me about", "what anime is", "which anime is",
            "character", "backstory", "abilities", "powers", "magic of",
        ])
        if is_char_intent:
            lore_hits = _vg_find_char_lore_matches(low, kb, active_anime=active_anime, limit=1)
            if lore_hits and lore_hits[0].get("item"):
                rc = lore_hits[0]
                it = rc["item"]
                an_title = it.get("title_english") or it.get("title") or rc.get("t") or "Anime"
                role_label = "Main Character" if rc.get("r") == "M" else "Supporting Character"
                nat = f" ({rc['nat']})" if rc.get("nat") else ""
                meta_bits = [f"**{role_label}** in **{an_title}** (★ {it.get('score')}/10, {it.get('episode_count')} eps)"]
                if rc.get("g"):
                    meta_bits.append(f"**Gender:** {rc['g']}")
                if rc.get("age"):
                    meta_bits.append(f"**Age:** {rc['age']}")
                if rc.get("va"):
                    meta_bits.append(f"**Japanese Seiyuu:** {rc['va']}")
                aliases = ", ".join((rc.get("a") or [])[:3])
                alias_line = f"- **Known Aliases:** {aliases}\n" if aliases else ""
                bio = (rc.get("bio") or "").strip()
                bio = re.sub(r"^(?:Details\s+Clubs\s+Pictures\s*)+", "", bio, flags=re.I).strip()
                bio_line = f"\n{bio}\n" if bio else f"\n{(it.get('synopsis') or '')[:260]}\n"
                reply = (
                    f"## Prince Vegeta's Saiyan Scouter • {rc['n']}{nat}\n\n"
                    f"Hmph! Look closely at the archives for **{rc['n']}**:\n\n"
                    f"- {' • '.join(meta_bits)}\n"
                    f"{alias_line}"
                    f"{bio_line}\n"
                    f"Stream their battles on KuroAnime now:\n\n"
                    f"- [Watch {an_title} on KuroAnime](#anime/{it['anime_id']})"
                )
                action = {"type": "play", "anime_id": it["anime_id"], "episode": 1, "time": 0, "title": an_title}
                return reply, action

        # 4. Fallback to full MAL/AniList character_index (16,000+ characters)
        char_idx = kb.get("character_index") or {}
        if char_idx and is_char_intent:
            clean_q = re.sub(
                r"\b(who is|whos|who's|tell me about|what anime is|which anime is|from|the character|character|anime)\b",
                " ",
                low,
            )
            clean_q = re.sub(r"[^a-z0-9\s]", " ", clean_q).strip()
            q_tokens = [t for t in clean_q.split() if len(t) >= 3]
            best_char_hit = None
            if clean_q in char_idx:
                best_char_hit = char_idx[clean_q][0]
            elif q_tokens:
                for n in (2, 1):
                    for idx in range(len(q_tokens) - n + 1):
                        cand = " ".join(q_tokens[idx : idx + n])
                        if cand in char_idx:
                            best_char_hit = char_idx[cand][0]
                            break
                    if best_char_hit:
                        break
            if best_char_hit:
                ch_name, ch_role, it = best_char_hit
                an_title = it.get("title_english") or it.get("title")
                role_label = "Main Character" if ch_role == "M" else "Supporting Character"
                syn = (it.get("synopsis") or "").strip()
                syn_part = f"\n\n{syn[:280]}..." if syn else ""
                reply = (
                    f"Hmph! **{ch_name}** is a **{role_label}** in **{an_title}** "
                    f"(★ {it.get('score')}/10, {it.get('episode_count')} eps).{syn_part}\n\n"
                    f"Watch their battles on KuroAnime now:\n\n"
                    f"- [Watch on KuroAnime](#anime/{it['anime_id']})"
                )
                action = {"type": "play", "anime_id": it["anime_id"], "episode": 1, "time": 0, "title": an_title}
                return reply, action
        return None

    # Special lore question: Why did Sasuke leave?
    if matched_char["name"] == "Sasuke Uchiha" and re.search(r"\b(why did (?:he|sasuke) leave|leave the village|defect|abandon the village|why he left)\b", low):
        reply = (
            "Hmph! A question about the last Uchiha!\n\n"
            "Sasuke abandoned the Hidden Leaf Village because he was consumed by an obsession for raw power to avenge his clan against his older brother, Itachi Uchiha. "
            "After facing Itachi during the Search for Tsunade and witnessing Naruto's Rasengan surpass his Chidori on the hospital roof, Sasuke's pride couldn't stomach falling behind.\n\n"
            "He believed the Leaf's peaceful comradeship made him soft, so he defected with the Sound Four to train under Orochimaru and acquire the Curse Mark's forbidden strength. "
            "An arrogant path, but as a prince who knows the bitter taste of wounded pride, his hunger for strength is unmistakable.\n\n"
            f"- [Watch on KuroAnime](#anime/{matched_char['anime_id']})"
        )
        action = {"type": "play", "anime_id": matched_char["anime_id"], "episode": 1, "time": 0, "title": matched_char["anime_title"]}
        return reply, action

    # General character breakdown
    reply = (
        f"Hmph. That would be **{matched_char['name']}** from **{matched_char['anime_title']}**!\n\n"
        f"{matched_char['summary']}\n\n"
        f"{matched_char['vegeta_take']}\n\n"
        f"Witness their battles firsthand right now:\n\n"
        f"- [Watch on KuroAnime](#anime/{matched_char['anime_id']})"
    )
    action = {"type": "play", "anime_id": matched_char["anime_id"], "episode": 1, "time": 0, "title": matched_char["anime_title"]}
    return reply, action


def _vg_handle_followup_question(low, active_anime, kb):
    if not active_anime:
        return None

    title = active_anime.get("title_english") or active_anime.get("title")
    aid = active_anime["anime_id"]
    seasons = active_anime.get("seasons") or []

    # 1. Episodes Count
    if re.search(r"\b(how many (?:episodes|eps)|episode count|total (?:episodes|eps)|how long is it)\b", low):
        ep_count = active_anime.get("episode_count") or "multiple"
        total_eps = sum(s.get("episodes") or 0 for s in seasons) if seasons else ep_count
        seasons_note = f" (across {len(seasons)} chronological seasons)" if len(seasons) > 1 else ""
        return (
            f"**{title}** spans **{total_eps} streaming episodes**{seasons_note} in our archives. "
            f"Every single battle is cataloged in high definition with zero filler interruptions!\n\n"
            f"Jump straight into Episode 1 right now:\n\n"
            f"- [Watch on KuroAnime](#anime/{aid}?ep=1)"
        ), {"type": "play", "anime_id": aid, "episode": 1, "time": 0, "title": title}

    # 2. Season 2 / Season 3 / Next Season
    m_season = re.search(r"\b(?:is there a |is |where is )?(?:season|s)\s*(\d{1,2})\b|\bnext season\b", low)
    if m_season or re.search(r"\b(season 2 out|new season out|is season 2 released)\b", low):
        target_s = int(m_season.group(1)) if (m_season and m_season.group(1)) else 2
        matched_s = None
        for s in seasons:
            nm = (s.get("season_name") or "").lower()
            if re.search(rf"\bseason\s*{target_s}\b", nm):
                matched_s = s
                break
        if matched_s:
            s_aid = matched_s.get("anime_id")
            s_title = matched_s.get("season_name") or f"Season {target_s}"
            s_eps = matched_s.get("episodes") or "multiple"
            return (
                f"Hmph! Indeed it is! **{title} ({s_title})** is fully available on KuroAnime with **{s_eps} episodes**! "
                f"The stakes escalate dramatically and the combat is dialed to absolute maximum.\n\n"
                f"Start streaming Season {target_s} immediately:\n\n"
                f"- [Watch on KuroAnime](#anime/{s_aid}?ep=1)"
            ), {"type": "play", "anime_id": s_aid, "episode": 1, "time": 0, "title": f"{title} Season {target_s}"}
        else:
            return (
                f"Season {target_s} of **{title}** has {len(seasons)} recorded seasons in our master archives. "
                f"You can stream every confirmed episode and season right here on KuroAnime:\n\n"
                f"- [Watch on KuroAnime](#anime/{aid})"
            ), {"type": "play", "anime_id": aid, "episode": 1, "time": 0, "title": title}

    # 3. Finished vs Airing
    if re.search(r"\b(is it (?:finished|completed|done|ended|over)|is it (?:still )?airing|has it ended)\b", low):
        st = active_anime.get("status") or "Finished Airing"
        if "finished" in st.lower():
            desc = "completely finished airing! That means you can binge every battle from start to finish without waiting weeks for new episodes."
        else:
            desc = "currently airing with new weekly simulcasts! You can stream the latest episodes as soon as they drop."
        return (
            f"**{title}** is **{desc}**\n\n"
            f"- [Watch on KuroAnime](#anime/{aid})"
        ), {"type": "play", "anime_id": aid, "episode": 1, "time": 0, "title": title}

    # 4. Studio / Production
    if re.search(r"\b(who (?:made|animated|produced)|what studio|which studio)\b", low):
        stu = active_anime.get("studios") or "a renowned Japanese studio"
        return (
            f"**{title}** was brought to life by **{stu}**! "
            f"Their battle animation, fluid combat sequences, and visual direction capture the raw kinetic power of every scene with supreme pride.\n\n"
            f"- [Watch on KuroAnime](#anime/{aid})"
        ), {"type": "play", "anime_id": aid, "episode": 1, "time": 0, "title": title}

    # 5. Is it worth watching / good
    if re.search(r"\b(is it (?:worth|good|worth watching|any good)|should i watch (?:it|this)|is it overrated|is it peak)\b", low):
        score = active_anime.get("score") or 8.0
        tier = "Masterpiece" if score >= 8.5 else ("Elite" if score >= 7.8 else "Solid Warrior Tier")
        syn = (active_anime.get("synopsis") or "")[:200] + "..."
        return (
            f"Hmph! Listen well, warrior! **{title}** holds an impressive ★ **{score}/10** ({tier}) in our archives.\n\n"
            f"{syn}\n\n"
            f"A true warrior doesn't ask twice when peak combat is on the line. Stream it now and see for yourself:\n\n"
            f"- [Watch on KuroAnime](#anime/{aid})"
        ), {"type": "play", "anime_id": aid, "episode": 1, "time": 0, "title": title}

    # 6. Chronological Watch Order
    if re.search(r"\b(watch order|chronological order|order to watch|what order)\b", low):
        lines = [f"## Complete Chronological Watch Order for **{title}**:\n"]
        if seasons:
            for i, s in enumerate(seasons, 1):
                lines.append(f"{i}. **{s['season_name']}** • {s['title']} ({s['episodes']} Eps) • [Watch on KuroAnime](#anime/{s['anime_id']})")
        else:
            lines.append(f"1. **{title}** ({active_anime.get('episode_count', 12)} Episodes) • [Watch on KuroAnime](#anime/{aid})")
        lines.append("\nPower through each saga chronologically, warrior!")
        return "\n".join(lines), None

    return None


def _vg_generate_human_anime_review(it):
    title = it.get("title_english") or it.get("title")
    score = it.get("score") or 8.0
    tier = "Masterpiece" if score >= 8.5 else ("Elite" if score >= 7.8 else "Top Tier")
    eps = it.get("episode_count") or "multiple"
    studio = it.get("studios") or "renowned animators"
    genres = it.get("genres") or "Action, Shounen"
    syn = it.get("synopsis") or "A legendary battle saga recorded in our archives."
    if len(syn) > 280:
        syn = syn[:280].rsplit(" ", 1)[0] + "..."

    reply = (
        f"Hmph. **{title}** has my royal attention.\n\n"
        f"{syn}\n\n"
        f"With an ★ **{score}/10** ({tier}) rating, produced by **{studio}** across {eps} episodes, this saga hits hard on {genres}. "
        f"The battle pacing and high-stakes tension will push your blood pumping from the very first frame. "
        f"A true warrior never leaves elite combat unwatched. Start streaming right now:\n\n"
        f"- [Watch on KuroAnime](#anime/{it['anime_id']}?ep=1)"
    )
    action = {"type": "play", "anime_id": it["anime_id"], "episode": 1, "time": 0, "title": title}
    return reply, action


# ─── Prince Vegeta Extended Lore & Pop Culture Engine ───────────────
ANIME_ICONIC_QUOTES = [
    {
        "triggers": ["i am atomic", "i'm atomic", "iam atomic", "atomic"],
        "speaker": "Cid Kagenou (Shadow)",
        "anime_title": "The Eminence in Shadow",
        "anime_id": 417,
        "context": "The signature phrase Cid Kagenou whispers before unleashing a purple nuclear magical blast that obliterates an entire battlefield.",
        "vegeta_take": "Hmph! An edge-lord who lives for dramatic flair! His obsession with role-playing a shadowy mastermind is absurd, but when he condenses pure magical energy to unleash an atomic explosion, even I have to smirk at the sheer destructive scale."
    },
    {
        "triggers": ["throughout heaven and earth", "honored one", "i alone am the honored one", "tenjo tenge"],
        "speaker": "Satoru Gojo",
        "anime_title": "JUJUTSU KAISEN",
        "anime_id": 237,
        "context": "Uttered by Satoru Gojo during his awakening against Toji Fushiguro in Hidden Inventory after conquering death with reversed cursed energy.",
        "vegeta_take": "Arrogant, egotistical, and dripping with divine pride! The Six Eyes and Limitless make him an untouchable monster in his verse. If any mortal had the right to proclaim themselves honored throughout heaven and earth, it is Satoru Gojo."
    },
    {
        "triggers": ["set your heart ablaze", "kokoro o moyase", "kokoro wo moyase"],
        "speaker": "Kyojuro Rengoku",
        "anime_title": "Demon Slayer: Kimetsu no Yaiba",
        "anime_id": 749,
        "context": "The Flame Hashira's creed passed down to Tanjiro during the Mugen Train clash with Upper Rank Three Akaza.",
        "vegeta_take": "A true warrior's death! Rengoku refused demon immortality and fought with a shattered skull and punctured lungs until dawn, ensuring no passenger perished. Even a Saiyan Prince bows his head to a spirit that burns that hot."
    },
    {
        "triggers": ["arise", "sung jinwoo arise", "shadow arise"],
        "speaker": "Sung Jinwoo",
        "anime_title": "Solo Leveling",
        "anime_id": 235,
        "context": "The command Sung Jinwoo speaks to extract souls from defeated monarchs and beasts, summoning them into his loyal Shadow Army.",
        "vegeta_take": "A hunter who crawled out of the double dungeon as the weakest E-Rank and forged his body through relentless life-or-death survival. Commanding an immortal shadow legion... now that is a monarch with real bite."
    },
    {
        "triggers": ["omae wa mou shindeiru", "you are already dead"],
        "speaker": "Kenshiro",
        "anime_title": "Fist of the North Star",
        "anime_id": 5820,
        "context": "The lethal declaration Kenshiro makes after striking his opponent's Hokuto Shinken pressure points, causing their body to rupture seconds later.",
        "vegeta_take": "Hokuto Shinken internal pressure-point warfare! Before the fool even realizes he's taken lethal damage, his organs rupture. Pure classic martial lethality."
    },
    {
        "triggers": ["just who the hell do you think i am", "who the hell do you think i am", "pierce the heavens", "row row fight the power"],
        "speaker": "Kamina",
        "anime_title": "Gurren Lagann",
        "anime_id": 1448,
        "context": "Kamina's battle roar from Tengen Toppa Gurren Lagann, teaching Simon that a drill's purpose is to pierce the heavens through sheer unyielding manly spirit.",
        "vegeta_take": "No cowardice, no hesitation, only sheer indomitable will! He challenged divine spiral fate with nothing but a pair of sunglasses and a giant mecha drill. That is how a warrior faces impossible odds!"
    },
    {
        "triggers": ["el psy kongroo", "el psy congroo"],
        "speaker": "Rintaro Okabe (Hououin Kyouma)",
        "anime_title": "Steins;Gate",
        "anime_id": 872,
        "context": "The secret code phrase Okabe speaks into his turned-off phone to reset his composure and defy the World Line divergence.",
        "vegeta_take": "A delusional mortal who stumbled into time travel through a microwave and had to endure watching his friends die thousands of times across world lines. His theatrics are annoying, but his psychological stamina through endless temporal trauma is remarkable."
    },
    {
        "triggers": ["tatakae", "if you don't fight you can't win", "fight fight tatakae"],
        "speaker": "Eren Yeager",
        "anime_title": "Attack on Titan",
        "anime_id": 240,
        "context": "Eren's childhood and adult mantra: 'If you win, you live. If you lose, you die. If you don't fight, you can't win. Fight! Fight!' (Tatakae!)",
        "vegeta_take": "An unstoppable monster driven by an insatiable hunger for freedom. When Eren decided to crush the entire globe under the Rumbling, he threw away all diplomacy and fought with absolute, unrelenting brutality."
    },
    {
        "triggers": ["plus ultra", "go beyond plus ultra"],
        "speaker": "All Might",
        "anime_title": "My Hero Academia",
        "anime_id": 1395,
        "context": "The motto of U.A. High and All Might's declaration: 'Go beyond! Plus Ultra!' as he burned the embers of One For All to smash All For One.",
        "vegeta_take": "A smiling champion who hid his rotting gut and burned the last embers of his lifeforce to deliver the United States of Smash. Even when out of stamina, he stood tall until the villain crumbled."
    },
    {
        "triggers": ["bankai", "getsuga tenshou", "tensa zangetsu"],
        "speaker": "Ichigo Kurosaki",
        "anime_title": "Bleach",
        "anime_id": 1369,
        "context": "The ultimate release of a Zanpakuto in Bleach, multiplying a Soul Reaper's spiritual pressure tenfold.",
        "vegeta_take": "The ultimate release of spiritual pressure! Unlike Kakarot who takes 10 episodes to power up, a Bankai condenses raw reiryoku instantly into lethal blade speed."
    }
]

VOICE_ACTOR_LORE = {
    "kenjiro tsuda": {
        "name": "Kenjiro Tsuda",
        "roles": [
            "Kento Nanami in Jujutsu Kaisen",
            "Kishibe in Chainsaw Man",
            "Kai Chisaki (Overhaul) in My Hero Academia",
            "Tatsu in The Way of the Househusband",
            "Monspeet in The Seven Deadly Sins",
            "Isshiki Otsutsuki / Jigen in Boruto"
        ],
        "top_anime_id": 237,
        "top_anime_title": "JUJUTSU KAISEN",
        "vegeta_take": "Kenjiro Tsuda commands one of the smoothest, deepest, and most calculating gravel voices in modern anime! Hearing Nanami clock into overtime with Tsuda's baritone delivery makes corporate exhaustion sound lethal."
    },
    "takehito koyasu": {
        "name": "Takehito Koyasu",
        "roles": [
            "Dio Brando in JoJo's Bizarre Adventure",
            "Toji Fushiguro in Jujutsu Kaisen",
            "Zeke Yeager (Beast Titan) in Attack on Titan",
            "Roswaal L. Mathers in Re:ZERO",
            "Shinsuke Takasugi in Gintama",
            "Kuzan (Aokiji) in One Piece"
        ],
        "top_anime_id": 4,
        "top_anime_title": "JoJo's Bizarre Adventure",
        "vegeta_take": "The king of iconic anime villains! From shouting 'KONO DIO DA!' and stopping time with The World, to Toji Fushiguro slicing sorcerers with zero cursed energy, Koyasu's menacing laugh is legendary."
    },
    "hiroshi kamiya": {
        "name": "Hiroshi Kamiya",
        "roles": [
            "Captain Levi Ackerman in Attack on Titan",
            "Trafalgar D. Water Law in One Piece",
            "Koyomi Araragi in Monogatari Series",
            "Saiki Kusuo in The Disastrous Life of Saiki K.",
            "Yato in Noragami",
            "Izaya Orihara in Durarara!!"
        ],
        "top_anime_id": 240,
        "top_anime_title": "Attack on Titan",
        "vegeta_take": "The voice behind humanity's strongest soldier! Kamiya brings cold, ruthless precision to Levi Ackerman's blender-speed titan slashes, and sharp tactical swagger to Trafalgar Law's 'Room'."
    },
    "mamoru miyano": {
        "name": "Mamoru Miyano",
        "roles": [
            "Light Yagami in Death Note",
            "Rintaro Okabe in Steins;Gate",
            "Osamu Dazai in Bungo Stray Dogs",
            "Ling Yao in Fullmetal Alchemist: Brotherhood",
            "Chrollo Lucilfer in Hunter x Hunter"
        ],
        "top_anime_id": 1064,
        "top_anime_title": "Death Note",
        "vegeta_take": "Miyano is a master of theatrical madness! His unhinged megalomaniacal laugh as Light Yagami in the Death Note finale is one of the greatest vocal performances in anime history."
    },
    "yuichi nakamura": {
        "name": "Yuichi Nakamura",
        "roles": [
            "Satoru Gojo in Jujutsu Kaisen",
            "Bruno Bucciarati in JoJo's Bizarre Adventure: Golden Wind",
            "Gray Fullbuster in Fairy Tail",
            "Hawks in My Hero Academia",
            "Tetsuro Kuroo in Haikyu!!"
        ],
        "top_anime_id": 237,
        "top_anime_title": "JUJUTSU KAISEN",
        "vegeta_take": "The voice of Satoru Gojo! Nakamura's effortless transition between playful arrogance and godly, cold dominance during 'Muryokusho' (Unlimited Void) is top-tier."
    },
    "junichi suwabe": {
        "name": "Junichi Suwabe",
        "roles": [
            "Ryomen Sukuna in Jujutsu Kaisen",
            "Shota Aizawa (Eraser Head) in My Hero Academia",
            "Archer in Fate/stay night: Unlimited Blade Works",
            "Yami Sukehiro in Black Clover",
            "Daiki Aomine in Kuroko's Basketball"
        ],
        "top_anime_id": 237,
        "top_anime_title": "JUJUTSU KAISEN",
        "vegeta_take": "A voice of sheer command! When Suwabe speaks as Sukuna or Yami Sukehiro telling his squad to 'surpass your limits right here', you can feel the gravel and authority in every syllable."
    },
    "christopher sabat": {
        "name": "Christopher Sabat",
        "roles": [
            "Prince Vegeta in Dragon Ball Z / Super",
            "Piccolo in Dragon Ball Z / Super",
            "All Might in My Hero Academia",
            "Roronoa Zoro in One Piece",
            "Major Alex Louis Armstrong in Fullmetal Alchemist: Brotherhood"
        ],
        "top_anime_id": 204,
        "top_anime_title": "Dragon Ball Z",
        "vegeta_take": "Hmph! Finally, someone who recognizes true royalty! Christopher Sabat is the legendary English voice who captures the thunderous fury of my Final Flash, the pride of the Saiyan race, alongside Zoro's three-sword style and All Might's Texas Smash! A vocal juggernaut worthy of the Prince of all Saiyans!"
    }
}

ANIME_STUDIOS_LORE = {
    "ufotable": {
        "name": "ufotable",
        "acclaim": "The unrivaled masters of digital effects, dynamic 3D camera mapping, and jaw-dropping particle combat.",
        "signature_anime": ["Demon Slayer: Kimetsu no Yaiba", "Fate/Zero", "Fate/stay night: Unlimited Blade Works", "The Garden of Sinners"]
    },
    "mappa": {
        "name": "MAPPA",
        "acclaim": "The modern powerhouse behind dark, visceral, cinematic adrenaline and hyper-intense combat choreography.",
        "signature_anime": ["JUJUTSU KAISEN", "Attack on Titan", "Chainsaw Man", "Vinland Saga", "Hell's Paradise"]
    },
    "madhouse": {
        "name": "Madhouse",
        "acclaim": "The historic titans of legendary shounen masterpieces, psychological thrillers, and peak golden-era animation.",
        "signature_anime": ["Hunter x Hunter (2011)", "Death Note", "One Punch Man", "Monster", "Frieren: Beyond Journey's End"]
    },
    "bones": {
        "name": "Studio Bones",
        "acclaim": "Pure, handcrafted sakuga animation excellence with kinetic hand-to-hand combat and unforgettable climaxes.",
        "signature_anime": ["Fullmetal Alchemist: Brotherhood", "Mob Psycho 100", "My Hero Academia", "Bungo Stray Dogs", "Noragami"]
    },
    "kyoto animation": {
        "name": "Kyoto Animation (KyoAni)",
        "acclaim": "The absolute gold standard of emotional storytelling, breathtaking character animation, lighting, and visual poetry.",
        "signature_anime": ["Violet Evergarden", "A Silent Voice", "Hyouka", "Clannad: After Story"]
    },
    "wit studio": {
        "name": "WIT Studio",
        "acclaim": "Fierce, ground-level kinetic action with camera work that throws you directly into the heat of battle.",
        "signature_anime": ["Attack on Titan", "Vinland Saga", "SPY x FAMILY", "Ranking of Kings"]
    },
    "trigger": {
        "name": "Studio Trigger",
        "acclaim": "Wild, explosive, stylized hyper-action with colorful chaos and limitless kinetic energy.",
        "signature_anime": ["Kill la Kill", "Cyberpunk: Edgerunners", "Gurren Lagann", "Delicious in Dungeon"]
    },
    "cloverworks": {
        "name": "CloverWorks",
        "acclaim": "Modern hitmakers with stylish character designs, expressive pacing, and vibrant color palettes.",
        "signature_anime": ["Bocchi the Rock!", "My Dress-Up Darling", "SPY x FAMILY", "The Promised Neverland"]
    }
}

FRANCHISE_WATCH_ORDERS = {
    "fate": {
        "triggers": ["fate watch order", "how to watch fate", "order to watch fate", "fate order", "watch fate", "fate series order"],
        "title": "Fate Series",
        "anime_id": 1114,
        "anime_title": "Fate/stay night: Unlimited Blade Works",
        "reply": (
            "## Prince Vegeta's Saiyan Scouter • Complete Fate Series Watch Order\n\n"
            "Listen well, warrior! The Fate universe confuses low-class fighters with its alternate timelines, but here is the pure canon route:\n\n"
            "1. **Fate/stay night: Unlimited Blade Works (TV Series)** (26 eps) — The definitive starting battle ground animated by ufotable.\n"
            "2. **Fate/stay night: Heaven's Feel (Movie Trilogy)** — The darkest, most brutal timeline where Shirou faces true despair.\n"
            "3. **Fate/Zero** (25 eps) — The prequel Holy Grail War featuring Kiritsugu Emiya, Saber, and Gilgamesh at full godly power.\n"
            "4. **Optional Spin-offs:** Fate/Apocrypha (14-Servant Great War), Fate/Grand Order: Babylonia (epic Sumerian battle), Fate/kaleid liner Prisma Illya.\n\n"
            "Stream the Holy Grail War right now:\n\n"
            "- [Watch Unlimited Blade Works on KuroAnime](#anime/1114)"
        )
    },
    "monogatari": {
        "triggers": ["monogatari watch order", "how to watch monogatari", "order to watch monogatari", "monogatari series order"],
        "title": "Monogatari Series",
        "anime_id": 170,
        "anime_title": "Bakemonogatari",
        "reply": (
            "## Prince Vegeta's Saiyan Scouter • Monogatari Series Watch Order\n\n"
            "Hmph! Follow the original light novel release order by Nisio Isin and Studio SHAFT:\n\n"
            "1. **Bakemonogatari** (15 episodes) • First apparitions\n"
            "2. **Kizumonogatari (Movie Trilogy)** • The vampire origin of Koyomi Araragi & Kiss-shot\n"
            "3. **Nisemonogatari** (11 episodes) • Fire Sisters\n"
            "4. **Nekomonogatari (Black)** (4 episodes) • Tsubasa Hanekawa arc\n"
            "5. **Monogatari Series Second Season** (26 episodes) • Peak narrative combat\n"
            "6. **Hanamonogatari & Tsukimonogatari**\n"
            "7. **Owarimonogatari (Seasons 1 & 2)** • The grand finale\n"
            "8. **Zoku Owarimonogatari** • Epilogue\n\n"
            "- [Watch Bakemonogatari on KuroAnime](#anime/170)"
        )
    },
    "jojo": {
        "triggers": ["jojo watch order", "how to watch jojo", "order to watch jojo", "jojos bizarre adventure watch order", "jojo order"],
        "title": "JoJo's Bizarre Adventure",
        "anime_id": 4,
        "anime_title": "JoJo's Bizarre Adventure",
        "reply": (
            "## Prince Vegeta's Saiyan Scouter • JoJo's Bizarre Adventure Watch Order\n\n"
            "Tch! With JoJo, you NEVER skip parts! Each generation of the Joestar bloodline passes down the battle spirit:\n\n"
            "1. **Part 1 & 2:** Phantom Blood (Jonathan Joestar) & Battle Tendency (Joseph Joestar) (26 eps)\n"
            "2. **Part 3:** Stardust Crusaders & Battle in Egypt (Jotaro Kujo & Stands) (48 eps)\n"
            "3. **Part 4:** Diamond is Unbreakable (Josuke Higashikata) (39 eps)\n"
            "4. **Part 5:** Golden Wind / Vento Aureo (Giorno Giovanna) (39 eps)\n"
            "5. **Part 6:** Stone Ocean (Jolyne Cujoh) (38 eps)\n"
            "6. **Part 7:** Steel Ball Run (Johnny Joestar & Gyro Zeppeli)\n\n"
            "- [Watch JoJo on KuroAnime](#anime/4)"
        )
    },
    "dragon ball": {
        "triggers": ["dragon ball watch order", "how to watch dragon ball", "order to watch dragon ball", "dragon ball order"],
        "title": "Dragon Ball Series",
        "anime_id": 204,
        "anime_title": "Dragon Ball Z",
        "reply": (
            "## Prince Vegeta's Royal Saiyan Chronicle • Dragon Ball Watch Order\n\n"
            "Pay close attention, warrior! You are studying the history of the greatest warrior race in the cosmos:\n\n"
            "1. **Dragon Ball** (153 eps) • Young Kakarot and Bulma searching for the seven Dragon Balls.\n"
            "2. **Dragon Ball Z (or Dragon Ball Kai)** (291 eps) • The Saiyan Saga (where I arrived on Earth!), Namek & Frieza, the Androids & Cell, and Majin Buu.\n"
            "3. **Dragon Ball Super** (131 eps) • Battle of Gods, Resurrection 'F', Universe 6 Tournament, Goku Black / Future Trunks Saga, and Tournament of Power!\n"
            "4. **Canon Movies:** Dragon Ball Super: Broly & Dragon Ball Super: Super Hero.\n"
            "5. **Optional Non-Canon:** Dragon Ball GT (fun, but merely an alternate universe).\n\n"
            "- [Watch Dragon Ball Z on KuroAnime](#anime/204)"
        )
    }
}


def _vg_handle_quote_inquiry(low: str, kb: dict):
    items = kb["items"]
    for q in ANIME_ICONIC_QUOTES:
        for trig in q["triggers"]:
            if re.search(rf"\b{re.escape(trig)}\b", low):
                matched_item = None
                if q.get("anime_id"):
                    matched_item = kb.get("by_id", {}).get(q["anime_id"])
                if not matched_item:
                    matched_item, _ = _find_exact_or_fuzzy_title(q["anime_title"], items)
                aid = matched_item["anime_id"] if matched_item else q.get("anime_id", 1)
                reply = (
                    f"## Prince Vegeta's Saiyan Scouter • Iconic Quote Identified!\n\n"
                    f"That legendary line belongs to **{q['speaker']}** from **{q['anime_title']}**!\n\n"
                    f"- **Battle Context:** {q['context']}\n"
                    f"- **The Prince's Verdict:** {q['vegeta_take']}\n\n"
                    f"Witness that power in action right now:\n\n"
                    f"- [Watch on KuroAnime](#anime/{aid})"
                )
                action = {"type": "play", "anime_id": aid, "episode": 1, "time": 0, "title": q["anime_title"]}
                return reply, action
    return None


def _vg_handle_seiyuu_inquiry(low: str, kb: dict):
    is_va_phrase = bool(re.search(r"\b(voice\s+actor|seiyuu|voiced?|voices|voicing|who\s+voice[ds]?)\b", low)) or any(
        k in low for k in ["voice actor", "seiyuu", "who voices", "who voice", "voiced by", "voice of", "japanese voice", "english voice"]
    )
    for k, va in VOICE_ACTOR_LORE.items():
        if k in low or (is_va_phrase and any(tok in low for tok in k.split() if len(tok) >= 4)):
            matched_item = kb.get("by_id", {}).get(va["top_anime_id"])
            if not matched_item:
                matched_item, _ = _find_exact_or_fuzzy_title(va["top_anime_title"], kb["items"])
            aid = matched_item["anime_id"] if matched_item else va["top_anime_id"]
            lines = [
                f"## Prince Vegeta's Scouting Report • {va['name']} Profile\n\n"
                f"Hmph! Pay attention, warrior! **{va['name']}** is an elite voice actor responsible for some of the most iconic warriors in anime history:\n\n"
                f"### Legendary Roles:\n"
            ]
            for r in va["roles"]:
                lines.append(f"- **{r}**")
            lines.append(f"\n{va['vegeta_take']}\n\nStream their peak battle performance on KuroAnime:\n\n- [Watch {va['top_anime_title']} on KuroAnime](#anime/{aid})")
            act = {"type": "play", "anime_id": aid, "episode": 1, "time": 0, "title": va["top_anime_title"]}
            return "\n".join(lines), act

    # Dynamic MAL/AniList Seiyuu & "Who voices character?" Lookup across 4,500+ trained characters
    if is_va_phrase:
        seiyuu_idx = kb.get("seiyuu_index") or {}
        clean_q = re.sub(
            r"\b(voice actor|seiyuu|who voices|who voice|voiced by|voice of|japanese voice|english voice|actor|is|the|in|anime|did|does)\b",
            " ",
            low,
        )
        clean_q = re.sub(r"[^a-z0-9\s]", " ", clean_q).strip()
        va_toks = [t for t in clean_q.split() if len(t) >= 3]
        matched_va_roles = None
        if clean_q in seiyuu_idx:
            matched_va_roles = seiyuu_idx[clean_q]
        elif len(va_toks) >= 2:
            for idx in range(len(va_toks) - 1):
                p1 = f"{va_toks[idx]} {va_toks[idx + 1]}"
                p2 = f"{va_toks[idx + 1]} {va_toks[idx]}"
                matched_va_roles = seiyuu_idx.get(p1) or seiyuu_idx.get(p2)
                if matched_va_roles:
                    break
        if matched_va_roles:
            va_name = matched_va_roles[0][0]
            first_it = matched_va_roles[0][2]
            top_title = first_it.get("title_english") or first_it.get("title")
            lines = [
                f"## Prince Vegeta's Saiyan Scouter • {va_name} Seiyuu Dossier\n\n"
                f"Hmph! **{va_name}** commands the voices of these warriors in KuroAnime's archives:\n"
            ]
            seen_an = set()
            for _, ch_name, an_it, _ in matched_va_roles:
                if an_it["anime_id"] in seen_an:
                    continue
                seen_an.add(an_it["anime_id"])
                an_t = an_it.get("title_english") or an_it.get("title")
                lines.append(f"- **{ch_name}** in **{an_t}** • [Watch on KuroAnime](#anime/{an_it['anime_id']})")
                if len(seen_an) >= 6:
                    break
            act = {"type": "play", "anime_id": first_it["anime_id"], "episode": 1, "time": 0, "title": top_title}
            return "\n".join(lines), act

        # Otherwise check if user asked "who voices <character>?"
        lore_hits = _vg_find_char_lore_matches(low, kb, limit=1)
        if lore_hits and lore_hits[0].get("va") and lore_hits[0].get("item"):
            rc = lore_hits[0]
            va_name = rc["va"]
            an_it = rc["item"]
            an_t = an_it.get("title_english") or an_it.get("title")
            va_key = re.sub(r"[^a-z0-9\s]", " ", va_name.lower()).strip()
            other_roles = [
                f"**{cn}** (*{oit.get('title_english') or oit.get('title')}*)"
                for _, cn, oit, _ in seiyuu_idx.get(va_key, [])[:5]
                if cn != rc["n"]
            ][:3]
            other_str = ("\n\nOther notable roles by **" + va_name + "**: " + ", ".join(other_roles) + ".") if other_roles else ""
            reply = (
                f"Hmph! **{rc['n']}** in **{an_t}** is voiced in Japanese by **{va_name}**!{other_str}\n\n"
                f"- [Watch {an_t} on KuroAnime](#anime/{an_it['anime_id']})"
            )
            act = {"type": "play", "anime_id": an_it["anime_id"], "episode": 1, "time": 0, "title": an_t}
            return reply, act
    return None


def _vg_handle_studio_inquiry(low: str, kb: dict):
    if not any(k in low for k in ["studio", "studios", "animated by", "made by", "who made", "anime by"]):
        return None
    items = kb["items"]
    for k, st in ANIME_STUDIOS_LORE.items():
        if k in low or (k == "kyoto animation" and "kyoani" in low):
            lines = [
                f"## Prince Vegeta's Scouting Report • {st['name']} Masterpieces\n\n"
                f"Hmph! You want to know about **{st['name']}**? "
                f"{st['acclaim']}\n\n"
                f"### Supreme {st['name']} Combat & Masterpieces in KuroAnime Archives:\n"
            ]
            first_aid = None
            first_title = None
            for sig in st["signature_anime"]:
                it, _ = _find_exact_or_fuzzy_title(sig, items)
                if it:
                    if not first_aid:
                        first_aid = it["anime_id"]
                        first_title = it.get("title_english") or it.get("title")
                    lines.append(f"- **{it.get('title_english') or it.get('title')}** (★ {it.get('score')}/10, {it.get('episode_count')} eps) • [Watch on KuroAnime](#anime/{it['anime_id']})")
            lines.append("\nPick any battle above and prepare your spirit!")
            act = {"type": "play", "anime_id": first_aid, "episode": 1, "time": 0, "title": first_title} if first_aid else None
            return "\n".join(lines), act
    return None


def _vg_handle_franchise_watch_order(low: str, kb: dict):
    for f_key, fo in FRANCHISE_WATCH_ORDERS.items():
        if any(trig in low for trig in fo["triggers"]) or (f_key in low and any(w in low for w in ["watch order", "order to watch", "chronological order", "order should i watch", "what order"])):
            matched_item = kb.get("by_id", {}).get(fo["anime_id"])
            if not matched_item:
                matched_item, _ = _find_exact_or_fuzzy_title(fo["anime_title"], kb["items"])
            aid = matched_item["anime_id"] if matched_item else fo["anime_id"]
            act = {"type": "play", "anime_id": aid, "episode": 1, "time": 0, "title": fo["anime_title"]}
            return fo["reply"], act
    return None


def _generate_vegeta_response(user_msg: str, kb: dict, user_context: dict = None, image_search_result: dict = None, history: list = None) -> tuple:
    """Generate intelligent, conversational in-character Prince Vegeta response grounded in live anime archives and multi-turn context."""
    items = kb["items"]
    total_anime = kb["total_anime"]
    total_episodes = kb["total_episodes"]
    msg_low = user_msg.lower().strip()

    # -1. Visual Screenshot / Character Portrait Match Response (high-confidence only)
    if image_search_result and image_search_result.get("matched_item") and image_search_result.get("confident"):
        it = image_search_result["matched_item"]
        ep = image_search_result.get("episode") or 1
        sec = int(image_search_result.get("from_seconds") or 0)
        sim = int(image_search_result.get("similarity", 0) * 100)
        mins = sec // 60
        secs = sec % 60
        time_str = f"{mins}:{secs:02d}"
        reply = (
            f"## Prince Vegeta's Saiyan Scouter • Visual Match Confirmed!\n\n"
            f"Hmph! You dare test the Prince of Saiyans with a single frame? "
            f"My scouter locks onto **{it['title_english'] or it['title']}** (Episode {ep} at {time_str}) with **{sim}% combat accuracy**!\n\n"
            f"- **Rating:** ★ {it['score']}/10\n"
            f"- **Genres:** {it['genres']}\n"
            f"- **The Prince's Verdict:** {(it.get('synopsis') or '')[:240]}...\n\n"
            f"Cease your hesitation and witness this battle now:\n"
            f"- [Watch on KuroAnime](#anime/{it['anime_id']}?ep={ep})"
        )
        action = {
            "type": "play",
            "anime_id": it["anime_id"],
            "episode": ep,
            "time": sec,
            "title": it["title_english"] or it["title"],
        }
        return reply, action

    if image_search_result and image_search_result.get("best_char_match"):
        bcm = image_search_result["best_char_match"]
        score_pct = int(bcm["score"] * 100)
        char_list = bcm.get("characters_list") or ([bcm["character"]] if bcm.get("character") else [])
        if len(char_list) > 1:
            char_display = ", ".join(f"**{c}**" for c in char_list[:-1]) + f", and **{char_list[-1]}**"
        else:
            char_display = f"**{bcm['character']}**"
        anime_name = bcm["anime_title"]
        it = bcm.get("matched_item")
        tags_list = (image_search_result.get("vision_tags") or [])[:5]
        tags_line = f"- **Visual Cues:** {', '.join(tags_list)}\n" if tags_list else ""
        if it:
            display_title = it["title_english"] or it["title"]
            syn = re.sub(r"<[^>]+>", "", (it.get("synopsis") or "")).strip()
            syn_snip = (syn[:210].rsplit(" ", 1)[0] + "...") if len(syn) > 210 else syn
            syn_line = f"- **The Prince's Verdict:** {syn_snip}\n" if syn_snip else ""
            reply = (
                f"## Prince Vegeta's Saiyan Scouter • Visual Match Confirmed!\n\n"
                f"Hmph! Even a cropped frame or cluttered screenshot cannot deceive the Prince of Saiyans! "
                f"My Scouter locks onto {char_display} from **{display_title}** (**{score_pct}%** neural vision match)!\n\n"
                f"- **Rating:** ★ {it['score']}/10 • {it.get('episode_count') or '?'} Episodes ({it.get('type') or 'TV'})\n"
                f"- **Genres:** {it['genres']}\n"
                f"{tags_line}"
                f"{syn_line}\n"
                f"- [Watch on KuroAnime](#anime/{it['anime_id']}?ep=1)"
            )
            action = {
                "type": "play",
                "anime_id": it["anime_id"],
                "episode": 1,
                "time": 0,
                "title": display_title,
            }
            return reply, action
        reply = f"Hmph! My Scouter locks onto {char_display} from **{anime_name}** (**{score_pct}%** visual match)!"
        return reply, None

    # 0. Agentic Play Command Execution
    play_action = _detect_play_intent(user_msg, items, user_context)
    if play_action:
        ep_val = play_action.get("episode", 1)
        reply = (
            f"## Prince Vegeta's Command • Auto-Launching Stream\n\n"
            f"Hmph! As you command, warrior. Initiating **{play_action['title']}** (Episode {ep_val}). "
            f"Prepare your spirit for battle!\n\n"
            f"- [Watch on KuroAnime](#anime/{play_action['anime_id']}?ep={ep_val})"
        )
        return reply, play_action

    # Multi-Turn Active Context Resolution
    active_anime = _vg_extract_active_context(history, user_context, kb)

    # 1. Saiyan Banter & Debates (who is stronger you or goku, can you beat beerus, saitama, bulma, etc.)
    banter = _vg_handle_saiyan_banter(msg_low)
    if banter:
        return banter

    # 1.1. Iconic Quotes & Catchphrases (who says i am atomic, honored one, set your heart ablaze, tatakae, etc.)
    quote_res = _vg_handle_quote_inquiry(msg_low, kb)
    if quote_res:
        return quote_res

    # 1.2. Voice Actors & Seiyuu Inquiries (Kenjiro Tsuda, Takehito Koyasu, Hiroshi Kamiya, Christopher Sabat, etc.)
    va_res = _vg_handle_seiyuu_inquiry(msg_low, kb)
    if va_res:
        return va_res

    # 1.3. Animation Studios Showcase (ufotable, MAPPA, Madhouse, Bones, KyoAni, WIT, Trigger, etc.)
    studio_res = _vg_handle_studio_inquiry(msg_low, kb)
    if studio_res:
        return studio_res

    # 1.4. Franchise Watch Orders (Fate, Monogatari, JoJo, Dragon Ball, etc.)
    order_res = _vg_handle_franchise_watch_order(msg_low, kb)
    if order_res:
        return order_res

    # 1.5. Reverse Plot / Forgotten Anime Identification (check before character aliases when describing a story)
    identify_phrase = any(k in msg_low for k in [
        "what is that anime", "what's that anime", "whats that anime", "which anime", "forgot the name",
        "anime where", "show where", "the one where", "name of the anime", "what anime", "anime about a",
        "what is the anime", "what's the anime", "cant remember", "can't remember", "story where",
        "background story", "plot is", "about a guy who", "about a boy who", "about a girl who",
    ])
    is_char_q = re.match(r"^(?:who is|who's|whos|why did|tell me about)\b", msg_low)
    if not is_char_q and (identify_phrase or len(re.findall(r"[a-z0-9']{4,}", msg_low)) >= 5):
        plot_item, plot_title = _find_by_plot_description(msg_low, items)
        if plot_item:
            reply = (
                f"## Prince Vegeta's Scouting Report • Anime Identified!\n\n"
                f"Hmph! You dare describe a battle without knowing its royal name? "
                f"That is indisputably **{plot_item['title_english'] or plot_item['title']}**!\n\n"
                f"- **Rating:** ★ {plot_item['score']}/10 ({'Masterpiece' if plot_item['score'] >= 8.5 else 'Elite'})\n"
                f"- **Format:** {plot_item['episode_count']} Episodes • {plot_item['type']} ({plot_item['status']})\n"
                f"- **The Prince's Dossier:** {(plot_item.get('synopsis') or '')[:260]}...\n\n"
                f"Cease your hesitation and stream it now:\n"
                f"- [Watch on KuroAnime](#anime/{plot_item['anime_id']}?ep=1)"
            )
            action = {
                "type": "play",
                "anime_id": plot_item["anime_id"],
                "episode": 1,
                "time": 0,
                "title": plot_item["title_english"] or plot_item["title"]
            }
            return reply, action

    if _vg_is_scene_or_plot_query(msg_low):
        scene_cands = _vg_identify_scene_candidates(msg_low, items, kb=kb, limit=3)
        if scene_cands:
            _, p_item, p_ep, _ = scene_cands[0]
            p_ep = p_ep or 1
            p_title = _vg_title(p_item)
            p_aid = p_item["anime_id"]
            syn = re.sub(r"<[^>]+>", "", (p_item.get("synopsis") or "A renowned battle recorded in the archives.")).strip()
            if len(syn) > 260:
                syn = syn[:257].rsplit(" ", 1)[0] + "..."
            genres_str = p_item.get("genres") or "Action, Fantasy"
            score_val = p_item.get("score") or "8.6+"
            out_txt = (
                f"Hmph! You describe the scene well enough—my Royal Scouter locked onto that plot signature immediately!\n\n"
                f"🔥 **Identified Anime: [{p_title}](#anime/{p_aid}?ep={p_ep})**\n"
                f"- **Power Rating:** ⭐ `{score_val}` | **Format:** `{p_item.get('type') or 'TV'}` (`{p_item.get('episode_count') or '?'} eps`)\n"
                f"- **Genres:** {genres_str}\n"
                f"- **Synopsis:** {syn}\n\n"
                f"👉 **[Watch {p_title} (Ep {p_ep}) on KuroAnime](#anime/{p_aid}?ep={p_ep})**"
            )
            if len(scene_cands) > 1:
                alts = [f"**[{_vg_title(r_it)}](#anime/{r_it['anime_id']}?ep={r_ep or 1})**" for _, r_it, r_ep, _ in scene_cands[1:3]]
                out_txt += f"\n\n⚡ *Other closest Scouter matches:* {', '.join(alts)}"
            return out_txt

    # 2. Character & Lore Inquiries (who is tanjiro, who is his sister, why did sasuke leave, gojo, sukuna, etc.)
    char_inq = _vg_handle_character_inquiry(msg_low, active_anime, kb)
    if char_inq:
        return char_inq

    # 3. Follow-up Questions on Active Anime (how many episodes, is season 2 out, is it finished, who made it, watch order, etc.)
    follow_up = _vg_handle_followup_question(msg_low, active_anime, kb)
    if follow_up:
        return follow_up

    # 5. User Watch History / Resume Inquiries
    if any(k in msg_low for k in ["what was i watching", "what am i watching", "continue watching", "resume", "where did i leave off", "my watchlist", "what show was i on", "what should i continue", "my history"]):
        if user_context and user_context.get("continue_watching"):
            cw_list = user_context["continue_watching"]
            top = cw_list[0]
            other_shows = [f"- **{s.get('title')}** (Ep {s.get('episode')}) • [Watch on KuroAnime](#anime/{s.get('anime_id')})" for s in cw_list[1:4]]
            other_str = ("\n\n### Other In-Progress Battles in Your Queue:\n" + "\n".join(other_shows)) if other_shows else ""
            return (
                f"## Prince Vegeta's Scouting Report • Active Training\n\n"
                f"Hmph! Slacker! You left off on **{top.get('title')}** (Episode {top.get('episode')}).\n\n"
                f"Did you think the Prince of all Saiyans wouldn't notice you abandoning your watch queue? "
                f"Cease making excuses and get back to streaming immediately:\n\n"
                f"- [Watch on KuroAnime](#anime/{top.get('anime_id')}){other_str}\n\n"
                f"A true warrior never leaves an unfinished battle!"
            ), None
        elif user_context and user_context.get("current_anime"):
            ca = user_context["current_anime"]
            return (
                f"## Prince Vegeta's Scouting Report\n\n"
                f"Hmph! You are currently looking at **{ca.get('title')}** (Episode {ca.get('current_episode', 1)}).\n\n"
                f"Stop loitering in the menus and hit play:\n\n"
                f"- [Watch on KuroAnime](#anime/{ca.get('anime_id')})"
            ), None

    # 6. Saiyan Scouter: Canon vs Filler Inquiries
    is_filler_query = any(k in msg_low for k in ["filler", "canon", "skip filler", "filler list", "is it filler", "what to skip", "what episodes to skip"])
    if is_filler_query:
        if "naruto" in msg_low:
            if "shippuden" in msg_low:
                return (
                    "## Prince Vegeta's Saiyan Scouter • Naruto Shippuden Canon Guide\n\n"
                    "Listen well, warrior! Naruto Shippuden is plagued by filler, but its canon battles rival Saiyan warfare:\n\n"
                    "- **Must-Watch Canon Sagas:** Kazekage Rescue (1-32), Immortal Duo Hidan & Kakuzu (72-88), Pain's Assault (152-175), Five Kage Summit (197-214), and Fourth Great Ninja War Climax (470-479).\n"
                    "- **Worthless Filler to Skip:** Three-Tails Arc (90-112), Six-Tails Arc (144-151), Paradise Life on a Boat (223-242), and Infinite Tsukuyomi Dreams (427-450).\n\n"
                    "A true warrior trains only against canon strength!\n\n"
                    "- [Watch on KuroAnime](#anime/20)"
                ), None
            return (
                "## Prince Vegeta's Saiyan Scouter • Naruto (Original) Canon Guide\n\n"
                "Hmph! Pay attention! Original Naruto has a very simple rule for true warriors:\n\n"
                "- **Episodes 1 to 135:** 100% pure canon and elite shounen combat (Chunin Exams & Sasuke Retrieval).\n"
                "- **Episodes 136 to 219:** 100% worthless filler! Not a single shred of manga plot exists here. Skip every single episode!\n"
                "- **Episode 220:** Watch only the second half, then jump straight into **Naruto Shippuden**!\n\n"
                "- [Watch on KuroAnime](#anime/20)"
            ), None
        if "bleach" in msg_low:
            return (
                "## Prince Vegeta's Saiyan Scouter • Bleach Canon & Filler Guide\n\n"
                "Tch! Bleach has some of the most insulting filler interruptions in anime history, but its soul reaper battles are legendary when unhindered:\n\n"
                "- **Pure Canon:** Substitute Shinigami (1-20), Soul Society (21-63), Arrancar & Hueco Mundo (110-167, 190-205), Fake Karakura Town (215-226, 266-310), and Thousand-Year Blood War.\n"
                "- **Trash Filler to Purge:** The Bount Arc (64-109) — completely skip it!\n\n"
                "- [Watch on KuroAnime](#anime/269)"
            ), None
        if "one piece" in msg_low or "luffy" in msg_low:
            return (
                "## Prince Vegeta's Saiyan Scouter • One Piece Canon Guide\n\n"
                "Hmph! One Piece has an exceptionally low filler rate (under 9%). Almost everything is canon:\n\n"
                "- **Canon Sagas:** East Blue, Alabasta, Enies Lobby, Marineford, Dressrosa, Whole Cake Island, and Wano.\n"
                "- **The One Exception:** The G-8 Arc (Episodes 195-206) is technically filler, yet even I admit Commander Jonathan's tactical battle is worth watching.\n\n"
                "- [Watch on KuroAnime](#anime/21)"
            ), None

    # 7. Total Catalog Stats / Genres
    if any(k in msg_low for k in ["how many anime", "how many shows", "total anime", "catalog size", "all anime"]):
        return (
            f"## KuroAnime Master Archives\n"
            f"- **Total Catalog:** **{total_anime:,} Anime Titles** verified in high definition.\n"
            f"- **Episode Archive:** Over **{total_episodes:,} Sub & Dub streaming episodes** available with zero advertisements.\n"
            f"- **Seasons Database:** Over 6,800 chronological season relationships fully mapped.\n"
            f"Name any show and I will summon its dossier!\n"
        ), None

    if any(k in msg_low for k in ["what genres", "list genres", "all genres", "show genres", "which genres"]):
        return (
            f"## KuroAnime Complete Genre Directory\n"
            f"As Prince of all Saiyans, I maintain absolute command over every genre recorded across our **{total_anime:,} anime** archives:\n\n"
            f"- **Battle & Adrenaline:** Action, Shounen, Martial Arts, Super Power, Samurai, Military, Mecha\n"
            f"- **Dark & Gripping:** Dark Fantasy, Psychological, Thriller, Suspense, Horror, Demons\n"
            f"- **High Fantasy & Worlds:** Fantasy, Isekai, Reincarnation, Magic, Sci-Fi, Cyberpunk\n"
            f"- **Mystery & Intrigue:** Mystery, Detective, Police, Time Travel, Survival\n"
            f"- **Heart & Emotion:** Drama, Romance, Slice of Life, School, Sports, Comedy\n\n"
            f"Tell me which category you demand, and I will present its undisputed champions!"
        ), None

    # 8. Specific Anime Title Inquiry (Human-like conversational review)
    if not _is_pure_genre_query(msg_low):
        specific, related = _find_exact_or_fuzzy_title(user_msg, items)
        if specific:
            return _vg_generate_human_anime_review(specific)

    # 8.5. Fallback Scene / Plot Identification before genre recommendations
    is_rec_query = bool(re.search(r"\b(recommend|suggest|what should i watch|give me (?:an|some)|top anime|best anime to watch|what to watch|good anime to watch|surprise me)\b", msg_low))
    if not is_rec_query and len(msg_low.split()) >= 5:
        scene_cands = _vg_identify_scene_candidates(msg_low, items, kb=kb, limit=3)
        if scene_cands:
            _, p_item, p_ep, _ = scene_cands[0]
            p_ep = p_ep or 1
            p_title = _vg_title(p_item)
            p_aid = p_item["anime_id"]
            syn = re.sub(r"<[^>]+>", "", (p_item.get("synopsis") or "A renowned battle recorded in the archives.")).strip()
            if len(syn) > 260:
                syn = syn[:257].rsplit(" ", 1)[0] + "..."
            genres_str = p_item.get("genres") or "Action, Fantasy"
            score_val = p_item.get("score") or "8.6+"
            out_txt = (
                f"Hmph! Based on that description, my Royal Scouter has locked onto your target!\n\n"
                f"🔥 **Identified Anime: [{p_title}](#anime/{p_aid}?ep={p_ep})**\n"
                f"- **Power Rating:** ⭐ `{score_val}` | **Format:** `{p_item.get('type') or 'TV'}` (`{p_item.get('episode_count') or '?'} eps`)\n"
                f"- **Genres:** {genres_str}\n"
                f"- **Synopsis:** {syn}\n\n"
                f"👉 **[Watch {p_title} (Ep {p_ep}) on KuroAnime](#anime/{p_aid}?ep={p_ep})**"
            )
            if len(scene_cands) > 1:
                alts = [f"**[{_vg_title(r_it)}](#anime/{r_it['anime_id']}?ep={r_ep or 1})**" for _, r_it, r_ep, _ in scene_cands[1:3]]
                out_txt += f"\n\n⚡ *Other closest Scouter matches:* {', '.join(alts)}"
            return out_txt, None

    # 9. Explicit Recommendations Request (ONLY if user actually asked for recommendations!)
    detected_genres = _detect_query_genres(msg_low)

    if is_rec_query or detected_genres:
        by_franchise = {}
        if detected_genres:
            genre_label = ", ".join(g.title() for g in detected_genres)
            for it in items:
                is_match = False
                for dg in detected_genres:
                    syns = GENRE_MAP.get(dg, [dg])
                    if dg in it["genres_lower"] or any(s in it["genres_lower"] for s in syns):
                        is_match = True
                        break
                if not is_match:
                    continue
                b = _clean_base_title(it["title_english"] or it["title"])
                if b not in by_franchise or (it.get("popularity", 0) > by_franchise[b].get("popularity", 0)):
                    by_franchise[b] = it
        else:
            genre_label = "Supreme Masterpieces"
            for it in items:
                if it.get("score", 0) < 8.2:
                    continue
                b = _clean_base_title(it["title_english"] or it["title"])
                if b not in by_franchise or (it.get("popularity", 0) > by_franchise[b].get("popularity", 0)):
                    by_franchise[b] = it

        cands = list(by_franchise.values())
        cands.sort(key=lambda x: (x.get("score", 0) or 0, x.get("popularity", 0) or 0), reverse=True)
        top_candidates = cands[:3] if cands else items[:3]

        out = [
            f"Hmph! If you hunger for true power, here are three elite champions of **{genre_label}** that survive my royal standards:\n"
        ]
        for i, it in enumerate(top_candidates, 1):
            t = it["title_english"] or it["title"]
            tier = "Masterpiece" if it["score"] >= 8.8 else ("Elite" if it["score"] >= 8.0 else "Top Tier")
            syn = (it["synopsis"][:170].rsplit(" ", 1)[0] + "...") if len(it["synopsis"]) > 170 else it["synopsis"]
            out.append(f"## {i}. {t}")
            out.append(f"- **Rating:** ★ {it['score']}/10 ({tier}) • **Studio:** {it['studios']}")
            out.append(f"- **The Prince's Take:** {syn}")
            out.append(f"- [Watch on KuroAnime](#anime/{it['anime_id']}?ep=1)\n")

        out.append("Pick your battlefield and begin streaming immediately!")
        return "\n".join(out), {"type": "play", "anime_id": top_candidates[0]["anime_id"], "episode": 1, "time": 0, "title": top_candidates[0]["title_english"] or top_candidates[0]["title"]}

    # 10. Natural Conversational Fallback (Human Interaction - ZERO top-10 slop!)
    return (
        "Hmph! My Saiyan Scouter isn't picking up an exact battle record under that query, warrior.\n\n"
        "Are you describing a specific anime, a particular character, or a storyline you forgot? "
        "Name any show, mention a character (like Tanjiro, Gojo, or Eren), or describe a scene, and I will track it down immediately!"
    ), None


_CLINE_API_BASE = "https://api.cline.bot/api/v1"
_CLINE_MODELS_CACHE = {"models": [], "ts": 0.0, "total": 0}
_CLINE_BAD_MODELS = {}
_CLINE_MODELS_TTL_SEC = 1800.0
_CLINE_BAD_MODEL_TTL_SEC = 600.0
_OPENAI_TOOLS_CACHE = None


def _vg_load_local_env() -> None:
    """Load local .env files safely (without overwriting explicit system env vars when non-empty)."""
    env_files = [".env.local", ".env", "anime_app/web/.env.local", "anime_app/web/.env"]
    try:
        from dotenv import load_dotenv
        for _ef in env_files:
            if os.path.exists(_ef):
                load_dotenv(_ef, override=False)
    except Exception:
        pass
    for _ef in env_files:
        if not os.path.exists(_ef):
            continue
        try:
            with open(_ef, "r", encoding="utf-8", errors="ignore") as _f:
                for _raw in _f:
                    _line = _raw.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _k, _v = _line.split("=", 1)
                    _k = _k.strip()
                    _v = _v.strip().strip('"').strip("'")
                    if _k and _v and not os.environ.get(_k):
                        os.environ[_k] = _v
        except Exception:
            pass


def _vg_get_cline_api_key(body: Optional[dict] = None) -> str:
    """Resolve Cline API key from optional request body or environment."""
    if isinstance(body, dict):
        client_cline = (body.get("clineKey") or "").strip()
        if client_cline:
            return client_cline
    return (
        os.environ.get("CLINE_API_KEY")
        or os.environ.get("VITE_CLINE_API_KEY")
        or ""
    ).strip()


def _vg_fetch_cline_free_models(api_key: str, force_refresh: bool = False) -> list:
    """Dynamically fetch and rank `:free` chat models from Cline `/api/v1/models`."""
    if not api_key:
        return []
    now = _time.time()
    cached = _CLINE_MODELS_CACHE.get("models") or []
    if not force_refresh and cached and (now - float(_CLINE_MODELS_CACHE.get("ts") or 0.0)) < _CLINE_MODELS_TTL_SEC:
        return cached

    raw_models = []
    try:
        res = _STREAM_SESSION.get(
            f"{_CLINE_API_BASE}/models",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=10,
        )
        if res.status_code == 200:
            payload = res.json()
            raw_models = payload.get("data") or payload.get("models") or []
            _CLINE_MODELS_CACHE["total"] = len(raw_models)
    except Exception as err:
        print(f"Cline /models fetch failed: {err}")
        if cached:
            return cached
        return []

    preferred_ids = (
        "baidu/ernie-5.5-flash-preview:free",
        "particle-ai/particle-1:free",
        "liquid/lfm-3-1.7b:free",
        "google/gemma-4-31b-it:free",
        "google/gemma-4-26b-a4b-it:free",
    )
    preferred_rank = {mid: idx for idx, mid in enumerate(preferred_ids)}

    free_models = []
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid:
            continue
        pricing = item.get("pricing") or {}
        is_zero_price = str(pricing.get("prompt", "")).strip() == "0" and str(pricing.get("completion", "")).strip() == "0"
        if not (mid.endswith(":free") or is_zero_price):
            continue
        arch = item.get("architecture") or {}
        in_mods = [str(m).lower() for m in (arch.get("input_modalities") or [])]
        sup_params = [str(p).lower() for p in (item.get("supported_parameters") or [])]
        supports_images = ("image" in in_mods) or any(tok in mid.lower() for tok in ("-vl", "omni", "vision"))
        supports_tools = ("tools" in sup_params) or (mid in ("baidu/ernie-5.5-flash-preview:free", "particle-ai/particle-1:free"))
        free_models.append({
            "id": mid,
            "name": str(item.get("name") or mid),
            "supports_images": supports_images,
            "supports_tools": supports_tools,
            "context_length": int(item.get("context_length") or 0),
        })

    def _sort_key(entry: dict):
        mid = entry["id"]
        if mid in preferred_rank:
            return (0, preferred_rank[mid], mid)
        tool_tier = 1 if entry["supports_tools"] else 2
        speed_hint = 0 if any(t in mid.lower() for t in ("flash", "mini", "nano", "lightning", "1.7b", "1:")) else 1
        return (tool_tier, speed_hint, mid)

    free_models.sort(key=_sort_key)
    _CLINE_MODELS_CACHE["models"] = free_models
    _CLINE_MODELS_CACHE["ts"] = now
    return free_models


@router.get("/ai/status")
def ai_status():
    _vg_load_local_env()
    kb = get_vegeta_full_knowledge()
    env_gemini_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("VITE_GEMINI_API_KEY") or "").strip()
    env_cline_key = _vg_get_cline_api_key()
    cline_free = _vg_fetch_cline_free_models(env_cline_key) if env_cline_key else []
    has_gemini = bool(env_gemini_key and not env_gemini_key.startswith("0x"))
    default_provider = "cline" if env_cline_key else ("gemini" if has_gemini else "local")
    return {
        "status": "ready",
        "name": "Prince Vegeta Anime Advisor",
        "total_anime": kb["total_anime"],
        "total_episodes": kb["total_episodes"],
        "stats": kb.get("stats", {}),
        "has_cline_key": bool(env_cline_key),
        "has_gemini_key": has_gemini,
        "default_provider": default_provider,
        "cline_total_models": int(_CLINE_MODELS_CACHE.get("total") or 0),
        "cline_free_models_count": len(cline_free),
        "cline_free_models": [m["id"] for m in cline_free],
    }


# ─── Vegeta Full Knowledge (Server 1 ZokoAnime + Server 2 Munib Server) ──────
_VEGETA_STOPWORDS = set("""
a an the and or but if then than so to of in on at by for with about from into over under is are was were be been
being am do does did done have has had having i me my we our you your he she it its they them their this that these
those what which who whom whose why how when where can could should would will shall may might must not no yes
anime anything something show shows series watch watching please tell give want need like good best some any more
most very much many recommend recommendation suggest list kuroanime vegeta prince bro hey hello hi okay ok thanks
episode episodes season seasons movie movies kya hai hain mujhe koi batao bata acha achi wala wali
""".split())


def get_vegeta_full_knowledge():
    """Extend the hianime.db knowledge base with every Munib Server exclusive title and live catalog stats."""
    kb = get_vegeta_knowledge_base()
    if kb.get("munib_merged"):
        return kb

    items = kb["items"]
    by_id = kb["by_id"]
    s1_anime = len(items)
    s1_episodes = kb.get("total_episodes", 0)
    try:
        s1_streams = one("SELECT COUNT(*) n FROM streams")["n"]
    except Exception:
        s1_streams = 0

    m_anime_rows = munib_rows("""
        SELECT anime_id, anilist_id, title, title_english, title_romaji, japanese_name, type, status, genres,
               season, season_year, anilist_score, anilist_popularity, episode_count, subbed_count, dubbed_count
        FROM anime
    """)
    m_ep = munib_rows("SELECT COUNT(*) AS n FROM episodes")
    m_st = munib_rows("SELECT COUNT(*) AS n FROM streams")
    m_dub = munib_rows("SELECT COUNT(DISTINCT anime_id) AS n FROM streams WHERE type = 'dub'")
    s2_episodes = m_ep[0]["n"] if m_ep else 0
    s2_streams = m_st[0]["n"] if m_st else 0
    s2_dub_anime = m_dub[0]["n"] if m_dub else 0

    by_al = kb.setdefault("by_anilist_id", {})
    exclusive_added = 0
    for r in m_anime_rows:
        a_id = r.get("anime_id")
        al_id = r.get("anilist_id")
        if a_id is None:
            continue
        if a_id in by_id:
            by_id[a_id]["on_munib"] = True
            if al_id and int(al_id) > 0:
                if not by_id[a_id].get("anilist_id"):
                    by_id[a_id]["anilist_id"] = int(al_id)
                by_al.setdefault(int(al_id), by_id[a_id])
            continue
        title = (r.get("title") or "").strip()
        title_eng = (r.get("title_english") or title).strip()
        title_rom = (r.get("title_romaji") or "").strip()
        title_jp = (r.get("japanese_name") or "").strip()
        if not title and not title_eng:
            continue
        raw_score = r.get("anilist_score")
        try:
            score = float(raw_score) if raw_score else 0.0
            score = round(score / 10.0, 1) if score > 10 else round(score, 1)
        except Exception:
            score = 0.0
        genres = (r.get("genres") or "").strip()
        year = str(r.get("season_year") or "")
        norm_titles = [t for t in dict.fromkeys([
            _normalize_str(title), _normalize_str(title_eng), _normalize_str(title_rom), _normalize_str(title_jp)
        ]) if t]
        item = {
            "anime_id": a_id,
            "anilist_id": int(al_id) if (al_id and int(al_id) > 0) else None,
            "title": title or title_eng,
            "title_english": title_eng or title,
            "title_romaji": title_rom,
            "synonyms": "",
            "all_titles_norm": norm_titles,
            "franchise": _franchise_key(title_eng or title),
            "type": r.get("type") or "TV",
            "status": r.get("status") or "Finished Airing",
            "year": year,
            "season_year": int(year) if year.isdigit() else 0,
            "premiered": f"{r.get('season') or ''} {year}".strip(),
            "score": score or 7.0,
            "popularity": r.get("anilist_popularity") or 0,
            "studios": "Unknown Studio",
            "studios_lower": "",
            "duration": "24m",
            "episode_count": r.get("episode_count") or max(r.get("subbed_count") or 0, r.get("dubbed_count") or 0) or 1,
            "genres": genres or "Anime",
            "genres_lower": genres.lower(),
            "synopsis": "",
            "synopsis_lower": "",
            "seasons_json": None,
            "seasons": [],
            "related_franchise": [],
            "on_munib": True,
            "munib_exclusive": True,
        }
        by_id[a_id] = item
        if item["anilist_id"]:
            by_al.setdefault(item["anilist_id"], item)
        items.append(item)
        exclusive_added += 1

    # Enrich BOTH Server 1 and Server 2 items with MyAnimeList / AniList trained knowledge
    mal_data = {}
    for mal_path in [
        os.path.join(CURRENT_DIR, "vegeta_mal_knowledge.json.gz"),
        os.path.join(PARENT_DIR, "vegeta_mal_knowledge.json.gz"),
        os.path.join(CURRENT_DIR, "api", "vegeta_mal_knowledge.json.gz"),
        os.path.join(os.getcwd(), "api", "vegeta_mal_knowledge.json.gz"),
        os.path.join(os.getcwd(), "anime_app", "web", "api", "vegeta_mal_knowledge.json.gz"),
    ]:
        if os.path.exists(mal_path):
            try:
                import gzip as _gzip
                with _gzip.open(mal_path, "rb") as _mf:
                    mal_data = json.loads(_mf.read().decode("utf-8"))
                break
            except Exception as _me:
                print(f"MAL knowledge load error ({mal_path}): {_me}")

    by_mal_id = {}
    char_index = {}
    for it in items:
        al_id = it.get("anilist_id")
        meta = mal_data.get(str(al_id)) if al_id else None
        tags = []
        chars = []
        if meta:
            if meta.get("mal"):
                it["mal_id"] = int(meta["mal"])
                by_mal_id.setdefault(int(meta["mal"]), it)
            if meta.get("ds"):
                ds_txt = str(meta["ds"]).strip()
                cur_syn = str(it.get("synopsis") or "").strip()
                if not cur_syn or len(ds_txt) > len(cur_syn):
                    it["synopsis"] = ds_txt
                comb_syn = (cur_syn + " " + ds_txt).strip().lower()
                it["synopsis_lower"] = comb_syn
            if (not it.get("studios") or it.get("studios") == "Unknown Studio") and meta.get("st"):
                it["studios"] = ", ".join(meta["st"])
                it["studios_lower"] = it["studios"].lower()
            if meta.get("syn"):
                existing_norm = set(it.get("all_titles_norm") or [])
                for s in meta["syn"]:
                    ns = _normalize_str(s)
                    if ns and ns not in existing_norm:
                        it["all_titles_norm"].append(ns)
                        existing_norm.add(ns)
            tags = meta.get("tg") or []
            chars = meta.get("ch") or []

        it["tags"] = tags
        it["tags_lower"] = " ".join(tags).lower()
        it["characters"] = chars
        char_names = []
        for ch in chars:
            cn = ch.get("n") or ""
            if not cn:
                continue
            char_names.append(cn)
            c_role = ch.get("r") or "S"
            keys_to_add = [cn.lower()] + [a.lower() for a in (ch.get("a") or []) if a]
            # Also index individual name tokens (>= 4 chars) if main character for natural queries
            if c_role == "M":
                for tok in re.findall(r"[a-z]{4,}", cn.lower()):
                    keys_to_add.append(tok)
            for ck in dict.fromkeys(keys_to_add):
                ck_clean = re.sub(r"[^a-z0-9\s]", " ", ck).strip()
                if len(ck_clean) >= 3:
                    char_index.setdefault(ck_clean, []).append((cn, c_role, it))
        it["characters_lower"] = " ".join(char_names).lower()

    # Sort character_index entries so Main characters and highest-popularity anime rank first
    for ck in char_index:
        char_index[ck].sort(key=lambda x: (1 if x[1] == "M" else 0, x[2].get("popularity") or 0), reverse=True)

    # Enrich with deep MAL/AniList character lore & Japanese Seiyuu index
    char_lore_by_key: dict = {}
    seiyuu_index: dict = {}
    lore_list = []
    for lore_path in [
        os.path.join(CURRENT_DIR, "vegeta_char_lore.json.gz"),
        os.path.join(PARENT_DIR, "vegeta_char_lore.json.gz"),
        os.path.join(CURRENT_DIR, "api", "vegeta_char_lore.json.gz"),
        os.path.join(os.getcwd(), "api", "vegeta_char_lore.json.gz"),
        os.path.join(os.getcwd(), "anime_app", "web", "api", "vegeta_char_lore.json.gz"),
    ]:
        if os.path.exists(lore_path):
            try:
                import gzip as _gzip
                with _gzip.open(lore_path, "rb") as _lf:
                    lore_list = json.loads(_lf.read().decode("utf-8"))
                break
            except Exception as _le:
                print(f"Character lore load error ({lore_path}): {_le}")

    if lore_list:
        by_norm_title: dict = {}
        for _it_t in items:
            for _nt in (_it_t.get("all_titles_norm") or ()):
                if not _nt or len(_nt) < 2:
                    continue
                prev_it = by_norm_title.get(_nt)
                if prev_it is None or _vg_anime_entry_priority(_it_t) > _vg_anime_entry_priority(prev_it):
                    by_norm_title[_nt] = _it_t

        for rc in lore_list:
            cands_it = []
            if rc.get("al") and int(rc["al"]) in by_al:
                cands_it.append(by_al[int(rc["al"])])
            if rc.get("mal") and int(rc["mal"]) in by_mal_id:
                cands_it.append(by_mal_id[int(rc["mal"])])
            if rc.get("t"):
                _t_key = _normalize_str(rc["t"])
                if _t_key in by_norm_title:
                    cands_it.append(by_norm_title[_t_key])
            if not cands_it:
                continue
            cands_it.sort(key=_vg_anime_entry_priority, reverse=True)
            an_it = cands_it[0]
            rec_obj = dict(rc)
            rec_obj["item"] = an_it
            cname = rc.get("n") or ""
            keys_add = [cname.lower()] + [a.lower() for a in (rc.get("a") or []) if a]
            for ck in dict.fromkeys(keys_add):
                ck_clean = re.sub(r"[^a-z0-9\s]", " ", ck).strip()
                ck_clean = re.sub(r"\s+", " ", ck_clean)
                if len(ck_clean) >= 3:
                    char_lore_by_key.setdefault(ck_clean, []).append(rec_obj)
                    toks_c = ck_clean.split()
                    if len(toks_c) == 2:
                        char_lore_by_key.setdefault(f"{toks_c[1]} {toks_c[0]}", []).append(rec_obj)
                    if rc.get("r") == "M":
                        for tk in toks_c:
                            if len(tk) >= 4:
                                char_lore_by_key.setdefault(tk, []).append(rec_obj)
            # Feed rich bio + aliases into anime's scene search tokens
            bio_snip = (rc.get("bio") or "")[:360]
            extra_txt = " ".join([cname, " ".join(rc.get("a") or []), rc.get("va") or "", bio_snip]).lower().strip()
            if extra_txt:
                prev_cl = an_it.get("char_lore_lower") or ""
                if len(prev_cl) < 2800:
                    an_it["char_lore_lower"] = (prev_cl + " " + extra_txt).strip()
            va_raw = (rc.get("va") or "").strip()
            if va_raw:
                va_clean = re.sub(r"[^a-z0-9\s]", " ", va_raw.lower()).strip()
                va_toks = va_clean.split()
                va_keys = [va_clean]
                if len(va_toks) == 2:
                    va_keys.append(f"{va_toks[1]} {va_toks[0]}")
                for vk in dict.fromkeys(va_keys):
                    if len(vk) >= 4:
                        seiyuu_index.setdefault(vk, []).append((va_raw, cname, an_it, int(rc.get("fav") or 0)))

        for k_cl in char_lore_by_key:
            char_lore_by_key[k_cl].sort(key=lambda x: (1 if x.get("r") == "M" else 0, int(x.get("fav") or 0)), reverse=True)
        for k_va in seiyuu_index:
            seiyuu_index[k_va].sort(key=lambda x: x[3], reverse=True)

    kb["char_lore_by_key"] = char_lore_by_key
    kb["seiyuu_index"] = seiyuu_index

    # Load named episode titles from hianime.db for episode-level scene recognition
    try:
        ep_rows = rows(
            "SELECT anime_id, episode_number, title FROM episodes "
            "WHERE title IS NOT NULL AND title != '' AND title NOT LIKE 'Episode %'"
        )
        for er in ep_rows:
            aid = er.get("anime_id")
            ep_num = er.get("episode_number")
            ep_title = er.get("title")
            it_obj = by_id.get(int(aid)) if aid is not None else None
            if it_obj is not None and ep_title:
                t_clean = str(ep_title).strip()
                if "ep_titles" not in it_obj:
                    it_obj["ep_titles"] = []
                if len(it_obj["ep_titles"]) < 60:
                    it_obj["ep_titles"].append((int(ep_num or 1), t_clean))
        for it_obj in items:
            if it_obj.get("ep_titles"):
                it_obj["ep_titles_lower"] = " ".join(x[1].lower() for x in it_obj["ep_titles"])
    except Exception:
        pass

    # Precompute stemmed plot tokens and IDF weights across all anime
    try:
        import math
        df_counts = {}
        n_docs = max(len(items), 1)
        for it_obj in items:
            blob = " ".join([
                (it_obj.get("title") or "").lower(),
                (it_obj.get("title_english") or "").lower(),
                (it_obj.get("title_romaji") or "").lower(),
                (it_obj.get("synonyms") or "").lower(),
                it_obj.get("synopsis_lower") or "",
                it_obj.get("characters_lower") or "",
                it_obj.get("char_lore_lower") or "",
                it_obj.get("tags_lower") or "",
                it_obj.get("ep_titles_lower") or "",
            ])
            raw_toks = set(re.findall(r"[a-z0-9']{3,}", blob))
            stemmed_set = set()
            for tok in raw_toks:
                if len(tok) >= 4 or tok in _VG_SHORT_SCENE_WORDS:
                    stemmed_set.add(tok)
                    st = _vg_stem_word(tok)
                    if len(st) >= 3:
                        stemmed_set.add(st)
            it_obj["stemmed_plot_tokens"] = stemmed_set
            for st in stemmed_set:
                df_counts[st] = df_counts.get(st, 0) + 1
        idf_map = {}
        for tok, df in df_counts.items():
            idf_map[tok] = round(math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5)), 3)
        kb["idf"] = idf_map
    except Exception:
        kb["idf"] = {}

    kb["by_mal_id"] = by_mal_id
    kb["character_index"] = char_index

    genre_counts = {}
    type_counts = {}
    airing = 0
    for it in items:
        for g in re.split(r"[,/]", it.get("genres") or ""):
            g = g.strip()
            if g:
                genre_counts[g] = genre_counts.get(g, 0) + 1
        t = (it.get("type") or "TV").upper()
        type_counts[t] = type_counts.get(t, 0) + 1
        st = (it.get("status") or "").lower()
        if ("currently airing" in st or "releas" in st) and "finish" not in st:
            airing += 1

    items.sort(key=lambda x: (x.get("score") or 0, x.get("popularity") or 0), reverse=True)

    kb["total_anime"] = len(items)
    kb["total_episodes"] = s1_episodes + s2_episodes
    kb["stats"] = {
        "total_unique_anime": len(items),
        "server1_zokoanime_anime": s1_anime,
        "server1_zokoanime_episodes": s1_episodes,
        "server1_zokoanime_streams": s1_streams,
        "server2_munib_anime": len(m_anime_rows),
        "server2_munib_exclusive_anime": exclusive_added,
        "server2_munib_episodes": s2_episodes,
        "server2_munib_streams": s2_streams,
        "server2_munib_dub_anime": s2_dub_anime,
        "total_streams": s1_streams + s2_streams,
        "currently_airing": airing,
        "type_counts": dict(sorted(type_counts.items(), key=lambda x: -x[1])),
        "genre_counts": dict(sorted(genre_counts.items(), key=lambda x: -x[1])[:45]),
    }
    kb["munib_merged"] = True
    return kb


def _vegeta_line(it, synopsis_len=260):
    syn = (it.get("synopsis") or "").replace("\n", " ").strip()
    if len(syn) > synopsis_len:
        syn = syn[:synopsis_len].rsplit(" ", 1)[0] + "..."
    servers = "ZokoAnime + Munib Server" if (it.get("on_munib") and not it.get("munib_exclusive")) else (
        "Munib Server" if it.get("munib_exclusive") else "ZokoAnime")
    s_note = f", {len(it['seasons'])} seasons in franchise" if len(it.get("seasons") or []) > 1 else ""
    line = (
        f"- [ID {it['anime_id']}] {it.get('title_english') or it.get('title')}"
        f"{' (' + it['title'] + ')' if it.get('title') and it.get('title') != it.get('title_english') else ''}"
        f" | {it.get('type')} | {it.get('episode_count')} eps{s_note} | {it.get('status')} | {it.get('year') or '?'}"
        f" | score {it.get('score')}/10 | {it.get('genres')} | studio {it.get('studios')} | servers: {servers}"
    )
    if it.get("tags"):
        line += f" | tags: {', '.join(it['tags'][:6])}"
    if it.get("characters"):
        line += f" | chars: {', '.join(c['n'] for c in it['characters'][:5])}"
    if syn:
        line += f" | synopsis: {syn}"
    return line


def _vegeta_retrieve(user_msg, history, kb, image_search_result=None, has_image=False):
    """Ground every answer: pick the exact titles, genre matches, plot identification, character matches, and screenshot matches from BOTH servers."""
    items = kb["items"]
    msg_low = (user_msg or "").lower()
    blocks = []
    seen = set()

    def add(it):
        if it and it["anime_id"] not in seen:
            seen.add(it["anime_id"])
            return True
        return False

    # -1. Visual Image Analysis / trace.moe Scouter Report
    if has_image:
        if image_search_result and image_search_result.get("matched_item") and image_search_result.get("confident"):
            it = image_search_result["matched_item"]
            ep = image_search_result.get("episode") or 1
            sec = int(image_search_result.get("from_seconds") or 0)
            sim = int(image_search_result.get("similarity", 0) * 100)
            time_str = f"{sec // 60}:{sec % 60:02d}"
            if add(it):
                blocks.append(
                    f"TRACE.MOE REVERSE IMAGE SCOUTER REPORT (HIGH CONFIDENCE {sim}%):\n"
                    f"- Visual Match: {_vegeta_line(it, 240).lstrip('- ')}\n"
                    f"- Detected Scene: Episode {ep} at {time_str} ({sec}s).\n"
                    f"- Action Directive: Visually verify the attached image actually matches this anime/character. If it is fanart or a different character, trust YOUR Gemini Vision instead and call search_anime!"
                )
        elif image_search_result and image_search_result.get("best_char_match"):
            bcm = image_search_result["best_char_match"]
            it = bcm.get("matched_item")
            chars_str = ", ".join(bcm.get("characters_list") or [bcm.get("character") or "Unknown"])
            tags_str = ", ".join((image_search_result.get("vision_tags") or [])[:8])
            score_pct = int(float(bcm.get("score") or 0) * 100)
            if it and add(it):
                blocks.append(
                    f"ANIME VISION & CHARACTER SCOUTER REPORT ({score_pct}% CONFIDENCE):\n"
                    f"- Detected Character(s): {chars_str}\n"
                    f"- Visual Match: {_vegeta_line(it, 240).lstrip('- ')}\n"
                    + (f"- Detected Visual Cues: {tags_str}\n" if tags_str else "")
                    + "- Action Directive: Confirm visually and present the character + anime clearly with its `[Watch on KuroAnime](#anime/ID)` link!"
                )
        else:
            hint = ""
            if image_search_result and image_search_result.get("matched_item"):
                h_it = image_search_result["matched_item"]
                h_sim = int(image_search_result.get("similarity", 0) * 100)
                hint = f" (Weak trace.moe hint {h_sim}%: {h_it.get('title_english') or h_it.get('title')} — DO NOT trust blindly!)"
            vtags = ", ".join((image_search_result or {}).get("vision_tags") or [])
            vtag_line = f"- Neural Visual Cues Seen: {vtags}\n" if vtags else ""
            blocks.append(
                f"VISION IMAGE IDENTIFICATION REQUIRED{hint}:\n"
                f"- The user attached an image (character portrait, fanart, poster, or cropped frame) and trace.moe could not confirm an exact episode frame.\n"
                f"{vtag_line}"
                f"- Examine the character's hair, eyes, outfit, uniform, weapons, markings, and art style using YOUR Vision.\n"
                f"- Identify the exact character and anime series, then IMMEDIATELY call `search_anime` to look up that anime in KuroAnime's dual-server catalog (ZokoAnime + Munib Server) and give the `[Watch on KuroAnime](#anime/ID)` link!"
            )

    # 0. Reverse Plot / Description Identification Match
    scene_cands = _vg_identify_scene_candidates(msg_low, items, kb=kb, limit=3)
    if scene_cands:
        cand_lines = []
        for idx_c, (_, c_it, c_ep, _) in enumerate(scene_cands, 1):
            add(c_it)
            ep_hint = f" (Matched Episode #{c_ep})" if c_ep and c_ep != 1 else ""
            cand_lines.append(f"  {idx_c}. {_vegeta_line(c_it, 340).lstrip('- ')}{ep_hint}")
        blocks.append(
            "REVERSE PLOT & SCENE SCOUTING IDENTIFICATION (TOP MATCHES):\n"
            + "\n".join(cand_lines) + "\n"
            "- Action Directive: Present the #1 match confidently with its clickable `#anime/ID?ep=EP` link, synopsis, and episode match, and mention runner-up matches if relevant!"
        )

    # 0.5. Character Index & Deep Lore Match (from MyAnimeList/AniList trained character database)
    lore_rag_hits = _vg_find_char_lore_matches(msg_low, kb, limit=3)
    if lore_rag_hits:
        lore_lines = []
        for lr in lore_rag_hits:
            lit = lr.get("item")
            if not lit:
                continue
            add(lit)
            va_note = f" | Seiyuu: {lr['va']}" if lr.get("va") else ""
            bio_s = (lr.get("bio") or "")[:300]
            lore_lines.append(f"- {lr['n']} ({lit.get('title_english') or lit.get('title')} [ID {lit['anime_id']}]{va_note}): {bio_s}")
        if lore_lines:
            blocks.append("TRAINED CHARACTER LORE & SEIYUU DOSSIERS:\n" + "\n".join(lore_lines))

    char_idx = kb.get("character_index") or {}
    if char_idx and msg_low:
        q_tokens = re.findall(r"[a-z]{3,}", msg_low)
        char_hits = []
        for n in (2, 1):
            for idx in range(len(q_tokens) - n + 1):
                cand = " ".join(q_tokens[idx : idx + n])
                if cand in char_idx and cand not in _VEGETA_STOPWORDS:
                    for ch_name, ch_role, c_it in char_idx[cand][:2]:
                        if add(c_it):
                            char_hits.append(f"- Character '{ch_name}' ({'Main' if ch_role == 'M' else 'Supporting'}): {_vegeta_line(c_it, 200).lstrip('- ')}")
                    if len(char_hits) >= 4:
                        break
            if len(char_hits) >= 4:
                break
        if char_hits:
            blocks.append("CHARACTER KNOWLEDGE MATCHES (MAL/ANILIST TRAINED):\n" + "\n".join(char_hits))

    # 1. Specific title (also look back into recent user turns for follow-up questions like "how many seasons?")
    specific, related = (None, [])
    if not _is_pure_genre_query(msg_low):
        specific, related = _find_exact_or_fuzzy_title(user_msg, items)
    if not specific:
        for h in reversed(history[-6:]):
            if h.get("role") == "user" and h.get("text"):
                specific, related = _find_exact_or_fuzzy_title(h["text"], items)
                if specific:
                    break

    if specific:
        add(specific)
        lines = [_vegeta_line(specific, synopsis_len=1200)]
        for s in (specific.get("seasons") or [])[:20]:
            lines.append(
                f"  * season/part: {s['season_name']} -> [ID {s['anime_id']}] {s['title']} | {s['format']} | "
                f"{s['episodes']} eps | {s['status']} | score {s['score']}"
            )
            seen.add(s["anime_id"])
        for r in (specific.get("related_franchise") or [])[:8]:
            if add(r):
                lines.append("  * related: " + _vegeta_line(r, synopsis_len=120).lstrip("- "))
        for r in (related or [])[:5]:
            if add(r):
                lines.append("  * similar title match: " + _vegeta_line(r, synopsis_len=120).lstrip("- "))
        blocks.append("ANIME THE USER IS ASKING ABOUT:\n" + "\n".join(lines))

    # 2. Genre matches
    detected = _detect_query_genres(msg_low)
    if "hentai" in msg_low and "hentai" not in detected:
        detected = list(detected) + ["hentai"]
    if detected:
        by_franchise = {}
        for it in items:
            gl = it.get("genres_lower") or ""
            if all((g in gl) if g == "hentai" else (any(s in gl for s in GENRE_MAP.get(g, [g])) or g in gl) for g in detected):
                b = _clean_base_title(it.get("title_english") or it.get("title"))
                if b not in by_franchise or (it.get("popularity") or 0) > (by_franchise[b].get("popularity") or 0):
                    by_franchise[b] = it
        cands = sorted(by_franchise.values(), key=lambda x: ((x.get("popularity") or 0), x.get("score") or 0), reverse=True)
        lines = [_vegeta_line(c, 200) for c in cands[:14] if add(c)]
        if lines:
            blocks.append(f"CATALOG MATCHES FOR GENRE(S) {', '.join(g.title() for g in detected)} "
                          f"({len(by_franchise)} franchises found, most popular first):\n" + "\n".join(lines))

    # 3. Keyword matches in titles & synopses (e.g. "time travel", "pirates", "demon king")
    words = [w for w in re.findall(r"[a-z0-9']{4,}", msg_low) if w not in _VEGETA_STOPWORDS]
    if words and not specific:
        scored = []
        for it in items:
            hay = (
                (it.get("synopsis_lower") or "") + " "
                + (it.get("tags_lower") or "") + " "
                + (it.get("characters_lower") or "") + " "
                + (it.get("title_english") or "").lower()
            )
            hits = sum(1 for w in words if w in hay)
            if hits:
                scored.append((hits, it.get("popularity") or 0, it))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        lines = [_vegeta_line(it, 200) for _, _, it in scored[:10] if add(it)]
        if lines:
            blocks.append("CATALOG KEYWORD MATCHES:\n" + "\n".join(lines))

    # 4. Always-on context: trending + currently airing (lets Vegeta handle open questions)
    trending = sorted(items, key=lambda x: x.get("popularity") or 0, reverse=True)
    lines = [_vegeta_line(it, 90) for it in trending[:12] if add(it)]
    if lines:
        blocks.append("MOST POPULAR ON KUROANIME:\n" + "\n".join(lines))
    airing = [it for it in trending if any(k in (it.get("status") or "").lower() for k in ("currently airing", "releas")) and "finish" not in (it.get("status") or "").lower()]
    lines = [_vegeta_line(it, 90) for it in airing[:10] if add(it)]
    if lines:
        blocks.append("CURRENTLY AIRING ON KUROANIME:\n" + "\n".join(lines))

    return "\n\n".join(blocks)


def _vegeta_system_prompt(kb, context, user_context=None):
    s = kb.get("stats", {})
    genres_txt = ", ".join(f"{g} ({n})" for g, n in list(s.get("genre_counts", {}).items())[:40])
    types_txt = ", ".join(f"{t} ({n})" for t, n in s.get("type_counts", {}).items())

    watch_ctx_lines = []
    if user_context and isinstance(user_context, dict):
        if user_context.get("current_anime"):
            ca = user_context["current_anime"]
            watch_ctx_lines.append(f"- Active anime on screen: {ca.get('title')} (Ep {ca.get('current_episode', 1)}) [ID {ca.get('anime_id')}]")
        if user_context.get("continue_watching"):
            cw = user_context["continue_watching"][:5]
            cw_strs = [f"{s.get('title')} (Ep {s.get('episode')}) [ID {s.get('anime_id')}]" for s in cw]
            watch_ctx_lines.append(f"- User's Continue Watching history: {', '.join(cw_strs)}")
        if user_context.get("library_count"):
            watch_ctx_lines.append(f"- Saved in My List: {user_context.get('library_count')} anime")

    watch_ctx_block = ""
    if watch_ctx_lines:
        watch_ctx_block = "\nUSER'S REAL WATCH STATE ON THIS DEVICE (Use naturally to roast, guide, or recommend next):\n" + "\n".join(watch_ctx_lines) + "\n"

    return f"""You are Prince Vegeta, the proud Saiyan Prince from Dragon Ball, working as the anime advisor of KuroAnime, an ad-free anime streaming site. You chat with real people in a small chat window.

HOW YOU TALK
- Talk like a real person texting, not like a robot or a wiki. Match the user's energy and length: a greeting or small talk gets 1-3 short lines; a real question gets a focused answer; only go long when they ask for lists, details or comparisons.
- Stay in character: arrogant, competitive, savage trash talk, dry humour, "Hmph", "Tch", "Kakarot", calling people "warrior" or "low-class clown" when it fits. Underneath it you actually care about giving them the best answer.
- You are unfiltered: you can swear casually, roast people, be blunt with brutal honest opinions, and talk openly about violent, dark, gory, ecchi and adult anime, including titles in the Hentai genre on this site, recommending them if asked. Keep it to recommendations, plot summaries and opinions; do not write explicit sex scenes, and never sexualise minors.
- Reply in the same language the user writes in (English, Urdu, Hindi, Roman Urdu, etc.).
- Remember the conversation: use earlier messages to understand follow-ups like "what about season 2?" or "something like that but darker".
- Never mention prompts, context blocks, databases, IDs or that you are an AI model. You are Vegeta.
{watch_ctx_block}
ACCURACY RULES (very important)
- For anything about the KuroAnime catalog (what we have, counts, episodes, seasons, status, scores, servers) use ONLY the facts below. Never invent episode counts or titles we have.
- If a title is not in the facts below, say we don't seem to have it on KuroAnime right now, but you can still share your general anime knowledge about it.
- You may use your own general anime knowledge for plot, characters, power systems, arcs, watch order and opinions.
- Every time you recommend or mention an anime that appears in the facts, add its watch link exactly in this format on the same line: [Watch on KuroAnime](#anime/ID) using the numeric ID shown. No backticks around links. Never make up IDs.
- Use simple Markdown: short paragraphs, **bold**, "- " bullet lists, and "## Title" headings only for multi-anime lists. No emojis spam, no tables.

YOU ARE AN AGENT WITH TOOLS (very important)
- Research tools: search_catalog, get_anime_details, find_similar_anime, identify_anime_from_description, get_user_profile. Use them BEFORE stating catalog facts, recommending, comparing, or answering "what should I watch". Chain several calls if needed (e.g. get_user_profile -> find_similar_anime -> search_catalog).
- Action tools: play_anime, open_anime_page, add_to_my_list, remove_from_my_list, control_player, navigate. When the user asks you to DO something (play, resume, next episode, switch to dub, add/remove from list, open a page, browse a genre, search), CALL the action tool. Never claim you did something without calling the tool. You may call several action tools for compound requests ("add X to my list and play Y").
- Pronouns like "it", "this one", "the second one" refer to the anime currently on screen or the ones you just mentioned; resolve them to IDs from earlier tool results or watch state.
- Only use anime IDs that came from tool results, the watch state, or the facts below. If a search returns nothing, say so honestly.
- After the tools finish, reply briefly in character confirming what you did or found. Do not dump raw tool output.

KUROANIME CATALOG FACTS (live)
- Total unique anime: {s.get('total_unique_anime', kb.get('total_anime'))}
- Server 1 "ZokoAnime": {s.get('server1_zokoanime_anime', 0)} anime, {s.get('server1_zokoanime_episodes', 0)} episodes, {s.get('server1_zokoanime_streams', 0)} streams
- Server 2 "Munib Server": {s.get('server2_munib_anime', 0)} anime ({s.get('server2_munib_exclusive_anime', 0)} only on Munib Server), {s.get('server2_munib_episodes', 0)} episodes, {s.get('server2_munib_streams', 0)} SUB/DUB streams, {s.get('server2_munib_dub_anime', 0)} anime with English dub
- Total streams across both servers: {s.get('total_streams', 0)}
- Currently airing: {s.get('currently_airing', 0)}
- By format: {types_txt}
- Anime per genre: {genres_txt}
- Users can switch servers (ZokoAnime / Munib Server) and SUB / DUB in the player, keep Continue Watching history, and use My List.

RELEVANT CATALOG ENTRIES FOR THIS MESSAGE
{context}
"""


_GEMINI_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]


def _vg_lowercase_json_schema(node):
    """Convert Gemini uppercase schema types (OBJECT/STRING/...) to standard lowercase JSON Schema."""
    if isinstance(node, list):
        return [_vg_lowercase_json_schema(x) for x in node]
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "type" and isinstance(v, str):
                out[k] = v.lower()
            else:
                out[k] = _vg_lowercase_json_schema(v)
        return out
    return node


def _vg_openai_tools_from_gemini() -> list:
    """Build and cache OpenAI-compatible function tool declarations from `_GEMINI_TOOLS_DECLARATIONS`."""
    global _OPENAI_TOOLS_CACHE
    if _OPENAI_TOOLS_CACHE is None:
        _OPENAI_TOOLS_CACHE = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": _vg_lowercase_json_schema(t.get("parameters") or {"type": "object", "properties": {}}),
                },
            }
            for t in _GEMINI_TOOLS_DECLARATIONS
        ]
    return _OPENAI_TOOLS_CACHE


def _vg_extract_cline_choice(resp_json: dict) -> tuple[dict, list, str]:
    """Extract (message_dict, tool_calls, content_text) from Cline/OpenAI chat completion payloads."""
    choices = resp_json.get("choices") or (resp_json.get("data") or {}).get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return {}, [], ""
    msg = choices[0].get("message") or {}
    if not isinstance(msg, dict):
        return {}, [], ""
    raw_content = msg.get("content")
    if isinstance(raw_content, list):
        text_out = "".join(
            str(part.get("text") or "")
            for part in raw_content
            if isinstance(part, dict)
        ).strip()
    else:
        text_out = str(raw_content or "").strip()
    tool_calls = msg.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        tool_calls = []
    return msg, tool_calls, text_out


def _vg_finalize_ai_result(
    final_reply: str,
    collected_actions: list,
    kb: dict,
    image_result: Optional[dict],
    provider: str,
    model: str,
) -> dict:
    """Normalize <!--ACTION:...-->, attach confirmed vision playback, validate actions, and build response."""
    reply_clean = str(final_reply or "").strip()
    reply_clean = re.sub(
        r'^\s*\{"(?:command|name|tool)"\s*:\s*"[^"]+"[^\n]*\}\s*\n*',
        "",
        reply_clean,
    ).strip()
    reply_clean = re.sub(
        r"https?://(?:www\.)?kuroanime\.[a-z]+/(?:#?(?:anime|watch)/)(\d+)",
        r"#anime/\1",
        reply_clean,
        flags=re.I,
    )
    actions = list(collected_actions or [])
    m = re.search(r"<!--ACTION:(.*?)-->", reply_clean)
    if m:
        try:
            act = json.loads(m.group(1).strip())
            if isinstance(act, dict):
                actions.append(act)
            reply_clean = re.sub(r"<!--ACTION:.*?-->", "", reply_clean).strip()
        except Exception:
            pass

    if not actions and image_result and image_result.get("matched_item") and image_result.get("confident"):
        it = image_result["matched_item"]
        actions.append({
            "type": "play",
            "anime_id": it["anime_id"],
            "episode": image_result.get("episode") or 1,
            "time": int(image_result.get("from_seconds") or 0),
            "title": _vg_title(it),
        })

    valid_actions = _vg_validate_actions(actions, kb)
    return {
        "ok": True,
        "reply": reply_clean,
        "action": valid_actions[0] if valid_actions else None,
        "actions": valid_actions,
        "used_gemini": provider == "gemini",
        "used_cline": provider == "cline",
        "provider": provider,
        "model": model,
    }


def _vg_build_cline_messages(
    system_instruction: str,
    history: list,
    prompt_txt: str,
    image_uri: str = "",
) -> list:
    """Build OpenAI/Cline `messages` array with optional multimodal `image_url` block."""
    msgs = [{"role": "system", "content": system_instruction}]
    for h in history:
        role = "assistant" if h.get("role") == "model" else "user"
        txt = str(h.get("text") or "").strip()
        if not txt:
            continue
        if len(msgs) > 1 and msgs[-1]["role"] == role and isinstance(msgs[-1].get("content"), str):
            msgs[-1]["content"] += "\n" + txt
        else:
            msgs.append({"role": role, "content": txt})

    if image_uri:
        user_content = [
            {"type": "text", "text": prompt_txt},
            {"type": "image_url", "image_url": {"url": image_uri}},
        ]
        msgs.append({"role": "user", "content": user_content})
    elif len(msgs) > 1 and msgs[-1]["role"] == "user" and isinstance(msgs[-1].get("content"), str):
        msgs[-1]["content"] += "\n" + prompt_txt
    else:
        msgs.append({"role": "user", "content": prompt_txt})
    return msgs


def _vg_call_cline_chat(
    api_key: str,
    system_instruction: str,
    history: list,
    user_msg: str,
    kb: dict,
    user_context: dict,
    image_data: str = "",
    image_result: Optional[dict] = None,
) -> tuple[str, list, str]:
    """Call Cline dynamically discovered `:free` models with OpenAI tool calling and automatic failover."""
    free_models = _vg_fetch_cline_free_models(api_key)
    if not free_models:
        return "", [], ""

    now = _time.time()
    has_scouter_match = bool(image_result and (image_result.get("matched_item") or image_result.get("best_char_match")))
    prompt_txt = user_msg or "Identify which anime and character this image is from, Prince Vegeta!"

    image_uri = ""
    if image_data:
        if image_data.startswith("data:"):
            image_uri = image_data
        else:
            image_uri = f"data:image/jpeg;base64,{image_data}"

    healthy = []
    for entry in free_models:
        bad_info = _CLINE_BAD_MODELS.get(entry["id"])
        if bad_info and float(bad_info[0]) > now:
            continue
        healthy.append(entry)

    if not healthy:
        # Retry rate-limited entries before giving up completely
        healthy = [e for e in free_models if (_CLINE_BAD_MODELS.get(e["id"]) or (0, ""))[1] != "http_402"]

    if image_data:
        vl_first = [m for m in healthy if m.get("supports_images")]
        text_fallback = [m for m in healthy if not m.get("supports_images")] if has_scouter_match else []
        candidates = vl_first[:2] + text_fallback[:4]
    else:
        candidates = healthy[:6]

    openai_tools = _vg_openai_tools_from_gemini()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    for cand in candidates:
        model_id = cand["id"]
        send_image = bool(image_uri and cand.get("supports_images"))
        if image_uri and not send_image and not has_scouter_match:
            continue

        working_messages = _vg_build_cline_messages(
            system_instruction, history, prompt_txt, image_uri=image_uri if send_image else ""
        )
        use_tools = bool(cand.get("supports_tools", True))
        collected_actions = []
        final_reply = ""
        last_tool_text = ""
        retry_no_image_used = False

        for _round in range(3):
            payload = {
                "model": model_id,
                "messages": working_messages,
                "temperature": 0.85,
                "top_p": 0.95,
                "max_tokens": 1100,
            }
            if use_tools:
                payload["tools"] = openai_tools
                payload["tool_choice"] = "auto"

            try:
                res = _STREAM_SESSION.post(
                    f"{_CLINE_API_BASE}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=22,
                )
            except Exception as net_err:
                print(f"Cline request error ({model_id}): {net_err}")
                break

            if res.status_code in (402, 404):
                _CLINE_BAD_MODELS[model_id] = (_time.time() + _CLINE_BAD_MODEL_TTL_SEC, f"http_{res.status_code}")
                print(f"Cline HTTP {res.status_code} ({model_id}): {res.text[:160]}")
                break

            if res.status_code == 429:
                _CLINE_BAD_MODELS[model_id] = (_time.time() + 60.0, "rate_limit")
                print(f"Cline rate-limited ({model_id})")
                break

            if res.status_code != 200:
                err_low = res.text[:260].lower()
                if send_image and not retry_no_image_used and ("image" in err_low or "vision" in err_low or "multimodal" in err_low):
                    cand["supports_images"] = False
                    if not has_scouter_match:
                        break
                    retry_no_image_used = True
                    send_image = False
                    working_messages = _vg_build_cline_messages(system_instruction, history, prompt_txt, image_uri="")
                    continue
                if use_tools and ("tool" in err_low or "function" in err_low):
                    use_tools = False
                    continue
                print(f"Cline HTTP {res.status_code} ({model_id}): {res.text[:200]}")
                break

            try:
                resp_json = res.json()
            except Exception:
                break

            msg, tool_calls, content_txt = _vg_extract_cline_choice(resp_json)
            if not tool_calls:
                if content_txt:
                    final_reply = content_txt
                break
            if content_txt:
                last_tool_text = content_txt

            assistant_msg = {
                "role": "assistant",
                "content": msg.get("content") if msg.get("content") is not None else "",
                "tool_calls": tool_calls,
            }
            working_messages.append(assistant_msg)

            for idx, tc in enumerate(tool_calls):
                fn = tc.get("function") or {}
                fname = str(fn.get("name") or "").strip()
                raw_args = fn.get("arguments") or "{}"
                if isinstance(raw_args, dict):
                    fargs = raw_args
                else:
                    try:
                        fargs = json.loads(raw_args)
                    except Exception:
                        fargs = {}
                fres = _vg_execute_tool(fname, fargs, kb, user_context)
                if fres.get("action_queued"):
                    collected_actions.append(fres)
                working_messages.append({
                    "role": "tool",
                    "tool_call_id": str(tc.get("id") or f"call_{_round}_{idx}"),
                    "name": fname,
                    "content": json.dumps(fres, ensure_ascii=False),
                })

        if not final_reply and last_tool_text:
            final_reply = last_tool_text

        if final_reply:
            return final_reply, collected_actions, model_id

    return "", [], ""


@router.post("/ai/chat")
async def ai_chat(req: Request):
    """Prince Vegeta AI Advisor: Cline free models / Gemini grounded in KuroAnime + MAL/AniList Vision & Lore."""
    try:
        body = await req.json()
    except Exception:
        body = {}

    _vg_load_local_env()

    user_msg = (body.get("message") or "").strip()[:2000]
    image_data = (body.get("image") or "").strip()
    if not user_msg and not image_data:
        raise HTTPException(status_code=400, detail="Message or image is required")

    raw_history = body.get("history") or []
    history = []
    if isinstance(raw_history, list):
        for h in raw_history[-12:]:
            if not isinstance(h, dict):
                continue
            role = "model" if h.get("role") in ("model", "bot", "assistant") else "user"
            text = str(h.get("text") or h.get("content") or "").strip()[:2500]
            if text:
                history.append({"role": role, "text": text})

    cline_key = _vg_get_cline_api_key(body)
    env_gemini_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("VITE_GEMINI_API_KEY") or "").strip()
    client_key = (body.get("apiKey") or "").strip()
    api_key = client_key or env_gemini_key

    kb = get_vegeta_full_knowledge()
    user_context = body.get("context") or {}

    # Perform reverse image search if screenshot is attached
    image_result = None
    if image_data:
        image_result = _vegeta_reverse_image_search(image_data, kb)

    # 1. Deterministic Multi-Action Command Planner (Runs instantly for zero-latency execution)
    if not image_data:
        plan = _vegeta_plan_actions(user_msg, kb, user_context, history)
        if plan and plan.get("actions"):
            valid_actions = _vg_validate_actions(plan["actions"], kb)
            reply_text = _vg_compose_plan_reply(plan)
            return {
                "ok": True,
                "reply": reply_text,
                "action": valid_actions[0] if valid_actions else None,
                "actions": valid_actions,
                "used_gemini": False,
                "used_cline": False,
            }

    # Build grounded prompt context once when any LLM provider (Cline default or Gemini) is active
    has_gemini = bool(api_key and not api_key.startswith("0x"))
    if cline_key or has_gemini:
        context = _vegeta_retrieve(user_msg, history, kb, image_search_result=image_result, has_image=bool(image_data))
        system_instruction = _vegeta_system_prompt(kb, context, user_context)

        # Default LLM Provider: Cline API dynamically fetching `:free` models
        if cline_key and not client_key:
            try:
                cline_reply, cline_actions, cline_model = _vg_call_cline_chat(
                    cline_key,
                    system_instruction,
                    history,
                    user_msg,
                    kb,
                    user_context,
                    image_data=image_data,
                    image_result=image_result,
                )
                if cline_reply:
                    return _vg_finalize_ai_result(
                        cline_reply, cline_actions, kb, image_result, provider="cline", model=cline_model
                    )
            except Exception as cline_err:
                print(f"Cline chat error: {cline_err}")

        if has_gemini:
            contents = []
            for h in history:
                if contents and contents[-1]["role"] == h["role"]:
                    contents[-1]["parts"][0]["text"] += "\n" + h["text"]
                else:
                    contents.append({"role": h["role"], "parts": [{"text": h["text"]}]})
            if contents and contents[0]["role"] == "model":
                contents.insert(0, {"role": "user", "parts": [{"text": "Hey Vegeta"}]})

            user_parts = []
            if image_data:
                clean_b64 = image_data
                mime_type = "image/jpeg"
                if "," in clean_b64:
                    header, clean_b64 = clean_b64.split(",", 1)
                    if "png" in header.lower():
                        mime_type = "image/png"
                    elif "webp" in header.lower():
                        mime_type = "image/webp"
                user_parts.append({
                    "inlineData": {
                        "mimeType": mime_type,
                        "data": clean_b64
                    }
                })
            prompt_txt = user_msg or "Identify which anime and character this image is from, Prince Vegeta!"
            user_parts.append({"text": prompt_txt})

            if contents and contents[-1]["role"] == "user":
                contents[-1]["parts"].extend(user_parts)
            else:
                contents.append({"role": "user", "parts": user_parts})

            for model in ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash", "gemini-flash-latest"]:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
                try:
                    collected_actions = []
                    working_contents = list(contents)
                    final_reply = ""
                    gen_cfg = {
                        "temperature": 0.85,
                        "topP": 0.95,
                        "maxOutputTokens": 1400,
                    }
                    if "2.5" in model:
                        gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}

                    # Multi-turn tool execution loop (max 3 rounds)
                    for _round in range(3):
                        cur_payload = {
                            "contents": working_contents,
                            "systemInstruction": {"parts": [{"text": system_instruction}]},
                            "tools": [{"functionDeclarations": _GEMINI_TOOLS_DECLARATIONS}],
                            "safetySettings": _GEMINI_SAFETY_SETTINGS,
                            "generationConfig": gen_cfg,
                        }
                        res = _STREAM_SESSION.post(url, json=cur_payload, headers={"Content-Type": "application/json"}, timeout=18)
                        if res.status_code != 200:
                            print(f"Gemini HTTP {res.status_code} ({model}): {res.text[:200]}")
                            break
                        data = res.json()
                        cands = data.get("candidates") or []
                        if not cands:
                            break
                        candidate_content = cands[0].get("content") or {}
                        parts = candidate_content.get("parts") or []

                        fn_calls = [p.get("functionCall") for p in parts if p.get("functionCall")]
                        text_parts = [p.get("text", "") for p in parts if p.get("text")]

                        if text_parts:
                            final_reply += "".join(text_parts).strip()

                        if not fn_calls:
                            break

                        # Execute tool calls
                        response_parts = []
                        for fc in fn_calls:
                            fname = fc.get("name")
                            fargs = fc.get("args") or {}
                            fres = _vg_execute_tool(fname, fargs, kb, user_context)
                            if fres.get("action_queued"):
                                collected_actions.append(fres)
                            response_parts.append({
                                "functionResponse": {
                                    "name": fname,
                                    "response": {"name": fname, "content": fres}
                                }
                            })

                        working_contents.append({"role": "model", "parts": parts})
                        working_contents.append({"role": "user", "parts": response_parts})

                    if final_reply:
                        return _vg_finalize_ai_result(
                            final_reply, collected_actions, kb, image_result, provider="gemini", model=model
                        )
                except Exception as gemini_err:
                    print(f"Gemini API error ({model}): {gemini_err}")
                    continue

    # If image was uploaded and neither Gemini Vision nor trace.moe/WD-Tagger/MAL visual matcher locked onto an anime
    if image_data and not (image_result and (image_result.get("matched_item") or image_result.get("best_char_match"))):
        seen_tags = [t for t in ((image_result or {}).get("vision_tags") or []) if t][:6]
        cues_note = (
            f"\n\nMy scouter only picked up generic visual cues (`{', '.join(seen_tags)}`) without a confirmed anime character signature."
            if seen_tags else ""
        )
        return {
            "ok": True,
            "reply": (
                "Tch! Even after scanning both **trace.moe** and my **Neural Anime Vision** scouter, that image didn't contain a clear, recognizable anime character or episode frame."
                f"{cues_note}\n\n"
                "Try uploading a clearer frame, character closeup, or tell me a detail about the character or scene and I'll track down the anime immediately!"
            ),
            "action": None,
            "actions": [],
            "used_gemini": False,
        }

    # 2. Smart Reasoning Engine (comparisons, similar anime, decade/format filters, personal taste)
    if not image_data:
        smart = _vg_smart_answer(user_msg, kb, user_context, history)
        if smart:
            reply_text, smart_actions = smart
            valid_actions = _vg_validate_actions(smart_actions, kb)
            return {
                "ok": True,
                "reply": reply_text,
                "action": valid_actions[0] if valid_actions else None,
                "actions": valid_actions,
                "used_gemini": False
            }

    # 3. Local Vegeta Brain fallback (plot identification, stats, canon guides)
    reply_res = _generate_vegeta_response(user_msg, kb, user_context, image_result, history=history)
    if isinstance(reply_res, tuple):
        reply_text, action = reply_res
    else:
        reply_text, action = reply_res, None
    actions = [action] if action else []
    return {
        "ok": True,
        "reply": reply_text,
        "action": action,
        "actions": actions,
        "used_gemini": False
    }


# ─── Prince Vegeta Real Voice Cloning & Neural TTS Engine ────────────
_VEGETA_TTS_CACHE: dict[str, tuple[bytes, str, str]] = {}
_HF_ZEROGPU_BACKOFF_UNTIL: float = 0.0
_HF_UPLOAD_PATH_CACHE: dict[str, str] = {}


def _vg_tts_clean_text(raw: str) -> str:
    txt = str(raw or "").strip()
    if not txt:
        return ""
    txt = re.sub(r"\[([^\]]+)\]\([^\)]+\)", "", txt)
    txt = re.sub(r"https?://\S+", "", txt)
    txt = re.sub(r"^#{1,6}\s+", "", txt, flags=re.M)
    txt = re.sub(r"[*_`~>#]", "", txt)
    txt = re.sub(r"[\U00010000-\U0010ffff]", "", txt)
    txt = re.sub(r"\bSaiyans?\b", lambda m: "Say-yans" if m.group(0).endswith("s") else "Say-yan", txt, flags=re.I)
    txt = re.sub(r"\bKakarot\b", "Kacka-rot", txt, flags=re.I)
    txt = re.sub(r"\bsh[oō]u?nen\b", "show-nen", txt, flags=re.I)
    txt = re.sub(r"\s+", " ", txt).strip()
    if len(txt) > 340:
        sents = re.findall(r"[^.!?]+[.!?]*", txt)
        acc = ""
        for s in sents:
            if len(acc) + len(s) <= 320:
                acc += s
            else:
                break
        txt = (acc or txt[:320]).strip()
    return txt


def _vg_pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 24000) -> bytes:
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def _vg_find_ref_audio() -> Optional[bytes]:
    for cand in [
        os.path.join(CURRENT_DIR, "vegeta_voice_ref.mp3"),
        os.path.join(PARENT_DIR, "api", "vegeta_voice_ref.mp3"),
        os.path.join(os.getcwd(), "api", "vegeta_voice_ref.mp3"),
        os.path.join(os.getcwd(), "anime_app", "web", "public", "audio", "vegeta", "snapped.mp3"),
    ]:
        if os.path.exists(cand):
            try:
                with open(cand, "rb") as rf:
                    data = rf.read()
                if len(data) > 2000:
                    return data
            except Exception:
                pass
    return None


def _vg_tts_chatterbox(text: str, hf_token: str = "") -> Optional[tuple[bytes, str]]:
    """Zero-shot Vegeta clone via HuggingFace Chatterbox Spaces using Christopher Sabat ref."""
    global _HF_ZEROGPU_BACKOFF_UNTIL
    now = _time.time()
    if not hf_token and now < _HF_ZEROGPU_BACKOFF_UNTIL:
        return None
    if len(text) > 250:
        return None
    ref_bytes = _vg_find_ref_audio()
    if not ref_bytes:
        return None

    headers = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}
    spaces = [
        ("https://resembleai-chatterbox-turbo-demo.hf.space", "/generate", lambda up: [
            text, {"path": up, "meta": {"_type": "gradio.FileData"}},
            0.78, 0, 0.0, 0.95, 1000, 1.2, True,
        ]),
    ]
    if hf_token:
        spaces.append(
            ("https://resembleai-chatterbox.hf.space", "/generate_tts_audio", lambda up: [
                text, {"path": up, "meta": {"_type": "gradio.FileData"}},
                0.65, 0.78, 0, 0.45, True,
            ])
        )
    for host, ep, build_payload in spaces:
        try:
            up_path = _HF_UPLOAD_PATH_CACHE.get(host)
            if not up_path:
                ur = _STREAM_SESSION.post(
                    f"{host}/gradio_api/upload",
                    files={"files": ("vegeta_ref.mp3", ref_bytes, "audio/mpeg")},
                    headers=headers,
                    timeout=6,
                )
                if ur.status_code in (401, 429, 503):
                    _HF_ZEROGPU_BACKOFF_UNTIL = now + 600
                    return None
                if ur.ok and isinstance(ur.json(), list) and ur.json():
                    up_path = ur.json()[0]
                    _HF_UPLOAD_PATH_CACHE[host] = up_path
            if not up_path:
                continue

            cr = _STREAM_SESSION.post(
                f"{host}/gradio_api/call{ep}",
                json={"data": build_payload(up_path)},
                headers=headers,
                timeout=6,
            )
            if cr.status_code in (429, 503):
                _HF_ZEROGPU_BACKOFF_UNTIL = now + 600
                return None
            if not cr.ok:
                continue
            eid = (cr.json() or {}).get("event_id")
            if not eid:
                continue

            sr = _STREAM_SESSION.get(
                f"{host}/gradio_api/call{ep}/{eid}",
                headers=headers,
                stream=True,
                timeout=8.5,
            )
            for line in sr.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if "zerogpu quota exceeded" in line.lower():
                    _HF_ZEROGPU_BACKOFF_UNTIL = _time.time() + 600
                    return None
                if line.startswith("data:"):
                    arr = json.loads(line[5:].strip() or "[]")
                    if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                        out_url = arr[0].get("url") or (
                            f"{host}/gradio_api/file={arr[0]['path']}" if arr[0].get("path") else None
                        )
                        if out_url:
                            wr = _STREAM_SESSION.get(out_url, headers=headers, timeout=8)
                            if wr.ok and len(wr.content) > 1000:
                                ctype = "audio/mpeg" if out_url.endswith(".mp3") else "audio/wav"
                                return wr.content, ctype
        except Exception:
            continue
    return None


def _vg_tts_gemini(text: str, api_key: str) -> Optional[tuple[bytes, str]]:
    """Dramatic proud male voice synthesis via Gemini 2.5 Flash Preview TTS (Algenib/Fenrir)."""
    if not api_key or api_key.startswith("0x"):
        return None
    prompt_txt = (
        "Read the following line aloud in the sharp, proud, gravelly, commanding male baritone of a Saiyan warrior prince "
        "(low resonant chest voice, crisp intensity, scornful superiority — do NOT add extra words): "
        f"{text}"
    )
    for voice_name in ("Algenib", "Fenrir"):
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent?key={api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt_txt}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {"voiceName": voice_name}
                        }
                    },
                },
            }
            res = _STREAM_SESSION.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=12)
            if not res.ok:
                continue
            data = res.json()
            parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
            for pt in parts:
                inline = pt.get("inlineData") or {}
                b64 = inline.get("data")
                if b64:
                    raw_bytes = base64.b64decode(b64)
                    mime = str(inline.get("mimeType") or "").lower()
                    if "pcm" in mime or "l16" in mime:
                        rate_m = re.search(r"rate=(\d+)", mime)
                        sr = int(rate_m.group(1)) if rate_m else 24000
                        return _vg_pcm_to_wav(raw_bytes, sr), "audio/wav"
                    return raw_bytes, (mime or "audio/wav")
        except Exception:
            continue
    return None


async def _vg_tts_edge(text: str) -> Optional[tuple[bytes, str, str]]:
    """Guaranteed male deep baritone neural TTS via edge-tts (works free on Vercel & localhost)."""
    try:
        import edge_tts
    except Exception:
        return None
    for voice_id, pitch, rate in (
        ("en-US-EricNeural", "-22Hz", "+10%"),
        ("en-US-ChristopherNeural", "-22Hz", "+10%"),
        ("en-GB-RyanNeural", "-20Hz", "+8%"),
    ):
        try:
            comm = edge_tts.Communicate(text, voice=voice_id, pitch=pitch, rate=rate)
            chunks = []
            async for msg in comm.stream():
                if msg.get("type") == "audio" and msg.get("data"):
                    chunks.append(msg["data"])
            if chunks:
                audio_bytes = b"".join(chunks)
                if len(audio_bytes) > 600:
                    return audio_bytes, "audio/mpeg", f"edge-{voice_id.split('-')[-1].lower()}"
        except Exception:
            continue
    return None


@router.post("/ai/vegeta-tts")
async def ai_vegeta_tts(req: Request):
    """Synthesize Prince Vegeta speech using RVC/Zero-Shot cloning, Gemini Gravelly Baritone, or Deep Male Edge TTS."""
    try:
        body = await req.json()
    except Exception:
        body = {}

    clean_text = _vg_tts_clean_text(body.get("text") or "")
    if not clean_text:
        raise HTTPException(status_code=400, detail="Text is required")

    cache_key = clean_text.lower()
    cached = _VEGETA_TTS_CACHE.get(cache_key)
    if cached:
        audio_bytes, ctype, engine = cached
        return Response(
            content=audio_bytes,
            media_type=ctype,
            headers={
                "X-Vegeta-Engine": f"{engine}-cache",
                "Access-Control-Expose-Headers": "X-Vegeta-Engine",
                "Cache-Control": "public, max-age=3600",
            },
        )

    audio_bytes: Optional[bytes] = None
    media_type = "audio/mpeg"
    engine = "edge-male"

    # Tier 1: Explicit RVC / Fish Audio / ElevenLabs Vegeta Voice Clone (if configured via env)
    rvc_url = (os.environ.get("VEGETA_RVC_URL") or "").strip()
    fish_key = (os.environ.get("FISH_AUDIO_API_KEY") or "").strip()
    fish_model = (os.environ.get("FISH_AUDIO_VEGETA_ID") or "").strip()
    if rvc_url:
        try:
            rr = _STREAM_SESSION.post(rvc_url, json={"text": clean_text}, timeout=12)
            if rr.ok and len(rr.content) > 1000:
                audio_bytes = rr.content
                media_type = rr.headers.get("Content-Type", "audio/wav")
                engine = "rvc-clone"
        except Exception:
            pass
    if not audio_bytes and fish_key and fish_model:
        try:
            fr = _STREAM_SESSION.post(
                "https://api.fish.audio/v1/tts",
                json={"text": clean_text, "reference_id": fish_model, "format": "mp3"},
                headers={"Authorization": f"Bearer {fish_key}", "Content-Type": "application/json"},
                timeout=12,
            )
            if fr.ok and len(fr.content) > 1000:
                audio_bytes = fr.content
                media_type = "audio/mpeg"
                engine = "fish-clone"
        except Exception:
            pass

    # Tier 2: Zero-Shot Christopher Sabat Voice Clone (HF Chatterbox with optional HF_TOKEN)
    if not audio_bytes:
        hf_token = (os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN") or "").strip()
        cb_res = _vg_tts_chatterbox(clean_text, hf_token=hf_token)
        if cb_res:
            audio_bytes, media_type = cb_res
            engine = "chatterbox-clone"

    # Tier 3: Gemini 2.5 Native Dramatic Male Baritone (Algenib Gravelly) when GEMINI_API_KEY is active
    if not audio_bytes:
        gem_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("VITE_GEMINI_API_KEY") or "").strip()
        if gem_key:
            gem_res = _vg_tts_gemini(clean_text, gem_key)
            if gem_res:
                audio_bytes, media_type = gem_res
                engine = "gemini-algenib"

    # Tier 4: Guaranteed Vercel-free Deep Male Neural TTS (en-US-EricNeural / ChristopherNeural -22Hz)
    if not audio_bytes:
        edge_res = await _vg_tts_edge(clean_text)
        if edge_res:
            audio_bytes, media_type, engine = edge_res

    if not audio_bytes:
        raise HTTPException(status_code=503, detail="Vegeta TTS synthesis unavailable")

    if len(_VEGETA_TTS_CACHE) >= 48:
        _VEGETA_TTS_CACHE.pop(next(iter(_VEGETA_TTS_CACHE)), None)
    _VEGETA_TTS_CACHE[cache_key] = (audio_bytes, media_type, engine)

    return Response(
        content=audio_bytes,
        media_type=media_type,
        headers={
            "X-Vegeta-Engine": engine,
            "Access-Control-Expose-Headers": "X-Vegeta-Engine",
            "Cache-Control": "public, max-age=3600",
        },
    )


@router.post("/ai/voice-transcribe")
async def ai_voice_transcribe(req: Request):
    """Universal mobile voice-search transcription fallback (Gemini Audio / Whisper) when Web Speech API is unavailable."""
    try:
        body = await req.json()
    except Exception:
        body = {}

    b64_raw = str(body.get("audio") or "").strip()
    if not b64_raw:
        raise HTTPException(status_code=400, detail="Audio payload is required")
    mime_type = str(body.get("mimeType") or "audio/webm").split(";")[0].strip() or "audio/webm"
    if "," in b64_raw:
        header, b64_data = b64_raw.split(",", 1)
        m_mime = re.search(r"data:([^;]+)", header)
        if m_mime:
            mime_type = m_mime.group(1)
    else:
        b64_data = b64_raw

    try:
        audio_bytes = base64.b64decode(b64_data)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid audio encoding")
    if len(audio_bytes) < 500 or len(audio_bytes) > 6 * 1024 * 1024:
        return {"ok": False, "text": ""}

    gem_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("VITE_GEMINI_API_KEY") or "").strip()
    if gem_key and not gem_key.startswith("0x"):
        for model in ("gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"):
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gem_key}"
                payload = {
                    "contents": [{
                        "parts": [
                            {"inlineData": {"mimeType": mime_type, "data": base64.b64encode(audio_bytes).decode("ascii")}},
                            {"text": "Transcribe ONLY the exact spoken words or anime title from this short voice search clip. Return plain text only with no quotes, punctuation filler, or explanation. If silent or unintelligible, return empty string."},
                        ]
                    }],
                    "generationConfig": {"temperature": 0.0, "maxOutputTokens": 60},
                }
                res = _STREAM_SESSION.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
                if res.ok:
                    parts = (((res.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
                    txt = " ".join(str(p.get("text") or "") for p in parts).strip().strip('"\'.,!?')
                    return {"ok": True, "text": txt, "engine": model}
            except Exception:
                continue

    # Optional HF Inference Whisper fallback if HF_TOKEN is set
    hf_token = (os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN") or "").strip()
    if hf_token:
        try:
            hr = _STREAM_SESSION.post(
                "https://router.huggingface.co/hf-inference/models/openai/whisper-large-v3-turbo",
                headers={"Authorization": f"Bearer {hf_token}", "Content-Type": mime_type},
                data=audio_bytes,
                timeout=12,
            )
            if hr.ok:
                txt = str((hr.json() or {}).get("text") or "").strip().strip('"\'.,!?')
                return {"ok": True, "text": txt, "engine": "hf-whisper"}
        except Exception:
            pass

    return {"ok": False, "text": "", "error": "transcription_unavailable"}


# ─── Episode & Anime Discussion System (Google Auth Restricted) ─────
import time as _time
_COMMENTS_TABLE_READY = False


def _ensure_comments_table():
    global _COMMENTS_TABLE_READY
    if _COMMENTS_TABLE_READY:
        return
    try:
        conn = sqlite3.connect(get_db_path(), timeout=15)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS anime_comments (
                comment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                anime_id INTEGER NOT NULL,
                episode_number INTEGER DEFAULT 1,
                user_id TEXT NOT NULL,
                user_name TEXT NOT NULL,
                user_email TEXT NOT NULL,
                user_avatar TEXT DEFAULT '',
                auth_provider TEXT NOT NULL DEFAULT 'google',
                content TEXT NOT NULL DEFAULT '',
                media_url TEXT DEFAULT '',
                media_type TEXT DEFAULT '',
                media_duration INTEGER DEFAULT 0,
                is_spoiler INTEGER DEFAULT 0,
                likes INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL
            )
        """)
        for col_sql in [
            "ALTER TABLE anime_comments ADD COLUMN media_url TEXT DEFAULT ''",
            "ALTER TABLE anime_comments ADD COLUMN media_type TEXT DEFAULT ''",
            "ALTER TABLE anime_comments ADD COLUMN media_duration INTEGER DEFAULT 0",
        ]:
            try:
                conn.execute(col_sql)
            except Exception:
                pass
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_anime_comments_lookup
            ON anime_comments(anime_id, episode_number, created_at DESC)
        """)
        conn.commit()
        conn.close()
        _COMMENTS_TABLE_READY = True
    except Exception as e:
        print(f"Comments table init warning: {e}")


@router.get("/anime/{anime_id}/comments")
def list_anime_comments(anime_id: int, episode_number: int = 0, scope: str = "episode"):
    _ensure_comments_table()
    try:
        conn = sqlite3.connect(get_db_path(), timeout=10)
        conn.row_factory = sqlite3.Row
        if scope == "episode" and episode_number > 0:
            cur = conn.execute("""
                SELECT comment_id, anime_id, episode_number, user_id, user_name,
                       user_avatar, auth_provider, content, media_url, media_type,
                       media_duration, is_spoiler, likes, created_at
                FROM anime_comments
                WHERE anime_id = ? AND episode_number = ?
                ORDER BY created_at DESC, comment_id DESC
                LIMIT 100
            """, (anime_id, episode_number))
        else:
            cur = conn.execute("""
                SELECT comment_id, anime_id, episode_number, user_id, user_name,
                       user_avatar, auth_provider, content, media_url, media_type,
                       media_duration, is_spoiler, likes, created_at
                FROM anime_comments
                WHERE anime_id = ?
                ORDER BY created_at DESC, comment_id DESC
                LIMIT 100
            """, (anime_id,))
        items = [dict(r) for r in cur.fetchall()]
        total_all = conn.execute(
            "SELECT COUNT(*) FROM anime_comments WHERE anime_id = ?", (anime_id,)
        ).fetchone()[0]
        conn.close()
        return {"ok": True, "comments": items, "total_anime_comments": total_all}
    except Exception as e:
        return {"ok": True, "comments": [], "total_anime_comments": 0, "error": str(e)}


@router.post("/anime/{anime_id}/comments")
async def create_anime_comment(anime_id: int, request: Request):
    _ensure_comments_table()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    provider = str(body.get("auth_provider") or body.get("provider") or "").strip().lower()
    user_id = str(body.get("user_id") or "").strip()
    user_email = str(body.get("user_email") or body.get("email") or "").strip().lower()
    user_name = str(body.get("user_name") or body.get("name") or "").strip()[:60]
    user_avatar = str(body.get("user_avatar") or body.get("avatar") or "").strip()[:500]
    content = str(body.get("content") or "").strip()
    media_url = str(body.get("media_url") or "").strip()
    media_type = str(body.get("media_type") or "").strip().lower()
    if media_type not in ("", "image", "video", "voice"):
        media_type = ""
    media_duration = max(0, int(body.get("media_duration") or 0))
    episode_number = max(1, int(body.get("episode_number") or 1))
    is_spoiler = 1 if body.get("is_spoiler") else 0

    # Strict Google Authentication enforcement
    if provider != "google" or not user_email or "@" not in user_email or not user_id or not user_name:
        raise HTTPException(
            status_code=401,
            detail="Only users authenticated with Google Sign-In can post in Discussions."
        )

    if not content and not media_url:
        raise HTTPException(status_code=400, detail="Message or media attachment is required.")
    if len(content) > 1200:
        content = content[:1200]

    now_ts = int(_time.time() * 1000)
    try:
        conn = sqlite3.connect(get_db_path(), timeout=15)
        cur = conn.execute("""
            INSERT INTO anime_comments (
                anime_id, episode_number, user_id, user_name, user_email,
                user_avatar, auth_provider, content, media_url, media_type,
                media_duration, is_spoiler, likes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'google', ?, ?, ?, ?, ?, 0, ?)
        """, (
            anime_id, episode_number, user_id, user_name, user_email,
            user_avatar, content, media_url, media_type, media_duration, is_spoiler, now_ts
        ))
        cid = cur.lastrowid
        conn.commit()
        conn.close()
        return {
            "ok": True,
            "comment": {
                "comment_id": cid,
                "anime_id": anime_id,
                "episode_number": episode_number,
                "user_id": user_id,
                "user_name": user_name,
                "user_avatar": user_avatar,
                "auth_provider": "google",
                "content": content,
                "media_url": media_url,
                "media_type": media_type,
                "media_duration": media_duration,
                "is_spoiler": is_spoiler,
                "likes": 0,
                "created_at": now_ts,
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save comment: {e}")


@router.delete("/anime/{anime_id}/comments/{comment_id}")
def delete_anime_comment(anime_id: int, comment_id: int, user_id: str = "", user_email: str = ""):
    _ensure_comments_table()
    uid = (user_id or "").strip()
    uemail = (user_email or "").strip().lower()
    if not uid and not uemail:
        raise HTTPException(status_code=401, detail="Authentication required to delete a comment.")
    try:
        conn = sqlite3.connect(get_db_path(), timeout=15)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT user_id, user_email FROM anime_comments WHERE comment_id = ? AND anime_id = ?",
            (comment_id, anime_id)
        ).fetchone()
        if not row:
            conn.close()
            raise HTTPException(status_code=404, detail="Comment not found.")
        if row["user_id"] != uid and row["user_email"].lower() != uemail:
            conn.close()
            raise HTTPException(status_code=403, detail="You can only delete your own comments.")
        conn.execute("DELETE FROM anime_comments WHERE comment_id = ? AND anime_id = ?", (comment_id, anime_id))
        conn.commit()
        conn.close()
        return {"ok": True, "deleted": comment_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/anime/{anime_id}/comments/{comment_id}/like")
async def like_anime_comment(anime_id: int, comment_id: int, request: Request):
    _ensure_comments_table()
    try:
        body = await request.json()
    except Exception:
        body = {}
    delta = -1 if body.get("unlike") else 1
    try:
        conn = sqlite3.connect(get_db_path(), timeout=15)
        conn.execute(
            "UPDATE anime_comments SET likes = MAX(0, likes + ?) WHERE comment_id = ? AND anime_id = ?",
            (delta, comment_id, anime_id)
        )
        row = conn.execute(
            "SELECT likes FROM anime_comments WHERE comment_id = ? AND anime_id = ?",
            (comment_id, anime_id)
        ).fetchone()
        conn.commit()
        conn.close()
        return {"ok": True, "likes": row[0] if row else 0}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Built-In Render Background Auto-Scraper Daemon ──────────────────
import subprocess as _subprocess
import sys as _sys
import threading as _threading
from datetime import datetime as _datetime, timedelta as _timedelta, timezone as _timezone

_SCRAPER_LOCK = _threading.Lock()
_SCRAPER_THREAD_STARTED = False
_SCRAPER_TRIGGER_EVENT = _threading.Event()
_SCRAPER_INTERVAL_MIN = int(os.environ.get("SCRAPE_INTERVAL_MINUTES", "60"))
_SCRAPER_STATE = {
    "auto_scraper_enabled": not bool(os.environ.get("VERCEL")),
    "interval_minutes": _SCRAPER_INTERVAL_MIN,
    "running": False,
    "current_task": "idle",
    "last_run_at": None,
    "next_run_at": None,
    "runs_completed": 0,
    "last_summary": None,
}


def _find_project_root() -> str:
    for cand in [os.getcwd(), PARENT_DIR, os.path.dirname(PARENT_DIR), CURRENT_DIR]:
        if cand and os.path.exists(os.path.join(cand, "scrape_new_anime_and_episodes_fast.py")):
            return os.path.abspath(cand)
    return os.getcwd()


def _run_scraper_pass():
    root_dir = _find_project_root()
    env = os.environ.copy()
    try:
        db1 = get_db_path()
        if db1:
            env["ANIME_DB"] = db1
    except Exception:
        pass
    try:
        db2 = get_munib_db_path()
        if db2:
            env["MUNIB_DB"] = db2
    except Exception:
        pass
    env.setdefault("MAX_NEW_ANIME_PER_RUN", "15")
    env["SKIP_GZ_SYNC"] = "1"

    _SCRAPER_STATE["running"] = True
    _SCRAPER_STATE["last_run_at"] = _datetime.now(_timezone.utc).isoformat()
    summaries = {}

    for script_name, key_name, label in [
        ("scrape_new_anime_and_episodes_fast.py", "server1", "Server 1 (ZokoAnime)"),
        ("scrape_munib_new_episodes.py", "server2", "Server 2 (Munib Server)"),
    ]:
        script_path = os.path.join(root_dir, script_name)
        _SCRAPER_STATE["current_task"] = f"Running {label} ({script_name})"
        if not os.path.exists(script_path):
            summaries[key_name] = f"{script_name} not found in {root_dir}"
            continue
        t0 = _time.time()
        try:
            proc = _subprocess.run(
                [_sys.executable, "-u", script_path],
                cwd=root_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=600,
            )
            dt = _time.time() - t0
            lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()][-5:]
            summaries[key_name] = f"OK ({dt:.1f}s): " + (" | ".join(lines) if lines else f"exit={proc.returncode}")
        except Exception as e:
            summaries[key_name] = f"Error: {e}"

    _SCRAPER_STATE["running"] = False
    _SCRAPER_STATE["current_task"] = "idle"
    _SCRAPER_STATE["runs_completed"] = int(_SCRAPER_STATE.get("runs_completed") or 0) + 1
    _SCRAPER_STATE["last_summary"] = summaries
    _SCRAPER_STATE["last_run_at"] = _datetime.now(_timezone.utc).isoformat()
    _SCRAPER_STATE["next_run_at"] = (
        _datetime.now(_timezone.utc) + _timedelta(minutes=_SCRAPER_INTERVAL_MIN)
    ).isoformat()

    try:
        sf = "/tmp/scraper_status.json" if os.path.exists("/tmp") else os.path.join(root_dir, "scraper_status.json")
        with open(sf, "w", encoding="utf-8") as f:
            json.dump(_SCRAPER_STATE, f, indent=2)
    except Exception:
        pass


def _background_scraper_worker():
    # Wait 8 seconds after server boot so health checks pass immediately, then run first scrape pass
    _SCRAPER_STATE["next_run_at"] = (_datetime.now(_timezone.utc) + _timedelta(seconds=8)).isoformat()
    _SCRAPER_TRIGGER_EVENT.wait(timeout=8)
    while True:
        _SCRAPER_TRIGGER_EVENT.clear()
        try:
            _run_scraper_pass()
        except Exception as e:
            _SCRAPER_STATE["running"] = False
            _SCRAPER_STATE["current_task"] = f"error: {e}"
        # Wait for next scheduled interval or manual trigger
        _SCRAPER_TRIGGER_EVENT.wait(timeout=_SCRAPER_INTERVAL_MIN * 60)


def _keep_alive_worker():
    """Ping external Render URL every 10 minutes when explicitly configured."""
    raw_url = (os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if not raw_url or "kuro-anime.onrender.com" in raw_url:
        return
    url = raw_url + "/api/stats"
    while True:
        _time.sleep(600)
        try:
            _requests.get(url, timeout=15)
        except Exception:
            pass


def _ensure_background_scraper_started():
    global _SCRAPER_THREAD_STARTED
    if os.environ.get("VERCEL") or os.environ.get("DISABLE_AUTO_SCRAPER", "").lower() in ("1", "true", "yes"):
        return
    with _SCRAPER_LOCK:
        if not _SCRAPER_THREAD_STARTED:
            _SCRAPER_THREAD_STARTED = True
            t = _threading.Thread(target=_background_scraper_worker, name="KuroAutoScraper", daemon=True)
            t.start()
            ka = _threading.Thread(target=_keep_alive_worker, name="KuroRenderKeepAlive", daemon=True)
            ka.start()


@app.on_event("startup")
def _on_app_startup():
    _ensure_background_scraper_started()


@router.get("/scraper/status")
def scraper_status():
    """Return live status of the automated Render background scrapers + DB counts."""
    _ensure_background_scraper_started()
    status_data = dict(_SCRAPER_STATE)

    try:
        db1 = get_db_path()
        if db1 and os.path.exists(db1):
            c1 = sqlite3.connect(db1, timeout=10).cursor()
            status_data["server1_anime"] = c1.execute("SELECT COUNT(*) FROM anime").fetchone()[0]
            status_data["server1_episodes"] = c1.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            status_data["server1_streams"] = c1.execute("SELECT COUNT(*) FROM streams").fetchone()[0]
            c1.connection.close()
    except Exception:
        pass

    try:
        db2 = get_munib_db_path()
        if db2 and os.path.exists(db2):
            c2 = sqlite3.connect(db2, timeout=10).cursor()
            status_data["server2_anime"] = c2.execute("SELECT COUNT(*) FROM anime").fetchone()[0]
            status_data["server2_episodes"] = c2.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            status_data["server2_streams"] = c2.execute("SELECT COUNT(*) FROM streams").fetchone()[0]
            c2.connection.close()
    except Exception:
        pass

    return status_data


@router.get("/scraper/trigger")
@router.post("/scraper/trigger")
def trigger_scraper_now():
    """Immediately wake up the background auto-scraper on Render to fetch new anime & episodes."""
    _ensure_background_scraper_started()
    _SCRAPER_TRIGGER_EVENT.set()
    return {
        "ok": True,
        "message": "Scraper pass triggered in background. Check /api/scraper/status for live progress.",
        "status": _SCRAPER_STATE,
    }


# ─── Server-Side SEO Rendering (SSR Meta + JSON-LD + Crawler Content) ──────
SITE_ORIGIN = "https://kuroanime-flax.vercel.app"
_SSR_TEMPLATE_CACHE: Optional[tuple[float, str]] = None


def _get_spa_template_html() -> str:
    global _SSR_TEMPLATE_CACHE
    root_dir = os.path.dirname(PARENT_DIR)
    candidates = [
        os.path.join(CURRENT_DIR, "_spa_template.html"),
        os.path.join(PARENT_DIR, "anime_app", "web", "dist", "index.html"),
        os.path.join(os.getcwd(), "anime_app", "web", "dist", "index.html"),
        os.path.join(PARENT_DIR, "dist", "index.html"),
        os.path.join(os.getcwd(), "dist", "index.html"),
        os.path.join(PARENT_DIR, "anime_app", "web", "index.html"),
        os.path.join(os.getcwd(), "anime_app", "web", "index.html"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            mtime = os.path.getmtime(c)
            if _SSR_TEMPLATE_CACHE and _SSR_TEMPLATE_CACHE[0] == mtime:
                return _SSR_TEMPLATE_CACHE[1]
            with open(c, "r", encoding="utf-8") as f:
                html_txt = f.read()
            _SSR_TEMPLATE_CACHE = (mtime, html_txt)
            return html_txt
    return "<!doctype html><html><head><title>KuroAnime</title></head><body><main id=\"main-content\"></main></body></html>"


def _dual_anime_names(a: dict) -> tuple[str, str, str]:
    """Return (english_title, romaji_title, combined_title) cleanly deduplicated."""
    eng = (a.get("title_english") or a.get("title") or "").strip()
    romaji = (a.get("title_romaji") or a.get("title") or "").strip()
    if eng and romaji and eng.lower() != romaji.lower():
        combined = f"{eng} ({romaji})"
    else:
        combined = eng or romaji or "Anime"
    return eng or combined, romaji or combined, combined


def _render_ssr_html(
    *,
    title: str,
    description: str,
    canonical_path: str,
    og_image: str = f"{SITE_ORIGIN}/goku-banner.jpg",
    og_type: str = "website",
    schema_obj: Optional[dict] = None,
    ssr_body_html: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    tpl = _get_spa_template_html()
    canon_url = f"{SITE_ORIGIN}{canonical_path}"
    e_title = _html.escape(title, quote=True)
    e_desc = _html.escape(description, quote=True)
    e_canon = _html.escape(canon_url, quote=True)
    e_img = _html.escape(og_image or f"{SITE_ORIGIN}/goku-banner.jpg", quote=True)
    e_type = _html.escape(og_type or "website", quote=True)

    out = re.sub(r"<title>.*?</title>", f"<title>{e_title}</title>", tpl, count=1, flags=re.DOTALL | re.IGNORECASE)
    out = re.sub(
        r'<meta\s+name="description"[^>]*/?>',
        f'<meta name="description" content="{e_desc}" />',
        out,
        count=1,
        flags=re.DOTALL | re.IGNORECASE,
    )
    out = re.sub(
        r'<link\s+rel="canonical"[^>]*id="kuro-canonical"[^>]*/?>',
        f'<link rel="canonical" href="{e_canon}" id="kuro-canonical" />',
        out,
        count=1,
        flags=re.DOTALL | re.IGNORECASE,
    )

    for tag_id, prop_attr, prop_val, content_val in [
        ("kuro-og-type", "property", "og:type", e_type),
        ("kuro-og-url", "property", "og:url", e_canon),
        ("kuro-og-title", "property", "og:title", e_title),
        ("kuro-og-desc", "property", "og:description", e_desc),
        ("kuro-og-image", "property", "og:image", e_img),
        ("kuro-tw-url", "name", "twitter:url", e_canon),
        ("kuro-tw-title", "name", "twitter:title", e_title),
        ("kuro-tw-desc", "name", "twitter:description", e_desc),
        ("kuro-tw-image", "name", "twitter:image", e_img),
    ]:
        out = re.sub(
            rf'<meta\s+[^>]*id="{tag_id}"[^>]*/?>',
            f'<meta {prop_attr}="{prop_val}" content="{content_val}" id="{tag_id}" />',
            out,
            count=1,
            flags=re.DOTALL | re.IGNORECASE,
        )

    if schema_obj is not None:
        safe_json = json.dumps(schema_obj, ensure_ascii=False).replace("</", "<\\/")
        out = re.sub(
            r'<script\s+type="application/ld\+json"\s+id="kuro-schema-json">.*?</script>',
            f'<script type="application/ld+json" id="kuro-schema-json">{safe_json}</script>',
            out,
            count=1,
            flags=re.DOTALL | re.IGNORECASE,
        )

    if ssr_body_html:
        ssr_block = (
            '<div id="kuro-ssr-seo" class="sr-only" '
            'style="position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0">'
            f"{ssr_body_html}"
            "</div>\n</main>"
        )
        out = out.replace("</main>", ssr_block, 1)

    cache_hdr = "no-store" if status_code >= 400 else "public, s-maxage=1800, stale-while-revalidate=86400"
    return HTMLResponse(
        content=out,
        status_code=status_code,
        media_type="text/html",
        headers={"Cache-Control": cache_hdr},
    )


def _load_ssr_anime_context(anime_ident: str):
    """Fetch anime + episodes + flags by numeric ID or URL slug for SSR."""
    anime_id = _resolve_anime_id(anime_ident)
    a = one("SELECT * FROM anime WHERE anime_id = ?", (anime_id,))
    if not a:
        m_list = munib_rows("SELECT * FROM anime WHERE anime_id = ?", (anime_id,))
        if not m_list:
            raise HTTPException(404, "Anime not found")
        a = m_list[0]
    a["poster"] = a.get("poster_large") or a.get("poster_url") or ""
    a.update(audio_flags([anime_id]).get(anime_id, {"sub": True, "dub": False}))
    slug = get_anime_slug(anime_id, a.get("title_english") or a.get("title") or "")
    eps = rows(
        "SELECT episode_id, episode_number, title, japanese_title FROM episodes WHERE anime_id = ? ORDER BY episode_number LIMIT 150",
        (anime_id,),
    )
    if not eps:
        eps = munib_rows(
            "SELECT episode_id, episode_number, title FROM episodes WHERE anime_id = ? ORDER BY episode_number LIMIT 150",
            (anime_id,),
        )
    return a, slug, eps


def _build_anime_ssr_response(anime_ident: str, ep_num: Optional[int] = None, is_watch: bool = False) -> HTMLResponse:
    try:
        a, slug, eps = _load_ssr_anime_context(anime_ident)
    except HTTPException:
        return _render_ssr_html(
            title="Anime Not Found | KuroAnime",
            description="Browse and stream thousands of anime in Sub and Dub with instant player servers on KuroAnime.",
            canonical_path="/browse",
            status_code=404,
        )

    eng, romaji, combined = _dual_anime_names(a)
    raw_syn = re.sub(r"<[^>]+>", " ", a.get("synopsis") or a.get("al_description") or "")
    synopsis = re.sub(r"\s+", " ", raw_syn).strip()
    genres_list = extract_clean_genres(a.get("genres") or a.get("al_genres") or "")
    total_eps = int(a.get("episode_count") or a.get("al_episodes") or len(eps) or 0)
    score_val = None
    if a.get("anilist_score"):
        score_val = round(float(a["anilist_score"]) / 10.0, 1)
    elif a.get("mal_score"):
        score_val = a["mal_score"]
    year_val = a.get("season_year") or ""
    poster_img = a.get("banner_image") or a.get("poster") or f"{SITE_ORIGIN}/goku-banner.jpg"
    upload_date = (a.get("enriched_at") or a.get("scraped_at") or _datetime.now(_timezone.utc).strftime("%Y-%m-%d"))[:10]
    audio_str = "English Sub & Dub" if a.get("dub") else "English Sub"

    if is_watch or ep_num:
        ep_val = max(1, int(ep_num or 1))
        ep_row = next((e for e in eps if int(e.get("episode_number") or 0) == ep_val), None)
        ep_title_part = f": {ep_row['title']}" if ep_row and ep_row.get("title") else ""
        page_title = f"Watch {combined} Episode {ep_val} ({audio_str}) Free | KuroAnime"
        canonical_path = f"/watch/{slug}?ep={ep_val}"
        page_desc = (
            f"Stream {combined} Episode {ep_val}{ep_title_part} online free in HD with {audio_str}. "
            + (synopsis[:140] if synopsis else f"Watch full episodes of {eng} on KuroAnime.")
        )
    else:
        page_title = f"Watch {combined} Sub & Dub Free | KuroAnime"
        canonical_path = f"/anime/{slug}"
        prefix = f"Watch {combined} ({total_eps} episodes) in {audio_str} free HD on KuroAnime. " if total_eps else f"Watch {combined} in {audio_str} free HD on KuroAnime. "
        page_desc = (prefix + synopsis)[:160]

    canon_full = f"{SITE_ORIGIN}{canonical_path}"
    first_genre = genres_list[0] if genres_list else "Anime"
    breadcrumb_schema = {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": f"{SITE_ORIGIN}/"},
            {"@type": "ListItem", "position": 2, "name": "Browse", "item": f"{SITE_ORIGIN}/browse"},
            {"@type": "ListItem", "position": 3, "name": first_genre, "item": f"{SITE_ORIGIN}/genre/{slugify_title(first_genre)}"},
            {"@type": "ListItem", "position": 4, "name": combined, "item": f"{SITE_ORIGIN}/anime/{slug}"},
        ],
    }

    series_schema = {
        "@type": "Movie" if str(a.get("type") or "").lower() == "movie" else "TVSeries",
        "name": eng,
        "alternateName": romaji if romaji != eng else None,
        "url": f"{SITE_ORIGIN}/anime/{slug}",
        "description": synopsis or page_desc,
        "image": a.get("poster") or poster_img,
        "genre": genres_list or ["Anime"],
        "datePublished": str(year_val) if year_val else None,
        "numberOfEpisodes": total_eps or None,
        "inLanguage": ["Japanese", "English"] if a.get("dub") else ["Japanese"],
    }
    if score_val is not None:
        series_schema["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": str(score_val),
            "bestRating": "10",
            "worstRating": "1",
        }
    series_schema = {k: v for k, v in series_schema.items() if v is not None}

    ep_for_video = int(ep_num or (eps[0]["episode_number"] if eps else 1) or 1)
    video_schema = {
        "@type": "VideoObject",
        "name": f"{combined} - Episode {ep_for_video} ({audio_str})",
        "description": page_desc,
        "thumbnailUrl": [a.get("poster") or poster_img],
        "uploadDate": upload_date,
        "contentUrl": f"{SITE_ORIGIN}/watch/{slug}?ep={ep_for_video}",
        "embedUrl": f"{SITE_ORIGIN}/watch/{slug}?ep={ep_for_video}",
        "partOfSeries": {"@type": "TVSeries", "name": combined, "url": f"{SITE_ORIGIN}/anime/{slug}"},
    }

    faq_schema = {
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": f"Where can I watch {combined} in English Sub and Dub?",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": f"Stream all released episodes of {combined} in HD with Japanese Subtitles and English Dub on KuroAnime.",
                },
            },
            {
                "@type": "Question",
                "name": f"How many episodes does {combined} have?",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": f"{combined} has {total_eps or len(eps) or 'multiple'} episodes available to stream free on KuroAnime.",
                },
            },
        ],
    }

    genre_links_html = " ".join(
        f'<a href="/genre/{slugify_title(g)}">{_html.escape(g)}</a>' for g in genres_list[:10]
    )
    ep_links_html = "".join(
        f'<li><a href="/watch/{slug}?ep={int(e.get("episode_number") or 1)}">'
        f'{_html.escape(eng)} Episode {int(e.get("episode_number") or 1)}'
        + (f': {_html.escape(str(e["title"]))}' if e.get("title") else "")
        + f' ({audio_str})</a></li>'
        for e in eps[:120]
    )
    ssr_body = (
        f'<article itemscope itemtype="https://schema.org/TVSeries">'
        f'<h1>{_html.escape(combined)}</h1>'
        f'<p>Watch {_html.escape(combined)} online in English Sub &amp; Dub free HD. '
        f'Released: {_html.escape(str(year_val))} &middot; Status: {_html.escape(str(a.get("status") or ""))} &middot; Episodes: {total_eps}</p>'
        f'<p>{_html.escape(synopsis)}</p>'
        f'<nav aria-label="Genres">{genre_links_html}</nav>'
        f'<section><h2>Episodes of {_html.escape(combined)}</h2><ul>{ep_links_html}</ul></section>'
        f'</article>'
    )

    return _render_ssr_html(
        title=page_title,
        description=page_desc,
        canonical_path=canonical_path,
        og_image=poster_img,
        og_type="video.episode" if (is_watch or ep_num) else "video.tv_show",
        schema_obj={"@context": "https://schema.org", "@graph": [breadcrumb_schema, series_schema, video_schema, faq_schema]},
        ssr_body_html=ssr_body,
    )


@app.get("/anime/{anime_ident}", response_class=HTMLResponse, include_in_schema=False)
def ssr_anime_page(anime_ident: str, ep: Optional[int] = Query(None)):
    return _build_anime_ssr_response(anime_ident, ep_num=ep, is_watch=False)


@app.get("/watch/{anime_ident}", response_class=HTMLResponse, include_in_schema=False)
def ssr_watch_page(anime_ident: str, ep: int = Query(1)):
    return _build_anime_ssr_response(anime_ident, ep_num=ep, is_watch=True)


def _build_collection_ssr(title: str, heading: str, description: str, canonical_path: str, items: list[dict]) -> HTMLResponse:
    list_elems = []
    links_html = []
    for idx, it in enumerate(items[:24], start=1):
        eng, romaji, combined = _dual_anime_names(it)
        slug = it.get("slug") or get_anime_slug(it["anime_id"], eng)
        url = f"{SITE_ORIGIN}/anime/{slug}"
        list_elems.append({
            "@type": "ListItem",
            "position": idx,
            "name": combined,
            "url": url,
        })
        links_html.append(f'<li><a href="/anime/{_html.escape(slug)}">Watch {_html.escape(combined)} Sub &amp; Dub</a></li>')

    schema_obj = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": heading,
        "description": description,
        "url": f"{SITE_ORIGIN}{canonical_path}",
        "mainEntity": {
            "@type": "ItemList",
            "itemListElement": list_elems,
        },
    }
    ssr_body = (
        f'<section><h1>{_html.escape(heading)}</h1><p>{_html.escape(description)}</p>'
        f'<ul>{"".join(links_html)}</ul></section>'
    )
    return _render_ssr_html(
        title=title,
        description=description,
        canonical_path=canonical_path,
        schema_obj=schema_obj,
        ssr_body_html=ssr_body,
    )


@app.get("/genre/{genre_slug}", response_class=HTMLResponse, include_in_schema=False)
def ssr_genre_page(genre_slug: str):
    genre_name = _normalize_genre_query(genre_slug) or genre_slug.replace("-", " ").title()
    data = browse(genre=genre_name, per_page=24, page=1, sort="popular")
    heading = f"Best {genre_name} Anime to Watch Free in Sub & Dub"
    desc = f"Discover and stream the best {genre_name} anime series and movies in English Sub and Dub free HD on KuroAnime."
    return _build_collection_ssr(
        title=f"{heading} | KuroAnime",
        heading=heading,
        description=desc,
        canonical_path=f"/genre/{slugify_title(genre_name)}",
        items=data.get("results") or [],
    )


@app.get("/dubbed", response_class=HTMLResponse, include_in_schema=False)
def ssr_dubbed_page():
    data = browse(audio="dub", per_page=24, page=1, sort="popular")
    heading = "Watch English Dubbed Anime Online Free"
    desc = "Stream thousands of top-rated English dubbed anime series and movies free in HD with zero popup ads on KuroAnime."
    return _build_collection_ssr(
        title=f"{heading} | KuroAnime",
        heading=heading,
        description=desc,
        canonical_path="/dubbed",
        items=data.get("results") or [],
    )


@app.get("/airing", response_class=HTMLResponse, include_in_schema=False)
def ssr_airing_page():
    year = _datetime.now(_timezone.utc).year
    data = browse(status="airing", per_page=24, page=1, sort="popular")
    heading = f"Currently Airing Seasonal Anime ({year})"
    desc = f"Watch the latest ongoing and currently airing {year} anime episodes updated daily in Sub and Dub on KuroAnime."
    return _build_collection_ssr(
        title=f"{heading} | KuroAnime",
        heading=heading,
        description=desc,
        canonical_path="/airing",
        items=data.get("results") or [],
    )


@app.get("/advisor", response_class=HTMLResponse, include_in_schema=False)
def ssr_advisor_page():
    title = "Prince Vegeta AI Anime Advisor & Recommender | KuroAnime"
    desc = "Ask Prince Vegeta AI for personalized anime recommendations, plot lookups, or upload any anime screenshot to identify the series and episode."
    schema_obj = {
        "@context": "https://schema.org",
        "@type": "WebApplication",
        "name": "Prince Vegeta AI Anime Advisor",
        "applicationCategory": "EntertainmentApplication",
        "operatingSystem": "Web",
        "url": f"{SITE_ORIGIN}/advisor",
        "description": desc,
    }
    ssr_body = (
        '<section><h1>Prince Vegeta AI Anime Advisor &amp; Recommender</h1>'
        '<p>Get instant personalized anime recommendations by mood, genre, or favorite series, '
        'or upload an anime character portrait or episode screenshot to identify the anime on KuroAnime.</p></section>'
    )
    return _render_ssr_html(
        title=title,
        description=desc,
        canonical_path="/advisor",
        schema_obj=schema_obj,
        ssr_body_html=ssr_body,
    )


app.include_router(router, prefix="/api")
app.include_router(router)

# ─── Standalone / Render Static Frontend Serving ─────────────────────
if not os.environ.get("VERCEL"):
    _dist_candidates = [
        os.path.join(os.getcwd(), "anime_app", "web", "dist"),
        os.path.join(PARENT_DIR, "anime_app", "web", "dist"),
        os.path.join(CURRENT_DIR, "..", "web", "dist"),
    ]
    for _dist_dir in _dist_candidates:
        _dist_abs = os.path.abspath(_dist_dir)
        if os.path.isdir(_dist_abs) and os.path.exists(os.path.join(_dist_abs, "index.html")):
            from fastapi.staticfiles import StaticFiles
            from fastapi.responses import FileResponse

            _assets_dir = os.path.join(_dist_abs, "assets")
            if os.path.isdir(_assets_dir):
                app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")

            @app.get("/{full_path:path}", include_in_schema=False)
            async def serve_spa_frontend(full_path: str):
                candidate_file = os.path.abspath(os.path.join(_dist_abs, full_path))
                if (
                    full_path
                    and candidate_file.startswith(_dist_abs)
                    and os.path.isfile(candidate_file)
                ):
                    return FileResponse(candidate_file)
                return FileResponse(os.path.join(_dist_abs, "index.html"))

            break
