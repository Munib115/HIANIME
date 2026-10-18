"""
Anime App API — read-only JSON API over hianime.db
Run:  python -m uvicorn api:app --host 0.0.0.0 --port 8000
(0.0.0.0 so a phone on the same Wi-Fi can reach it at http://<PC-LAN-IP>:8000)
"""
import json
import os
import sqlite3
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

DB_PATH = os.environ.get(
    "ANIME_DB",
    os.path.join(os.path.dirname(__file__), "..", "..", "hianime.db"),
)

app = FastAPI(title="Anime App API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

CARD_COLS = """anime_id, title, title_english, type, status, episode_count,
               anilist_score, mal_score, season_year,
               COALESCE(NULLIF(poster_large,''), poster_url) AS poster"""


def db():
    # Read-only + WAL-friendly so the scraper can keep writing while the API reads
    path = os.path.abspath(DB_PATH)
    try:
        uri_path = path.replace("\\", "/")
        if not uri_path.startswith("/"):
            uri_path = "/" + uri_path
        conn = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True, timeout=10)
    except Exception:
        conn = sqlite3.connect(path, timeout=10)
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


@app.get("/api/stats")
def stats():
    return {
        "anime": one("SELECT COUNT(*) n FROM anime")["n"],
        "episodes": one("SELECT COUNT(*) n FROM episodes")["n"],
        "streams": one("SELECT COUNT(*) n FROM streams")["n"],
    }


@app.get("/api/home")
def home():
    spotlight = rows(f"""
        SELECT {CARD_COLS}, banner_image, synopsis, genres
        FROM anime
        WHERE banner_image IS NOT NULL AND banner_image != ''
        ORDER BY anilist_popularity DESC NULLS LAST
        LIMIT 8
    """)
    rails = {
        "trending": rows(f"SELECT {CARD_COLS} FROM anime ORDER BY anilist_popularity DESC NULLS LAST LIMIT 20"),
        "top_rated": rows(f"SELECT {CARD_COLS} FROM anime WHERE anilist_score IS NOT NULL ORDER BY anilist_score DESC LIMIT 20"),
        "airing": rows(f"SELECT {CARD_COLS} FROM anime WHERE status LIKE '%Airing%' OR anilist_status='RELEASING' ORDER BY anilist_popularity DESC NULLS LAST LIMIT 20"),
        "movies": rows(f"SELECT {CARD_COLS} FROM anime WHERE type LIKE '%Movie%' ORDER BY anilist_popularity DESC NULLS LAST LIMIT 20"),
        "recent": rows(f"SELECT {CARD_COLS} FROM anime ORDER BY scraped_at DESC LIMIT 20"),
    }
    for k in rails:
        with_flags(rails[k])
    with_flags(spotlight)
    return {"spotlight": spotlight, "rails": rails}


@app.get("/api/anime/{anime_id}")
def anime_detail(anime_id: int):
    a = one("SELECT * FROM anime WHERE anime_id = ?", (anime_id,))
    if not a:
        raise HTTPException(404, "Anime not found")
    a["poster"] = a.get("poster_large") or a.get("poster_url")
    a["seasons"] = rows("""
        SELECT season_name, season_anime_id, season_poster, is_current
        FROM anime_seasons WHERE parent_anime_id = ? ORDER BY id
    """, (anime_id,))
    # mark which seasons are already available in our DB
    have = {r["anime_id"] for r in rows("SELECT anime_id FROM anime")} if a["seasons"] else set()
    for s in a["seasons"]:
        s["available"] = s["season_anime_id"] in have
    a.update(audio_flags([anime_id]).get(anime_id, {}))
    a.pop("seasons_json", None)
    return a


@app.get("/api/anime/{anime_id}/episodes")
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


@app.get("/api/episode/{episode_id}/streams")
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


@app.get("/api/search")
def search(q: str = Query(..., min_length=1), limit: int = 30):
    like = f"%{q.strip()}%"
    res = rows(f"""
        SELECT {CARD_COLS} FROM anime
        WHERE title LIKE ? OR title_english LIKE ? OR title_romaji LIKE ? OR synonyms LIKE ?
        ORDER BY anilist_popularity DESC NULLS LAST
        LIMIT ?
    """, (like, like, like, like, min(limit, 60)))
    return {"results": with_flags(res)}


@app.get("/api/browse")
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


@app.get("/api/genres")
def genres():
    counts = {}
    for r in rows("SELECT genres FROM anime WHERE genres IS NOT NULL AND genres != ''"):
        for g in r["genres"].split(","):
            g = g.strip()
            if g:
                counts[g] = counts.get(g, 0) + 1
    return {"genres": sorted(counts, key=lambda g: -counts[g])}
