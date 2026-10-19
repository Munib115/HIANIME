"""
KuroAnime API — Vercel Serverless & Standalone FastAPI Backend
Provides JSON API over SQLite hianime.db
"""
import os
import sqlite3
import re
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, APIRouter
from fastapi.middleware.cors import CORSMiddleware

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
        os.path.join(PARENT_DIR, "hianime.db"),
        os.path.join(CURRENT_DIR, "hianime.db"),
        os.path.join(os.getcwd(), "hianime.db"),
        os.path.join(PARENT_DIR, "api", "hianime.db"),
        os.path.join(os.getcwd(), "api", "hianime.db"),
        os.path.join(os.getcwd(), "anime_app", "web", "api", "hianime.db"),
        os.path.join(os.getcwd(), "anime_app", "web", "hianime.db"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return os.path.abspath(c)
    return os.path.join(CURRENT_DIR, "hianime.db")

app = FastAPI(title="KuroAnime API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
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

    # On Lambda / Vercel, copy to /tmp so SQLite has full read/lock access without journal errors
    if os.name != 'nt' and os.path.exists('/tmp'):
        target = '/tmp/hianime.db'
        try:
            if not os.path.exists(target) or os.path.getsize(target) != os.path.getsize(source):
                import shutil
                shutil.copyfile(source, target)
            _INITIALIZED_DB = target
            return target
        except Exception:
            pass

    _INITIALIZED_DB = source
    return source


def db():
    path = get_db_path()
    try:
        conn = sqlite3.connect(path, timeout=10)
    except Exception:
        uri_path = path.replace("\\", "/")
        conn = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn




def rows(sql, params=()):
    with db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(sql, params=()):
    with db() as conn:
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else None


def audio_flags(anime_ids):
    """Return {anime_id: {'sub': bool, 'dub': bool}} for card badges."""
    if not anime_ids:
        return {}
    q = ",".join("?" * len(anime_ids))
    out = {i: {"sub": False, "dub": False} for i in anime_ids}
    for r in rows(f"SELECT DISTINCT anime_id, type FROM streams WHERE anime_id IN ({q})", anime_ids):
        if r["type"] in ("sub", "dub"):
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
    return {
        "status": "ok",
        "db_path": db_file,
        "db_exists": os.path.exists(db_file),
        "db_size": os.path.getsize(db_file) if os.path.exists(db_file) else 0,
        "files_here": os.listdir(CURRENT_DIR) if os.path.exists(CURRENT_DIR) else [],
    }


@router.get("/stats")
def stats():

    return {
        "anime": one("SELECT COUNT(*) n FROM anime")["n"],
        "episodes": one("SELECT COUNT(*) n FROM episodes")["n"],
        "streams": one("SELECT COUNT(*) n FROM streams")["n"],
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


@router.get("/anime/{anime_id}")
def anime_detail(anime_id: int):
    a = one("SELECT * FROM anime WHERE anime_id = ?", (anime_id,))
    if not a:
        raise HTTPException(404, "Anime not found")
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
    return {"anime_id": anime_id, "episodes": eps}


@router.get("/episode/{episode_id}/streams")
def episode_streams(episode_id: str, anime_id: Optional[int] = None):
    sql = "SELECT type, server_name, provider, embed_url FROM streams WHERE episode_id = ? AND server_name != 'HD-1' AND provider NOT LIKE '%megaplay%'"
    params = [episode_id]
    if anime_id is not None:
        sql += " AND anime_id = ?"
        params.append(anime_id)
    data = rows(sql + " ORDER BY type, CASE WHEN server_name = 'ZokoAnime' THEN 0 ELSE 1 END, server_name", params)
    return {
        "sub": [s for s in data if s["type"] == "sub"],
        "dub": [s for s in data if s["type"] == "dub"],
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
                
    return {"results": with_flags(res[:min(limit, 60)])}


@router.get("/browse")
def browse(
    genre: Optional[str] = None,
    type: Optional[str] = None,
    sort: str = "popular",
    page: int = 1,
    per_page: int = 30,
):
    where, params = [], []
    if genre:
        where.append("(genres LIKE ? OR al_genres LIKE ?)")
        params += [f"%{genre}%", f"%{genre}%"]
    if type:
        where.append("type LIKE ?")
        params.append(f"%{type}%")
    order = {
        "popular": "anilist_popularity DESC NULLS LAST",
        "score": "anilist_score DESC NULLS LAST",
        "az": "title COLLATE NOCASE ASC",
        "new": "season_year DESC NULLS LAST",
    }.get(sort, "anilist_popularity DESC NULLS LAST")
    per_page = max(1, min(per_page, 60))
    sql = f"SELECT {CARD_COLS} FROM anime"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
    res = rows(sql, params + [per_page, (max(page, 1) - 1) * per_page])
    return {"page": page, "results": with_flags(res)}


@router.get("/genres")
def genres():
    counts = {}
    for r in rows("SELECT genres FROM anime WHERE genres IS NOT NULL AND genres != ''"):
        for g in r["genres"].split(","):
            g = g.strip()
            if g:
                counts[g] = counts.get(g, 0) + 1
    return {"genres": sorted(counts, key=lambda g: -counts[g])}


# Mount both /api and root routes so that rewrites work seamlessly
app.include_router(router, prefix="/api")
app.include_router(router)
