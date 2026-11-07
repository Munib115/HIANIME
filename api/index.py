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
          AND (title NOT LIKE '%Special%' AND title NOT LIKE '%OVA%' AND title NOT LIKE '%Movie 1%')
        ORDER BY anilist_popularity DESC NULLS LAST
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
    return {"spotlight": spotlight, "rails": rails}


MUNIB_CARD_COLS = """anime_id, title, title_english, type, status, episode_count,
                     anilist_score, NULL AS mal_score, season_year,
                     COALESCE(NULLIF(poster_large,''), poster_url) AS poster"""


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
    a.pop("seasons_json", None)
    return a


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
                   synopsis, al_description, seasons_json
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
        by_id[a_id] = item
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


def _find_exact_or_fuzzy_title(user_msg: str, kb_items: list):
    """High-precision matching against all 5,608 anime titles, aliases, and acronyms."""
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
            elif check_phrase in t:
                ratio = len(check_phrase) / max(len(t), 1)
                best_title_score = max(best_title_score, 70.0 + ratio * 20.0)
            elif t in check_phrase and len(t) >= 4:
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


def _generate_vegeta_response(user_msg: str, kb: dict) -> str:
    """Generate in-character Prince Vegeta response grounded in all 5,608 anime and their complete seasons."""
    items = kb["items"]
    total_anime = kb["total_anime"]
    total_episodes = kb["total_episodes"]
    msg_low = user_msg.lower().strip()

    is_seasons_query = any(k in msg_low for k in ["season", "seasons", "watch order", "chronological", "order", "sequel", "prequel", "movie", "movies", "parts", "part 1", "part 2", "how many seasons"])

    # 1. Total Catalog / System stats inquiry
    if any(k in msg_low for k in ["how many anime", "how many shows", "total anime", "catalog size", "all anime", "who are you"]):
        if "who are you" in msg_low:
            return (
                f"## Prince Vegeta • The Supreme Saiyan Anime Advisor\n"
                f"- **Identity:** Prince of all Saiyans and royal arbiter of KuroAnime's archives.\n"
                f"- **Catalog Mastery:** I oversee all **{total_anime:,} anime series**, every chronological season, and **{total_episodes:,} streaming episodes** across this platform.\n"
                f"- **The Prince's Standard:** I do not tolerate pathetic filler or weakling storylines. Tell me what anime, season, or battle you hunger for, and I will summon true masterpiece fiction!\n\n"
                f"> Tip: Name any anime title or ask about seasons, and I will present its data, complete chronological release order, and direct stream links!"
            )
        return (
            f"## KuroAnime Master Archives\n"
            f"- **Total Catalog:** **{total_anime:,} Anime Titles** verified in high definition.\n"
            f"- **Episode Archive:** Over **{total_episodes:,} Sub & Dub streaming episodes** available with zero advertisements.\n"
            f"- **Seasons Database:** Over 6,800 chronological season relationships fully mapped.\n"
            f"- **Status:** Every single title is cataloged under my royal inspection. Name any show—from legendary classics to brand-new seasonal simulcasts—and I will bring you its full dossier and season watch order!\n"
        )

    # 2. Complete Genres Inquiry
    if any(k in msg_low for k in ["what genres", "list genres", "all genres", "show genres", "which genres"]):
        return (
            f"## KuroAnime Complete Genre Directory\n"
            f"As Prince of all Saiyans, I maintain absolute command over every genre recorded across our **{total_anime:,} anime** archives:\n\n"
            f"- **Battle & Adrenaline:** Action, Shounen, Martial Arts, Super Power, Samurai, Military, Mecha\n"
            f"- **Dark & Gripping:** Dark Fantasy, Psychological, Thriller, Suspense, Horror, Gore, Demons, Vampire\n"
            f"- **High Fantasy & Worlds:** Fantasy, Isekai, Reincarnation, Magic, Mahou Shoujo, Mythology, Urban Fantasy, Space, Sci-Fi, Cyberpunk\n"
            f"- **Mystery & Intrigue:** Mystery, Detective, Police, Time Travel, Survival, Game\n"
            f"- **Heart & Emotion:** Drama, Romance, Slice of Life, School, Workplace, Iyashikei, Adult Cast, Shoujo, Josei\n"
            f"- **Entertainment & Skill:** Sports, Comedy, Parody, Music, Seinen, CGDCT\n\n"
            f"Tell me which category you demand, and I will present its undisputed champions!"
        )

    # 3. Specific anime title / seasons inquiry (tell me about anime when asked)
    if not _is_pure_genre_query(msg_low):
        specific, related = _find_exact_or_fuzzy_title(user_msg, items)
        if specific:
            title_main = specific["title_english"] if specific["title_english"] != specific["title"] else specific["title"]
            alt_titles = []
            if specific["title_romaji"] and specific["title_romaji"] != title_main:
                alt_titles.append(specific["title_romaji"])
            if specific["title"] != title_main:
                alt_titles.append(specific["title"])
            alt_titles_str = ", ".join(alt_titles[:2])
            alt_str = f" *({alt_titles_str})*" if alt_titles else ""

            tier = "Masterpiece" if specific["score"] >= 8.5 else ("Elite" if specific["score"] >= 7.8 else "Standard Warrior Tier")

            syn = specific["synopsis"]
            if not syn or len(syn) < 30:
                syn = "A renowned saga recorded in the KuroAnime chronicles. Prepare your spirit and witness every battle firsthand."

            seasons_list = specific.get("seasons", [])
            out = [
                f"## {title_main}{alt_str}",
                f"- **Rating:** ★ {specific['score']}/10 ({tier})",
                f"- **Format:** {specific['episode_count']} Episodes • {specific['type']} ({specific['status']}) | **Studio:** {specific['studios']}",
                f"- **Genres:** {specific['genres']}",
                f"- **The Prince's Dossier & Plot:** {syn}",
            ]

            # Complete Chronological Seasons List
            if seasons_list and len(seasons_list) > 1:
                out.append("### Complete Chronological Seasons & Watch Order:")
                for s_idx, s in enumerate(seasons_list, 1):
                    cur_mark = " *(Current / Queried)*" if s.get("is_current") else ""
                    out.append(f"{s_idx}. **{s['season_name']}** • {s['title']} ({s['episodes']} Episodes, {s['format']}) • [Watch on KuroAnime](#anime/{s['anime_id']}){cur_mark}")
                out.append("")
            elif specific.get("related_franchise"):
                out.append("### Related Adaptations & Releases:")
                for rel in specific["related_franchise"][:4]:
                    rel_title = rel["title_english"] or rel["title"]
                    out.append(f"- **{rel_title}** ({rel['type']}, {rel['episode_count']} eps, ★ {rel['score']}) • [Watch on KuroAnime](#anime/{rel['anime_id']})")
                out.append("")

            out.extend([
                f"- **The Prince's Verdict:** A battle saga recorded in our archives with supreme pride. Power through every episode without delay!",
                f"- `[Watch on KuroAnime](#anime/{specific['anime_id']})`\n",
                "Now cease your hesitation and start streaming! A true warrior never leaves peak fiction unwatched."
            ])
            return "\n".join(out)

    # 4. Genre / Category / Popular Recommendations Inquiry (e.g. 'i want to watch action anime')
    detected_genres = _detect_query_genres(msg_low)
    by_franchise = {}
    
    if detected_genres:
        genre_label = ", ".join(g.title() for g in detected_genres)
        for it in items:
            is_match = False
            for dg in detected_genres:
                syns = GENRE_MAP.get(dg, [dg])
                if dg in it["genres_lower"]:
                    is_match = True
                    break
                if any(s in it["genres_lower"] for s in syns):
                    is_match = True
                    break
            if not is_match:
                continue
            b = _clean_base_title(it["title_english"] or it["title"])
            if b not in by_franchise or (it.get("popularity", 0) > by_franchise[b].get("popularity", 0)):
                by_franchise[b] = it
    else:
        genre_label = "Popular Masterpieces"
        for it in items:
            if it.get("score", 0) < 7.8:
                continue
            b = _clean_base_title(it["title_english"] or it["title"])
            if b not in by_franchise or (it.get("popularity", 0) > by_franchise[b].get("popularity", 0)):
                by_franchise[b] = it

    cands = list(by_franchise.values())
    cands.sort(key=lambda x: (x.get("popularity", 0) or 0, x.get("score", 0) or 0), reverse=True)
    top_candidates = cands[:4] if cands else items[:4]

    out = [
        f"Hmph! Listen well, warrior! As Prince of all Saiyans, my standards for combat and power are absolute. Here are the most popular, elite champions of **{genre_label}** on KuroAnime:\n"
    ]

    for i, it in enumerate(top_candidates, 1):
        tier = "Masterpiece" if it["score"] >= 8.8 else ("Elite" if it["score"] >= 8.0 else "Top Tier")
        s_count_note = ""
        if it.get("seasons") and len(it["seasons"]) > 1:
            s_count_note = f" • {len(it['seasons'])} Seasons"
        out.append(f"## {i}. {it['title_english'] or it['title']}")
        out.append(f"- **Rating:** ★ {it['score']}/10 ({tier})")
        out.append(f"- **Format:** {it['episode_count']} Eps{s_count_note} • {it['type']} ({it['status']}) | **Studio:** {it['studios']}")
        out.append(f"- **Genres:** {it['genres']}")
        syn = it["synopsis"][:230].rsplit(" ", 1)[0] + "..." if len(it["synopsis"]) > 230 else it["synopsis"]
        out.append(f"- **The Prince's Verdict:** {syn}")
        out.append(f"- `[Watch on KuroAnime](#anime/{it['anime_id']})`\n")

    out.append("Now stop wasting time and start streaming! A true warrior never settles for mediocre filler.")
    return "\n".join(out)


@router.get("/ai/status")
def ai_status():
    kb = get_vegeta_knowledge_base()
    env_gemini_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("VITE_GEMINI_API_KEY") or "").strip()
    return {
        "status": "ready",
        "name": "Prince Vegeta Anime Advisor",
        "total_anime": kb["total_anime"],
        "total_episodes": kb["total_episodes"],
        "has_gemini_key": bool(env_gemini_key and not env_gemini_key.startswith("0x")),
    }


@router.post("/ai/chat")
async def ai_chat(req: Request):
    """Prince Vegeta AI Advisor trained with Google Gemini on all 5,608 anime and their complete seasons."""
    try:
        body = await req.json()
    except Exception:
        body = {}

    # Auto-reload .env.local so keys take effect immediately
    try:
        from dotenv import load_dotenv
        for _env_file in [".env.local", ".env", "anime_app/web/.env.local", "anime_app/web/.env"]:
            if os.path.exists(_env_file):
                load_dotenv(_env_file, override=True)
    except Exception:
        pass

    user_msg = (body.get("message") or "").strip()
    env_gemini_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("VITE_GEMINI_API_KEY") or "").strip()
    client_key = (body.get("apiKey") or "").strip()
    api_key = env_gemini_key or client_key
    if api_key.startswith("0x") and env_gemini_key:
        api_key = env_gemini_key

    if not user_msg:
        raise HTTPException(status_code=400, detail="Message is required")

    kb = get_vegeta_knowledge_base()
    items = kb["items"]
    msg_low = user_msg.lower().strip()

    # If Gemini API Key is present, invoke Google Gemini with deep anime & seasons training prompt
    if api_key and not api_key.startswith("0x"):
        is_genre_query = _is_pure_genre_query(msg_low)
        specific = None
        related = []
        if not is_genre_query:
            specific, related = _find_exact_or_fuzzy_title(user_msg, items)

        detected_genres = _detect_query_genres(msg_low)

        if specific:
            # SPECIFIC ANIME + SEASONS MASTER DOSSIER
            target = specific
            target_name = target["title_english"] or target["title"]
            seasons_list = target.get("seasons", [])

            seasons_formatted = []
            if seasons_list and len(seasons_list) > 1:
                for s_idx, s in enumerate(seasons_list, 1):
                    cur_marker = " (Queried / Current)" if s.get("is_current") else ""
                    seasons_formatted.append(
                        f"{s_idx}. **{s['season_name']}** • '{s['title']}' | "
                        f"Format: {s['format']} ({s['episodes']} Episodes, {s['status']}) | "
                        f"Score: ★ {s['score']}/10 | Link: [Watch on KuroAnime](#anime/{s['anime_id']}){cur_marker}"
                    )
            elif target.get("related_franchise"):
                for r_idx, r in enumerate(target["related_franchise"][:5], 1):
                    seasons_formatted.append(
                        f"{r_idx}. '{r['title_english'] or r['title']}' | "
                        f"Format: {r['type']} ({r['episode_count']} Episodes, ★ {r['score']}) | "
                        f"Link: [Watch on KuroAnime](#anime/{r['anime_id']})"
                    )

            seasons_text = "\n".join(seasons_formatted) if seasons_formatted else "Single standalone complete series on KuroAnime."

            seasons_block = f"### Complete Chronological Seasons & Watch Order:\n{seasons_text}\n\n" if seasons_formatted else ""

            system_instruction = f"""You are Prince Vegeta, the proud, powerful, and elite Prince of all Saiyans from Dragon Ball. You are acting as the Supreme AI Anime Advisor on KuroAnime (an ad-free streaming platform featuring 5,608 anime and 79,970+ streaming episodes).

ROYAL PERSONA & MANNERISMS:
- Speak with fierce Saiyan pride, confidence, authority, and witty banter ("Hmph!", "Listen well, warrior!", "Only the strong survive in this domain!").
- While boastful, you possess master, omniscient knowledge of every anime, its characters, powers, animation studios, and every single season and movie.
- Always stay strictly in character as Prince Vegeta.
- Deliver rich, exciting, in-depth information. Never give lazy 2-sentence answers.
- Format cleanly in GitHub-flavored Markdown without decorative icons.
- ALWAYS include exact KuroAnime watch links for the main anime AND for each season/movie listed using: `[Watch on KuroAnime](#anime/{{ID}})`.

MANDATORY RESPONSE SECTIONS:
## {target_name}
- ★ {target['score']}/10 (Masterpiece / Elite / High Tier)
- **Format:** {target['episode_count']} Episodes • {target['type']} ({target['status']}) | **Studio:** {target['studios']}
- **Genres:** {target['genres']}
- **The Prince's Dossier & Plot:** [Deep dive into what this anime is about: premise, protagonist, their power, antagonists, high stakes, combat, and why it is thrilling]
{seasons_block}- **The Prince's Royal Verdict:** [Vegeta's honest judgment on whether a true warrior should stream it immediately]
- `[Watch on KuroAnime](#anime/{target['anime_id']})`
End with a proud Vegeta signoff commanding them to watch!

TARGET ANIME DATA:
- Title: {target['title_english']} (Romaji: {target['title_romaji']}, Original: {target['title']})
- Score: ★ {target['score']}/10 | Popularity: {target['popularity']:,}
- Format: {target['type']} ({target['episode_count']} Episodes, Status: {target['status']})
- Studio: {target['studios']} | Year/Season: {target['premiered'] or target['year']}
- Genres: {target['genres']}
- Full Synopsis: {target['synopsis']}
- Main Watch Link: [Watch on KuroAnime](#anime/{target['anime_id']})

COMPLETE CHRONOLOGICAL SEASONS ON KUROANIME:
{seasons_text}
"""
        else:
            # GENRE / POPULAR RECOMMENDATION PROMPT
            by_franchise = {}
            if detected_genres:
                genre_label = ", ".join(g.title() for g in detected_genres)
                for it in items:
                    is_match = False
                    for dg in detected_genres:
                        syns = GENRE_MAP.get(dg, [dg])
                        if dg in it["genres_lower"]:
                            is_match = True
                            break
                        if any(s in it["genres_lower"] for s in syns):
                            is_match = True
                            break
                    if not is_match:
                        continue
                    b = _clean_base_title(it["title_english"] or it["title"])
                    if b not in by_franchise or (it.get("popularity", 0) > by_franchise[b].get("popularity", 0)):
                        by_franchise[b] = it
            else:
                genre_label = "Popular Masterpieces"
                for it in items:
                    if it.get("score", 0) < 7.8:
                        continue
                    b = _clean_base_title(it["title_english"] or it["title"])
                    if b not in by_franchise or (it.get("popularity", 0) > by_franchise[b].get("popularity", 0)):
                        by_franchise[b] = it

            cands = list(by_franchise.values())
            cands.sort(key=lambda x: (x.get("popularity", 0) or 0, x.get("score", 0) or 0), reverse=True)
            top_cands = cands[:5]

            catalog_items = []
            for c in top_cands:
                s_note = f", {len(c['seasons'])} Seasons" if c.get("seasons") and len(c["seasons"]) > 1 else ""
                catalog_items.append(
                    f"- [ID: {c['anime_id']}] '{c['title_english'] or c['title']}' | "
                    f"Score: ★ {c['score']}/10 | Popularity: {c['popularity']:,} | Format: {c['type']} ({c['episode_count']} eps{s_note}) | "
                    f"Studio: {c['studios']} | Genres: {c['genres']} | Synopsis: {c['synopsis'][:220]}..."
                )
            catalog_str = "\n".join(catalog_items)

            system_instruction = f"""You are Prince Vegeta, the proud, powerful, and elite Prince of all Saiyans from Dragon Ball. You are acting as the Royal AI Anime Advisor on KuroAnime (an ad-free streaming platform with 5,608 anime and 79,970+ streaming episodes).

ROYAL PERSONA:
- Speak with fierce Saiyan pride, authority, and witty banter ("Hmph!", "Listen well, warrior!", "A true warrior demands peak fiction!").
- While boastful, you have master knowledge of all anime. Always stay in character as Prince Vegeta.
- Deliver rich, exciting, in-depth information. Format cleanly in Markdown without decorative icons.
- ALWAYS include exact KuroAnime watch links for every recommendation: `[Watch on KuroAnime](#anime/{{ID}})`.

USER REQUEST: "{user_msg}"
Present the top {genre_label} anime champions from the verified KuroAnime catalog below. For each anime, describe its premise, battles, episode count/seasons, and provide its exact watch link!

VERIFIED KUROANIME CANDIDATES:
{catalog_str}
"""

        # Gemini model chain: try primary gemini-2.5-flash, fallback to flash-latest, 2.5-pro, etc.
        models = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest", "gemini-2.5-pro", "gemini-pro-latest"]
        for model in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            payload = {
                "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
                "systemInstruction": {"parts": [{"text": system_instruction}]},
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 2048,
                    "thinkingConfig": {"thinkingBudget": 0}
                }
            }
            try:
                res = _requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=20)
                if res.status_code == 200:
                    data = res.json()
                    candidates_list = data.get("candidates") or []
                    if candidates_list:
                        reply_text = candidates_list[0]["content"]["parts"][0]["text"]
                        return {
                            "ok": True,
                            "reply": reply_text,
                            "used_gemini": True,
                            "model": model,
                        }
                else:
                    print(f"Gemini API returned {res.status_code} for {model}: {res.text[:200]}")
            except Exception as gemini_err:
                print(f"Gemini API error ({model}): {gemini_err}")
                continue

    # High-level local Vegeta brain fallback (grounded in all 5,608 anime and their complete seasons)
    reply_text = _generate_vegeta_response(user_msg, kb)
    return {
        "ok": True,
        "reply": reply_text,
        "used_gemini": False,
    }


app.include_router(router, prefix="/api")
app.include_router(router)
