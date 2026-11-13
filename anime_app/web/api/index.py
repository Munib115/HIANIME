"""
KuroAnime API — Vercel Serverless & Standalone FastAPI Backend
Provides JSON API over SQLite hianime.db + Direct Ad-Free HLS Stream Resolver & Proxy
"""
import os
import sqlite3
import re
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

# ─── Persistent HTTP Pool for Fast HLS Segment Proxying ─────────────
_STREAM_SESSION = _requests.Session()
_STREAM_ADAPTER = HTTPAdapter(pool_connections=30, pool_maxsize=30)
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

def get_munib_db_path():
    global _INITIALIZED_MUNIB_DB
    if _INITIALIZED_MUNIB_DB and os.path.exists(_INITIALIZED_MUNIB_DB):
        return _INITIALIZED_MUNIB_DB

    source = find_munib_db()
    if not source or not os.path.exists(source):
        return None

    if source.endswith(".gz"):
        import gzip
        import shutil
        import tempfile
        tmp_dir = "/tmp" if (os.environ.get("VERCEL") or os.path.exists("/tmp")) else tempfile.gettempdir()
        tmp_db = os.path.join(tmp_dir, "munib_server.db")
        try:
            if not os.path.exists(tmp_db) or os.path.getsize(tmp_db) == 0:
                with gzip.open(source, "rb") as f_in, open(tmp_db, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            if os.path.exists(tmp_db) and os.path.getsize(tmp_db) > 0:
                _INITIALIZED_MUNIB_DB = tmp_db
                return _INITIALIZED_MUNIB_DB
        except Exception:
            return None

    if os.environ.get("VERCEL") or os.path.exists("/tmp"):
        tmp_db = "/tmp/munib_server.db"
        try:
            if not os.path.exists(tmp_db) or os.path.getsize(tmp_db) != os.path.getsize(source):
                import shutil
                shutil.copyfile(source, tmp_db)
            if os.path.exists(tmp_db) and os.path.getsize(tmp_db) > 0:
                _INITIALIZED_MUNIB_DB = tmp_db
                return _INITIALIZED_MUNIB_DB
        except Exception:
            pass

    _INITIALIZED_MUNIB_DB = source
    return source


def munib_db():
    path = get_munib_db_path()
    if not path or not os.path.exists(path):
        return None

    if path.startswith("/tmp"):
        try:
            conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn
        except Exception:
            pass

    norm_path = os.path.abspath(path).replace("\\", "/")
    if not norm_path.startswith("/"):
        norm_path = "/" + norm_path

    try:
        conn = sqlite3.connect(f"file://{norm_path}?mode=ro&immutable=1", uri=True, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        pass

    try:
        conn = sqlite3.connect(f"file://{norm_path}?mode=ro", uri=True, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        pass

    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


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

CARD_COLS = """anime_id, title, title_english, type, status, episode_count,
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

    if source.endswith(".gz"):
        import gzip
        import shutil
        import tempfile
        tmp_dir = "/tmp" if (os.environ.get("VERCEL") or os.path.exists("/tmp")) else tempfile.gettempdir()
        tmp_db = os.path.join(tmp_dir, "hianime.db")
        try:
            if not os.path.exists(tmp_db) or os.path.getsize(tmp_db) == 0:
                with gzip.open(source, "rb") as f_in, open(tmp_db, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            if os.path.exists(tmp_db) and os.path.getsize(tmp_db) > 0:
                _INITIALIZED_DB = tmp_db
                return _INITIALIZED_DB
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to decompress DB: {e}")

    # In serverless read-only environments (e.g. Vercel / AWS Lambda), copy DB to /tmp
    # to guarantee fully concurrent read/write locks, -shm/-wal handling, and zero disk errors
    if os.environ.get("VERCEL") or os.path.exists("/tmp"):
        tmp_db = "/tmp/hianime.db"
        try:
            if not os.path.exists(tmp_db) or os.path.getsize(tmp_db) != os.path.getsize(source):
                import shutil
                shutil.copyfile(source, tmp_db)
            if os.path.exists(tmp_db) and os.path.getsize(tmp_db) > 0:
                _INITIALIZED_DB = tmp_db
                return _INITIALIZED_DB
        except Exception:
            pass

    _INITIALIZED_DB = source
    return source


def db():
    path = get_db_path()

    # If DB is in /tmp, direct connection has full local filesystem support
    if path.startswith("/tmp"):
        try:
            conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn
        except Exception:
            pass

    norm_path = os.path.abspath(path).replace("\\", "/")
    if not norm_path.startswith("/"):
        norm_path = "/" + norm_path

    # On read-only media like /var/task, immutable=1 completely disables WAL/SHM file locking
    try:
        conn = sqlite3.connect(f"file://{norm_path}?mode=ro&immutable=1", uri=True, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        pass

    try:
        conn = sqlite3.connect(f"file://{norm_path}?mode=ro", uri=True, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        pass

    conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


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


def with_flags(cards):
    flags = audio_flags([c["anime_id"] for c in cards])
    for c in cards:
        c.update(flags.get(c["anime_id"], {"sub": False, "dub": False}))
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


MUNIB_CARD_COLS = """anime_id, title, title_english, type, status, episode_count,
                     anilist_score, NULL AS mal_score, season_year,
                     COALESCE(NULLIF(poster_large,''), poster_url) AS poster"""



def clean_youtube_trailer_id(raw_id: str):
    if not raw_id:
        return None
    m = re.search(r"[a-zA-Z0-9_-]{11}", str(raw_id))
    return m.group(0) if m else None


def resolve_trailer_id(anilist_id=None, title=None):
    if anilist_id:
        try:
            q = "query ($id: Int) { Media(id: $id, type: ANIME) { trailer { id site } } }"
            r = _requests.post("https://graphql.anilist.co", json={"query": q, "variables": {"id": int(anilist_id)}}, timeout=4)
            if r.status_code == 200:
                tr = r.json().get("data", {}).get("Media", {}).get("trailer")
                if tr and tr.get("site") == "youtube" and tr.get("id"):
                    cid = clean_youtube_trailer_id(tr["id"])
                    if cid:
                        return cid
        except Exception:
            pass

    if title:
        try:
            q = "query ($search: String) { Media(search: $search, type: ANIME) { trailer { id site } } }"
            r = _requests.post("https://graphql.anilist.co", json={"query": q, "variables": {"search": title}}, timeout=4)
            if r.status_code == 200:
                tr = r.json().get("data", {}).get("Media", {}).get("trailer")
                if tr and tr.get("site") == "youtube" and tr.get("id"):
                    cid = clean_youtube_trailer_id(tr["id"])
                    if cid:
                        return cid
        except Exception:
            pass

    if title:
        try:
            enc = urllib.parse.quote(title)
            r = _requests.get(f"https://api.jikan.moe/v4/anime?q={enc}&limit=1", timeout=4)
            if r.status_code == 200:
                d = r.json().get("data", [])
                if d and isinstance(d, list) and len(d) > 0:
                    yt_id = d[0].get("trailer", {}).get("youtube_id")
                    if yt_id:
                        cid = clean_youtube_trailer_id(yt_id)
                        if cid:
                            return cid
        except Exception:
            pass

    if title:
        try:
            enc = urllib.parse.quote(f"{title} official anime trailer")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9"
            }
            r = _requests.get(f"https://www.youtube.com/results?search_query={enc}", headers=headers, timeout=4)
            if r.status_code == 200:
                matches = re.findall(r"/watch\?v=([a-zA-Z0-9_-]{11})", r.text)
                if matches:
                    return matches[0]
                json_matches = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', r.text)
                if json_matches:
                    return json_matches[0]
        except Exception:
            pass

    return None


@router.get("/anime/{anime_id}")
def anime_detail(anime_id: int):
    a = one("SELECT * FROM anime WHERE anime_id = ?", (anime_id,))
    if not a:
        m_list = munib_rows("SELECT * FROM anime WHERE anime_id = ?", (anime_id,))
        if not m_list:
            raise HTTPException(404, "Anime not found")
        a = m_list[0]
        a["poster"] = a.get("poster_large") or a.get("poster_url")
        a["seasons"] = []
        a.update(audio_flags([anime_id]).get(anime_id, {"sub": True, "dub": False}))
        return a
    a["poster"] = a.get("poster_large") or a.get("poster_url")
    a["seasons"] = rows("""
        SELECT season_name, season_anime_id, season_poster, is_current
        FROM anime_seasons WHERE parent_anime_id = ? ORDER BY id
    """, (anime_id,))
    have = {r["anime_id"] for r in rows("SELECT anime_id FROM anime")} if a["seasons"] else set()
    for s in a["seasons"]:
        s["available"] = s["season_anime_id"] in have
    a.update(audio_flags([anime_id]).get(anime_id, {}))
    try:
        t_row = one("SELECT trailer_id, site FROM anime_trailers WHERE anime_id = ?", (anime_id,))
        if t_row and t_row.get("trailer_id"):
            a["trailer_id"] = clean_youtube_trailer_id(t_row["trailer_id"])
            a["trailer_site"] = t_row.get("site") or "youtube"
    except Exception:
        pass
    a.pop("seasons_json", None)
    return a



@router.get("/anime/{anime_id}/trailer")
def anime_trailer(anime_id: int):
    # 1. Check local anime_trailers table
    try:
        t_row = one("SELECT trailer_id, site FROM anime_trailers WHERE anime_id = ?", (anime_id,))
        if t_row and t_row.get("trailer_id"):
            tid = clean_youtube_trailer_id(t_row["trailer_id"])
            if tid:
                return {
                    "ok": True,
                    "anime_id": anime_id,
                    "trailer_id": tid,
                    "site": t_row.get("site") or "youtube",
                    "embed_url": f"https://www.youtube.com/embed/{tid}?autoplay=1&playsinline=1&rel=0&modestbranding=1"
                }
    except Exception:
        pass

    # 2. Get anime record
    a = one("SELECT anime_id, title, title_english, anilist_id FROM anime WHERE anime_id = ?", (anime_id,))
    if not a:
        m_list = munib_rows("SELECT anime_id, title, title_english, anilist_id FROM anime WHERE anime_id = ?", (anime_id,))
        a = m_list[0] if m_list else None
    if not a:
        raise HTTPException(404, "Anime not found")

    anilist_id = a.get("anilist_id")
    title = a.get("title_english") or a.get("title")

    # 3. Dynamic multi-source resolution
    tid = resolve_trailer_id(anilist_id, title)
    if tid:
        try:
            conn = db()
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


@router.get("/anime/{anime_id}/episodes")
def anime_episodes(anime_id: int):
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
                    # Live fallback to reanime.to flix API if not yet cached in munib_server.db
                    if not munib_data and ep_no is not None:
                        try:
                            fr = _STREAM_SESSION.get(
                                f"https://reanime.to/api/flix/{al_id}/{ep_no}",
                                headers={"User-Agent": _STREAM_HEADERS["User-Agent"], "Referer": "https://reanime.to/home"},
                                timeout=3,
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


@router.get("/browse")
def browse(
    genre: Optional[str] = None,
    type: Optional[str] = None,
    status: Optional[str] = None,
    sort: str = "popular",
    page: int = 1,
    per_page: int = 30,
):
    where, params = [], []
    m_where, m_params = [], []
    if genre:
        where.append("(genres LIKE ? OR al_genres LIKE ?)")
        params += [f"%{genre}%", f"%{genre}%"]
        m_where.append("genres LIKE ?")
        m_params.append(f"%{genre}%")
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
    order = {
        "popular": "anilist_popularity DESC NULLS LAST",
        "score": "anilist_score DESC NULLS LAST",
        "az": "title COLLATE NOCASE ASC",
        "new": "season_year DESC NULLS LAST, anilist_popularity DESC NULLS LAST",
    }.get(sort, "anilist_popularity DESC NULLS LAST")
    per_page = max(1, min(per_page, 60))
    offset = (max(page, 1) - 1) * per_page

    # If filtering by Hentai (or if main DB has no matches), query Munib Server DB directly
    if genre and genre.strip().lower() == "hentai":
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
    counts = {}
    for r in rows("SELECT genres FROM anime WHERE genres IS NOT NULL AND genres != ''"):
        for g in r["genres"].split(","):
            g = g.strip()
            if g:
                counts[g] = counts.get(g, 0) + 1
    for r in munib_rows("SELECT genres FROM anime WHERE genres IS NOT NULL AND genres != '' AND anime_id >= 1000000"):
        for g in r["genres"].split(","):
            g = g.strip()
            if g:
                counts[g] = counts.get(g, 0) + 1
    return {"genres": sorted(counts, key=lambda g: -counts[g])}


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


@router.get("/player/resolve")
def player_resolve(url: str = Query(...)):
    """
    Extract direct HLS master playlist, quality variants, multi-language VTT subtitles,
    and intro/outro skip timestamps from an embed URL.
    """
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid embed URL")

    try:
        r = _STREAM_SESSION.get(url, headers={
            "User-Agent": _STREAM_HEADERS["User-Agent"],
            "Referer": "https://hianime.at/",
        }, timeout=12)
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Upstream embed returned {r.status_code}")

        m = re.search(r'window\.__P\s*=\s*["\']([^"\']+)["\']', r.text)
        if not m:
            raise HTTPException(status_code=422, detail="Stream payload not found in embed page")

        payload = _deobfuscate_payload(m.group(1))
        master_url = payload.get("src") or ""
        if not master_url:
            raise HTTPException(status_code=422, detail="No HLS source URL in payload")

        # Fetch master.m3u8 to discover quality variants
        qualities = []
        try:
            mr = _STREAM_SESSION.get(master_url, headers=_STREAM_HEADERS, timeout=10)
            if mr.status_code == 200 and "#EXTM3U" in mr.text:
                qualities = _parse_master_variants(mr.text, master_url)
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

        return {
            "ok": True,
            "embed_url": url,
            "raw_master_url": master_url,
            "master_url": f"/api/player/hls?url={urllib.parse.quote(master_url, safe='')}",
            "qualities": qualities,
            "subtitles": subtitles,
            "skip": payload.get("skip") or {"intro": None, "outro": None},
            "poster": payload.get("poster") or "",
            "video_id": payload.get("video_id"),
            "download_url": payload.get("download_url") or "",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stream resolve failed: {str(e)}")


def _normalize_segment_url(seg_url: str, origin: str) -> str:
    """Normalize edge segment URLs (/s/...) to the primary HLS origin so they never 403."""
    parsed = urllib.parse.urlparse(seg_url)
    if parsed.path.startswith("/s/"):
        return f"{origin}{parsed.path}{'?' + parsed.query if parsed.query else ''}"
    return seg_url


@router.get("/player/hls")
def player_hls(url: str = Query(...)):
    """Proxy and rewrite master or media .m3u8 playlists so all requests route with clean CORS and Referer."""
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid HLS URL")

    try:
        r = _STREAM_SESSION.get(url, headers=_STREAM_HEADERS, timeout=12)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="Upstream HLS playlist error")

        parsed_base = urllib.parse.urlparse(url)
        origin = f"{parsed_base.scheme}://{parsed_base.netloc}"

        out_lines = []
        for raw_line in r.text.splitlines():
            line = raw_line.strip()
            if not line:
                out_lines.append("")
                continue

            if line.startswith("#"):
                # Rewrite URI="..." inside #EXT-X-KEY, #EXT-X-MAP, or #EXT-X-MEDIA
                if 'URI="' in line:
                    def _repl_uri(match):
                        raw_uri = match.group(1)
                        u = urllib.parse.urljoin(url, raw_uri)
                        parsed_u = urllib.parse.urlparse(u)
                        if parsed_u.path.endswith(".m3u8"):
                            return f'URI="/api/player/hls?url={urllib.parse.quote(u, safe="")}"'
                        u_norm = _normalize_segment_url(u, origin)
                        return f'URI="/api/player/segment?url={urllib.parse.quote(u_norm, safe="")}"'
                    line = re.sub(r'URI="([^"]+)"', _repl_uri, line)
                out_lines.append(line)
                continue

            full_url = urllib.parse.urljoin(url, line)
            parsed_target = urllib.parse.urlparse(full_url)
            if parsed_target.path.endswith(".m3u8"):
                out_lines.append(f"/api/player/hls?url={urllib.parse.quote(full_url, safe='')}")
            else:
                norm_seg = _normalize_segment_url(full_url, origin)
                out_lines.append(f"/api/player/segment?url={urllib.parse.quote(norm_seg, safe='')}")

        content = "\n".join(out_lines)
        return Response(
            content=content,
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-cache",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"HLS proxy error: {str(e)}")


@router.get("/player/segment")
def player_segment(url: str = Query(...)):
    """Proxy an individual MPEG-TS video segment with Referer authentication and automatic edge failover."""
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid segment URL")

    parsed = urllib.parse.urlparse(url)
    candidate_urls = [url]
    if parsed.path.startswith("/s/"):
        for fallback_host in ("https://hls.dramahot.top", "https://hls1.zoko1.cfd", "https://hls1.proxy1.cfd"):
            alt = f"{fallback_host}{parsed.path}{'?' + parsed.query if parsed.query else ''}"
            if alt not in candidate_urls:
                candidate_urls.append(alt)

    last_status = 502
    for target in candidate_urls:
        try:
            r = _STREAM_SESSION.get(target, headers=_STREAM_HEADERS, timeout=15)
            last_status = r.status_code
            if r.status_code == 200 and r.content:
                return Response(
                    content=r.content,
                    media_type="video/mp2t",
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": "public, max-age=86400, immutable",
                    },
                )
        except Exception:
            continue

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
        import urllib.request

        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": url,
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

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
        raise HTTPException(status_code=502, detail=f"Failed to fetch clean stream: {str(e)}")



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

        syn_clean = _clean_html(r[18] or r[19] or "")
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
            "synopsis_lower": syn_clean.lower(),
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
            movie_penalty = -35.0 if ("movie" in it["title"].lower() and "movie" not in check_phrase) else 0.0
            tv_boost = 10.0 if it["type"] == "TV" else 0.0
            pop_boost = min((it["popularity"] or 0) / 10000.0, 20.0)
            ep_boost = min((it["episode_count"] or 1) / 10.0, 15.0)
            score_boost = (it["score"] / 10.0) * 5.0
            total = best_title_score + primary_boost + eng_boost + movie_penalty + tv_boost + pop_boost + ep_boost + score_boost
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


def _vegeta_reverse_image_search(image_base64: str, kb: dict) -> dict:
    """Reverse search an anime screenshot using trace.moe with strict confidence gating."""
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
        import urllib.request as _urllib_req
        req = _urllib_req.Request(
            "https://api.trace.moe/search",
            data=img_bytes,
            headers={
                "Content-Type": "image/jpeg",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
        )
        with _urllib_req.urlopen(req, timeout=9) as res:
            data = json.loads(res.read().decode("utf-8"))
            results = data.get("result") or []
            if not results:
                return None

            by_al = kb.get("by_anilist_id", {})
            best_sim = float(results[0].get("similarity") or 0.0)

            # Discard low-similarity trace.moe hits (< 0.78) to prevent false matches on character portraits/fanart
            if best_sim < 0.78:
                return {
                    "similarity": best_sim,
                    "confident": False,
                    "anilist_id": results[0].get("anilist"),
                    "episode": results[0].get("episode") or 1,
                    "from_seconds": float(results[0].get("from") or 0.0),
                    "filename": results[0].get("filename") or "",
                    "matched_item": None,
                }

            for cand in results[:3]:
                similarity = float(cand.get("similarity") or 0.0)
                if similarity < 0.78:
                    continue
                al_id = cand.get("anilist")
                episode = cand.get("episode") or 1
                from_sec = float(cand.get("from") or 0.0)
                filename = cand.get("filename") or ""

                matched_item = None
                if al_id and int(al_id) in by_al:
                    matched_item = by_al[int(al_id)]

                if not matched_item and filename and similarity >= 0.84:
                    clean_fn = re.sub(r"\[.*?\]|\(.*?\)|_|\.mp4|\.mkv|\d{3,4}p|x264|AAC", " ", filename, flags=re.I)
                    clean_fn = re.sub(r"-\s*\d+.*$", "", clean_fn).strip()
                    if clean_fn:
                        matched_item, _ = _find_exact_or_fuzzy_title(clean_fn, kb["items"])

                if matched_item:
                    return {
                        "similarity": similarity,
                        "confident": similarity >= 0.87,
                        "anilist_id": al_id,
                        "episode": episode,
                        "from_seconds": from_sec,
                        "filename": filename,
                        "matched_item": matched_item,
                    }

            return {
                "similarity": best_sim,
                "confident": False,
                "anilist_id": results[0].get("anilist"),
                "episode": results[0].get("episode") or 1,
                "from_seconds": float(results[0].get("from") or 0.0),
                "filename": results[0].get("filename") or "",
                "matched_item": None,
            }
    except Exception as err:
        print(f"trace.moe search error: {err}")
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


def _find_by_plot_description(msg_low: str, items: list) -> tuple:
    """Identify an anime from a story background / plot description across both servers."""
    plot_signatures = [
        ("slime", ["slime", "reincarnated as a slime", "rimuru", "tempest"], "That Time I Got Reincarnated as a Slime"),
        ("death note", ["notebook", "write names", "shinigami", "ryuk", "kira", "l lawliet"], "Death Note"),
        ("attack on titan", ["titans", "giant humanoid", "walls", "scout regiment", "eren", "wall maria", "odm gear"], "Attack on Titan"),
        ("demon slayer", ["bamboo muzzle", "nezuko", "tanjiro", "breathing technique", "demon slayer corps", "hashira"], "Demon Slayer"),
        ("jujutsu kaisen", ["eats a finger", "cursed spirit", "sukuna", "gojo", "domain expansion", "yuji itadori"], "Jujutsu Kaisen"),
        ("one punch man", ["one punch", "bald hero", "defeats everyone easily", "saitama", "genos", "hero association"], "One Punch Man"),
        ("sword art online", ["virtual reality", "trapped in a game", "die in game", "die in real life", "kirito", "nervegear", "aincrad"], "Sword Art Online"),
        ("fullmetal alchemist", ["human transmutation", "two brothers", "arm and leg", "alchemist", "philosopher stone", "elric", "state alchemist"], "Fullmetal Alchemist: Brotherhood"),
        ("steins gate", ["microwave", "time travel", "banana", "divergence meter", "okabe", "kurisu", "mad scientist", "el psy kongroo"], "Steins;Gate"),
        ("monster", ["doctor", "surgeon", "saves a young boy", "johan liebert", "charismatic psychopath", "tenma"], "Monster"),
        ("tokyo ghoul", ["half ghoul", "coffee", "eats humans", "kaneki", "organ transplant", "kagune", "anteiku"], "Tokyo Ghoul"),
        ("parasyte", ["alien parasite", "right hand", "migi", "shinichi"], "Parasyte -the maxim-"),
        ("dr stone", ["turned to stone", "petrified", "science", "senku", "rebuild civilization", "stone world"], "Dr. STONE"),
        ("vinland saga", ["vikings", "thorfinn", "revenge", "askeladd", "duels", "vinland"], "Vinland Saga"),
        ("hunter x hunter", ["hunter exam", "killua", "gon", "nen", "kurapika", "phantom troupe", "chimera ant"], "Hunter x Hunter"),
        ("code geass", ["geass", "absolute obedience", "lelouch", "zero", "britannia", "knightmare"], "Code Geass"),
        ("chainsaw man", ["chainsaw", "devil", "denji", "pochita", "makima", "public safety"], "Chainsaw Man"),
        ("solo leveling", ["shadow monarch", "weakest hunter", "system", "sung jinwoo", "double dungeon", "arise"], "Solo Leveling"),
        ("re zero", ["return by death", "subaru", "emilia", "rem", "time loop", "dies and resets"], "Re:ZERO -Starting Life in Another World-"),
        ("frieren", ["elf mage", "defeated the demon king", "defeating the demon king", "demon king", "after the hero dies", "frieren", "fern", "himmel"], "Frieren: Beyond Journey's End"),
        ("mushoku tensei", ["jobless", "rudeus", "reincarnated as a baby", "magic prodigy", "eris", "roxy"], "Mushoku Tensei: Jobless Reincarnation"),
        ("spy x family", ["fake family", "telepath", "anya", "assassin mother", "spy father", "loid", "yor"], "SPY x FAMILY"),
        ("my hero academia", ["quirkless", "all might", "deku", "midoriya", "ua high", "one for all"], "My Hero Academia"),
        ("bleach", ["soul reaper", "shinigami", "ichigo", "hollow", "zanpakuto", "soul society", "bankai"], "Bleach"),
        ("naruto", ["nine tailed fox", "hokage", "ninja", "naruto", "sasuke", "hidden leaf", "rasengan"], "Naruto"),
        ("one piece", ["rubber body", "devil fruit", "pirate king", "luffy", "straw hat", "grand line", "zoro"], "One Piece"),
        ("dragon ball", ["saiyan", "seven dragon balls", "goku", "vegeta", "kamehameha", "super saiyan"], "Dragon Ball Z"),
        ("blue lock", ["egoist", "soccer prison", "striker", "isagi", "blue lock"], "BLUE LOCK"),
        ("haikyuu", ["short volleyball", "hinata", "karasuno", "kageyama", "volleyball"], "Haikyu!!"),
        ("dandadan", ["ghosts and aliens", "turbo granny", "okarun", "momo ayase", "golden ball"], "DAN DA DAN"),
        ("kaiju no 8", ["cleanup crew", "kafka hibino", "turns into a kaiju", "defense force"], "Kaiju No. 8"),
        ("overlord", ["skeleton", "ainz ooal gown", "nazarick", "trapped in game", "undead overlord"], "Overlord"),
        ("mob psycho", ["esper", "psychic", "reigen", "100 percent", "shigeo", "mob"], "Mob Psycho 100"),
        ("cyberpunk edgerunners", ["night city", "sandevistan", "david martinez", "lucy", "cyberpsychosis"], "Cyberpunk: Edgerunners"),
        ("your name", ["body swap", "comet", "mitsuha", "taki", "red string"], "Your Name."),
        ("oshi no ko", ["reincarnated as idol", "ai hoshino", "ruby", "aqua", "star eyes"], "Oshi No Ko"),
        ("classroom of the elite", ["ayanokoji", "white room", "class d", "meritocracy", "points system"], "Classroom of the Elite"),
        ("black clover", ["no magic", "anti magic", "asta", "yuno", "wizard king", "five leaf grimoire"], "Black Clover"),
        ("cowboy bebop", ["bounty hunter", "spaceship", "spike spiegel", "bebop", "jazz"], "Cowboy Bebop"),
        ("evangelion", ["eva unit", "angels", "shinji", "nerv", "third impact", "get in the robot"], "Neon Genesis Evangelion"),
        ("erased", ["revival", "goes back in time", "elementary school", "kayo", "satoru"], "ERASED"),
        ("shield hero", ["rising of the shield", "naofumi", "falsely accused", "raphtalia", "four cardinal heroes"], "The Rising of the Shield Hero"),
        ("eminence in shadow", ["atomic", "shadow garden", "cid kagenou", "chunnibyou", "i am atomic"], "The Eminence in Shadow"),
    ]

    identify_phrase = any(k in msg_low for k in [
        "what is that anime", "what's that anime", "whats that anime", "which anime", "forgot the name",
        "anime where", "show where", "the one where", "name of the anime", "what anime", "anime about a",
        "what is the anime", "what's the anime", "cant remember", "can't remember", "story where",
        "background story", "plot is", "about a guy who", "about a boy who", "about a girl who",
    ])
    need = 1 if identify_phrase else 2
    for key_tag, triggers, target_title in plot_signatures:
        matches = sum(1 for t in triggers if re.search(rf"\b{re.escape(t)}\b", msg_low))
        if matches >= need:
            specific, _ = _find_exact_or_fuzzy_title(target_title, items)
            if specific:
                return specific, target_title

    plot_stopwords = _VEGETA_STOPWORDS | {
        "anime", "show", "series", "story", "background", "character", "main", "guy", "boy", "girl",
        "man", "woman", "world", "power", "powers", "people", "gets", "become", "becomes", "wants",
        "finds", "lives", "where", "about", "forgot", "name", "remember", "called", "which", "there",
    }
    words = [w for w in re.findall(r"[a-z0-9']{4,}", msg_low) if w not in plot_stopwords]
    if len(words) >= 3 and (identify_phrase or len(words) >= 5):
        best_item = None
        best_score = 0.0
        for it in items:
            syn_h = it.get("synopsis_lower") or ""
            tag_h = it.get("tags_lower") or ""
            chr_h = it.get("characters_lower") or ""
            gen_h = it.get("genres_lower") or ""
            score = 0.0
            hits = 0
            for w in words:
                w_hit = False
                if w in chr_h:
                    score += 3.5
                    w_hit = True
                if w in tag_h:
                    score += 2.5
                    w_hit = True
                if w in syn_h:
                    score += 2.0
                    w_hit = True
                if w in gen_h:
                    score += 1.0
                    w_hit = True
                if w_hit:
                    hits += 1
            if hits >= (2 if len(words) <= 4 else 3):
                # Tie-break gently with popularity so main series beats obscure OVAs
                pop_bonus = min(1.5, ((it.get("popularity") or 0) / 500000.0))
                total_score = score + pop_bonus
                if total_score > best_score:
                    best_score = total_score
                    best_item = it
        if best_item and best_score >= 4.5:
            return best_item, best_item["title_english"] or best_item["title"]

    return None, None


# ─── Vegeta Agent Brain v2: planner, tools, similarity, taste, Gemini tool loop ─────
import math as _vg_math
import random as _vg_random
import datetime as _vg_dt

_VG_CMD_VERBS = (
    "play", "start", "watch", "stream", "resume", "continue", "add", "save", "bookmark", "remove",
    "delete", "drop", "open", "show", "go", "take", "navigate", "search", "look up", "switch", "change",
    "next", "skip", "previous", "prev", "put", "launch", "browse", "explore", "surprise", "pull up",
    "bring up", "set", "turn", "dub", "sub",
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
    it, _ = _find_exact_or_fuzzy_title(p, kb["items"])
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
        return f"Opening the **{t}** page"
    if typ == "add_to_list":
        return f"Adding **{t}** to your list"
    if typ == "remove_from_list":
        return f"Removing **{t}** from your list"
    if typ == "player":
        return {"next": "Skipping to the next episode", "previous": "Going back one episode",
                "dub": "Switching audio to English Dub", "sub": "Switching audio to Japanese with subs"}.get(a.get("command"), "Adjusting the player")
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
        if a["type"] in ("play", "open", "add_to_list", "remove_from_list"):
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


def _vg_handle_character_inquiry(low, active_anime, kb):
    # 1. Check direct character matches
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
        # 3. Fallback to MAL/AniList-trained character_index across both servers
        char_idx = kb.get("character_index") or {}
        if char_idx and any(k in low for k in ["who is", "whos", "who's", "tell me about", "what anime is", "which anime is", "character"]):
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
                # Try 2-word and 1-word combinations against character_index
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
    is_va_phrase = any(k in low for k in ["voice actor", "seiyuu", "who voices", "who voice", "voiced by", "voice of", "japanese voice", "english voice", "actor"])
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

    # -1. Visual Screenshot Match Response (only auto-confirm high-confidence matches)
    if image_search_result and image_search_result.get("matched_item"):
        it = image_search_result["matched_item"]
        ep = image_search_result.get("episode") or 1
        sec = int(image_search_result.get("from_seconds") or 0)
        sim = int(image_search_result.get("similarity", 0) * 100)
        confident = bool(image_search_result.get("confident"))
        mins = sec // 60
        secs = sec % 60
        time_str = f"{mins}:{secs:02d}"

        if confident:
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
                "title": it["title_english"] or it["title"]
            }
            return reply, action
        else:
            reply = (
                f"Tch! My frame scouter only picked up a weak signal (**{sim}%**). If this is a character portrait, fanart, or cropped image rather than a 16:9 episode frame, my offline scouter cannot guarantee a lock.\n\n"
                f"Closest episode frame candidate: **{it['title_english'] or it['title']}** (Ep {ep} at {time_str}) — "
                f"[Watch on KuroAnime](#anime/{it['anime_id']}?ep={ep})"
            )
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

    # 9. Explicit Recommendations Request (ONLY if user actually asked for recommendations!)
    is_rec_query = bool(re.search(r"\b(recommend|suggest|what should i watch|give me (?:an|some)|top anime|best anime to watch|what to watch|good anime to watch|surprise me)\b", msg_low))
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


@router.get("/ai/status")
def ai_status():
    kb = get_vegeta_full_knowledge()
    env_gemini_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("VITE_GEMINI_API_KEY") or "").strip()
    return {
        "status": "ready",
        "name": "Prince Vegeta Anime Advisor",
        "total_anime": kb["total_anime"],
        "total_episodes": kb["total_episodes"],
        "stats": kb.get("stats", {}),
        "has_gemini_key": bool(env_gemini_key and not env_gemini_key.startswith("0x")),
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
            if not it.get("synopsis") and meta.get("ds"):
                it["synopsis"] = meta["ds"]
                it["synopsis_lower"] = meta["ds"].lower()
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
        else:
            hint = ""
            if image_search_result and image_search_result.get("matched_item"):
                h_it = image_search_result["matched_item"]
                h_sim = int(image_search_result.get("similarity", 0) * 100)
                hint = f" (Weak trace.moe hint {h_sim}%: {h_it.get('title_english') or h_it.get('title')} — DO NOT trust blindly!)"
            blocks.append(
                f"GEMINI VISION IMAGE IDENTIFICATION REQUIRED{hint}:\n"
                f"- The user attached an image (character portrait, fanart, poster, or cropped frame) and trace.moe could not confirm an exact episode frame.\n"
                f"- Examine the character's hair, eyes, outfit, uniform, weapons, markings, and art style using YOUR Gemini Vision.\n"
                f"- Identify the exact character and anime series, then IMMEDIATELY call `search_anime` to look up that anime in KuroAnime's dual-server catalog (ZokoAnime + Munib Server) and give the `[Watch on KuroAnime](#anime/ID)` link!"
            )

    # 0. Reverse Plot / Description Identification Match
    plot_item, plot_title = _find_by_plot_description(msg_low, items)
    if plot_item and add(plot_item):
        blocks.append(
            f"REVERSE PLOT SCOUTING IDENTIFICATION:\n"
            f"- Identified Anime: {_vegeta_line(plot_item, 320).lstrip('- ')}\n"
            f"- Action Directive: Verify if this matches the story/background the user described; if another anime fits better from your knowledge, call `search_anime` for that title instead!"
        )

    # 0.5. Character Index Match (from MyAnimeList/AniList trained character database)
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


@router.post("/ai/chat")
async def ai_chat(req: Request):
    """Prince Vegeta AI Advisor: Gemini grounded in the full KuroAnime catalog (both servers) with conversation memory, reverse image search, and agentic auto-play."""
    try:
        body = await req.json()
    except Exception:
        body = {}

    try:
        from dotenv import load_dotenv
        for _env_file in [".env.local", ".env", "anime_app/web/.env.local", "anime_app/web/.env"]:
            if os.path.exists(_env_file):
                load_dotenv(_env_file, override=True)
    except Exception:
        pass

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

    env_gemini_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("VITE_GEMINI_API_KEY") or "").strip()
    client_key = (body.get("apiKey") or "").strip()
    api_key = env_gemini_key or client_key

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
                "used_gemini": False
            }

    if api_key and not api_key.startswith("0x"):
        context = _vegeta_retrieve(user_msg, history, kb, image_search_result=image_result, has_image=bool(image_data))
        system_instruction = _vegeta_system_prompt(kb, context, user_context)

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
                    # Also parse any <!--ACTION:(.*?)--> in text
                    m = re.search(r"<!--ACTION:(.*?)-->", final_reply)
                    if m:
                        try:
                            act = json.loads(m.group(1).strip())
                            if isinstance(act, dict):
                                collected_actions.append(act)
                            final_reply = re.sub(r"<!--ACTION:.*?-->", "", final_reply).strip()
                        except Exception:
                            pass
                    
                    if not collected_actions:
                        if image_result and image_result.get("matched_item") and image_result.get("confident"):
                            it = image_result["matched_item"]
                            collected_actions.append({
                                "type": "play",
                                "anime_id": it["anime_id"],
                                "episode": image_result.get("episode") or 1,
                                "time": int(image_result.get("from_seconds") or 0),
                                "title": _vg_title(it),
                            })
                    
                    valid_actions = _vg_validate_actions(collected_actions, kb)
                    return {
                        "ok": True,
                        "reply": final_reply,
                        "action": valid_actions[0] if valid_actions else None,
                        "actions": valid_actions,
                        "used_gemini": True,
                        "model": model
                    }
            except Exception as gemini_err:
                print(f"Gemini API error ({model}): {gemini_err}")
                continue

    # If image was uploaded and Gemini Vision was unavailable/failed and trace.moe wasn't confident
    if image_data and not (image_result and image_result.get("matched_item")):
        return {
            "ok": True,
            "reply": (
                "Tch! My offline frame scouter only indexes exact 16:9 anime episode frames — it cannot identify cropped character portraits, fanart, or posters without **Gemini Vision**!\n\n"
                "Set your `GEMINI_API_KEY` on the server (or paste a full 16:9 episode screenshot) and I will lock onto the character and anime instantly."
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
    """Ping external Render URL every 10 minutes so Render Free Tier never sleeps or resets /tmp."""
    url = (os.environ.get("RENDER_EXTERNAL_URL") or "https://kuro-anime.onrender.com").rstrip("/") + "/api/stats"
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
