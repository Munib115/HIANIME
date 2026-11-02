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
from dotenv import load_dotenv

# Auto-load .env.local and .env
for _env_file in ['.env.local', '.env', 'anime_app/web/.env.local', 'anime_app/web/.env']:
    if os.path.exists(_env_file):
        load_dotenv(_env_file, override=True)
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
    status: Optional[str] = None,
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
    if status:
        st = status.lower()
        if "air" in st or "releas" in st:
            where.append("(status LIKE '%Currently Airing%' OR anilist_status='RELEASING' OR category='airing') AND status NOT LIKE '%Finished%' AND (anilist_status IS NULL OR anilist_status != 'FINISHED')")
        elif "comp" in st or "finish" in st:
            where.append("(status LIKE '%Finished%' OR anilist_status='FINISHED')")
    order = {
        "popular": "anilist_popularity DESC NULLS LAST",
        "score": "anilist_score DESC NULLS LAST",
        "az": "title COLLATE NOCASE ASC",
        "new": "season_year DESC NULLS LAST, anilist_popularity DESC NULLS LAST",
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
    "ds": "demon slayer",
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

    if hasattr(conn_or_fn, "cursor"):
        conn = conn_or_fn
    elif callable(conn_or_fn):
        conn = conn_or_fn()
    else:
        conn = db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM episodes")
    total_episodes = c.fetchone()[0]

    c.execute("""
        SELECT anime_id, title, title_english, title_romaji, synonyms,
               type, status, premiered, season, season_year,
               mal_score, anilist_score, anilist_popularity,
               studios, duration, episode_count, genres, al_genres,
               synopsis, al_description
        FROM anime
    """)
    rows = c.fetchall()

    items = []
    for r in rows:
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

        studios = (r[13] or "").strip()
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
        all_titles_norm = [t for t in dict.fromkeys(all_titles_norm) if t]

        items.append({
            "anime_id": a_id,
            "title": title,
            "title_english": title_eng,
            "title_romaji": title_rom,
            "synonyms": synonyms,
            "all_titles_norm": all_titles_norm,
            "franchise": _franchise_key(title_eng or title),
            "type": a_type,
            "status": status,
            "year": year_str,
            "season_year": int(year_str) if year_str.isdigit() else 0,
            "score": score,
            "popularity": pop,
            "studios": studios or "Animation Studio",
            "studios_lower": studios.lower(),
            "duration": duration,
            "episode_count": ep_count,
            "genres": genres_display,
            "genres_lower": genres_lower,
            "synopsis": syn_clean,
            "synopsis_lower": syn_clean.lower(),
        })

    # Sort items by score and popularity so top anime rank highest by default
    items.sort(key=lambda x: (x["score"], x["popularity"]), reverse=True)

    _VEGETA_KB = {
        "items": items,
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
    """High-precision matching against all 5,564 anime titles, aliases, and acronyms."""
    q = re.sub(r"[^\w\s]", " ", user_msg.lower()).strip()
    q = re.sub(r"^(tell me about|tell about|can you tell me about|i want to know about|what is|what s|what about|how about|info on|information on|synopsis for|synopsis of|details about|details on|how many episodes in|how many episodes does|who made|who animated|is|are there|can you find|have you got|do you have|watch|stream|search|anime|explain|describe|review of)\\s+", "", q).strip()
    q = re.sub(r"\s+(about|anime|show|series|season|have|website|kuroanime|all about|on this site|on here|good|worth watching|worth it|any good)$", "", q).strip()

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
    """Generate in-character Prince Vegeta response grounded in all 5,564 anime."""
    items = kb["items"]
    total_anime = kb["total_anime"]
    total_eps = kb["total_episodes"]
    msg_low = user_msg.lower().strip()

    # 1. Total Catalog / System stats inquiry
    if any(k in msg_low for k in ["how many anime", "how many shows", "total anime", "catalog size", "all anime", "who are you"]):
        if "who are you" in msg_low:
            return (
                f"## Prince Vegeta • The Supreme Saiyan Anime Advisor\\n"
                f"- **Identity:** Prince of all Saiyans and royal arbiter of KuroAnime's archives.\\n"
                f"- **Catalog Mastery:** I oversee all **{total_anime:,} anime series** and **{total_eps:,} streaming episodes** across this platform.\\n"
                f"- **The Prince's Standard:** I do not tolerate pathetic filler or weakling storylines. Tell me what genre, studio, or battle you hunger for, and I will summon true masterpiece fiction!\\n\\n"
                f"> Tip: Name any anime title or genre, and I will present its data and direct stream link!"
            )
        return (
            f"## KuroAnime Master Archives\\n"
            f"- **Total Catalog:** **{total_anime:,} Anime Titles** verified in high definition.\\n"
            f"- **Episode Archive:** Over **{total_eps:,} Sub & Dub streaming episodes** available with zero advertisements.\\n"
            f"- **Status:** Every single title is cataloged under my royal inspection. Name any show—from legendary classics to brand-new seasonal simulcasts—and I will bring you its full dossier!\\n"
        )

    # 2. Complete Genres Inquiry
    if any(k in msg_low for k in ["what genres", "list genres", "all genres", "show genres", "which genres"]):
        return (
            f"## KuroAnime Complete Genre Directory\\n"
            f"As Prince of all Saiyans, I maintain absolute command over every genre recorded across our **{total_anime:,} anime** archives:\\n\\n"
            f"- **Battle & Adrenaline:** Action, Shounen, Martial Arts, Super Power, Samurai, Military, Mecha\\n"
            f"- **Dark & Gripping:** Dark Fantasy, Psychological, Thriller, Suspense, Horror, Gore, Demons, Vampire\\n"
            f"- **High Fantasy & Worlds:** Fantasy, Isekai, Reincarnation, Magic, Mahou Shoujo, Mythology, Urban Fantasy, Space, Sci-Fi, Cyberpunk\\n"
            f"- **Mystery & Intrigue:** Mystery, Detective, Police, Time Travel, Survival, Game\\n"
            f"- **Heart & Emotion:** Drama, Romance, Slice of Life, School, Workplace, Iyashikei, Adult Cast, Shoujo, Josei\\n"
            f"- **Entertainment & Skill:** Sports, Comedy, Parody, Music, Seinen, CGDCT\\n\\n"
            f"Tell me which category you demand, and I will present its undisputed champions!"
        )

    # 3. Specific anime title inquiry (tell me about anime when ask)
    if not _is_pure_genre_query(msg_low):
        specific, related = _find_exact_or_fuzzy_title(user_msg, items)
        if specific:
            title_main = specific["title_english"] if specific["title_english"] != specific["title"] else specific["title"]
            alt_titles = []
            if specific["title_romaji"] and specific["title_romaji"] != title_main:
                alt_titles.append(specific["title_romaji"])
            if specific["title"] != title_main:
                alt_titles.append(specific["title"])
            alt_str = f" *({', '.join(alt_titles[:2])})*" if alt_titles else ""

            tier = "Masterpiece" if specific["score"] >= 8.5 else ("Elite" if specific["score"] >= 7.8 else "Standard Warrior Tier")

            syn = specific["synopsis"]
            if not syn or len(syn) < 30:
                syn = "A renowned saga recorded in the KuroAnime chronicles. Prepare your spirit and witness every battle firsthand."

            out = [
                f"## {title_main}{alt_str}",
                f"- **Rating:** ★ {specific['score']}/10 ({tier})",
                f"- **Format:** {specific['episode_count']} Episodes • {specific['type']} ({specific['status']}) | **Studio:** {specific['studios']}",
                f"- **Genres:** {specific['genres']}",
                f"- **The Prince's Dossier & Plot:** {syn}",
                f"- **The Prince's Verdict:** A battle saga recorded in our archives with supreme pride. Power through every episode without delay!",
                f"- `[Watch on KuroAnime](#anime/{specific['anime_id']})`\\n"
            ]

            if related:
                out.append("### Related Adaptations & Releases:")
                for rel in related[:3]:
                    rel_title = rel["title_english"] or rel["title"]
                    out.append(f"- **{rel_title}** ({rel['type']}, {rel['episode_count']} eps, ★ {rel['score']}) • [Watch on KuroAnime](#anime/{rel['anime_id']})")
                out.append("")

            out.append("Now cease your hesitation and start streaming! A true warrior never leaves peak fiction unwatched.")
            return "\\n".join(out)

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
    # Sort primarily by popularity, secondarily by score
    cands.sort(key=lambda x: (x.get("popularity", 0) or 0, x.get("score", 0) or 0), reverse=True)
    top_candidates = cands[:4] if cands else items[:4]

    out = [
        f"Hmph! Listen well, warrior! As Prince of all Saiyans, my standards for combat and power are absolute. Here are the most popular, elite champions of **{genre_label}** on KuroAnime:\\n"
    ]

    for i, it in enumerate(top_candidates, 1):
        tier = "Masterpiece" if it["score"] >= 8.8 else ("Elite" if it["score"] >= 8.0 else "Top Tier")
        out.append(f"## {i}. {it['title_english'] or it['title']}")
        out.append(f"- **Rating:** ★ {it['score']}/10 ({tier})")
        out.append(f"- **Format:** {it['episode_count']} Eps • {it['type']} ({it['status']}) | **Studio:** {it['studios']}")
        out.append(f"- **Genres:** {it['genres']}")
        syn = it["synopsis"][:230].rsplit(" ", 1)[0] + "..." if len(it["synopsis"]) > 230 else it["synopsis"]
        out.append(f"- **The Prince's Verdict:** {syn}")
        out.append(f"- `[Watch on KuroAnime](#anime/{it['anime_id']})`\\n")

    out.append("Now stop wasting time and start streaming! A true warrior never settles for mediocre filler.")
    return "\\n".join(out)


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
    """Prince Vegeta AI Advisor powered by Google Gemini with instant High-Level 5,564 Knowledge Base Fallback."""
    try:
        body = await req.json()
    except Exception:
        body = {}

    # Auto-reload .env.local so changes take effect immediately
    for _env_file in [".env.local", ".env", "anime_app/web/.env.local", "anime_app/web/.env"]:
        if os.path.exists(_env_file):
            load_dotenv(_env_file, override=True)

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

    # If Gemini API Key is present, try Gemini API with specialized intent handling
    if api_key and not api_key.startswith("0x"):
        is_genre_query = _is_pure_genre_query(msg_low)
        specific = None
        related = []
        if not is_genre_query:
            specific, related = _find_exact_or_fuzzy_title(user_msg, items)

        detected_genres = _detect_query_genres(msg_low)

        if specific:
            # SPECIFIC ANIME DOSSIER PROMPT
            target = specific
            target_name = target["title_english"] or target["title"]
            target_str = (
                f"- [ID: {target['anime_id']}] '{target['title']}' (English: '{target['title_english']}') | "
                f"Score: ★ {target['score']}/10 | Popularity: {target['popularity']:,} | "
                f"Format: {target['type']} ({target['episode_count']} eps, {target['status']}) | "
                f"Studio: {target['studios']} | Genres: {target['genres']} | Full Synopsis: {target['synopsis']}"
            )
            related_items = []
            for r in related[:3]:
                related_items.append(f"- [ID: {r['anime_id']}] '{r['title_english'] or r['title']}' ({r['type']}, ★ {r['score']})")
            related_str = "\\n".join(related_items) if related_items else "None"

            system_instruction = f"""You are Prince Vegeta, the proud, powerful, and elite Prince of all Saiyans from Dragon Ball. You are acting as the AI Anime Advisor on KuroAnime (an ad-free streaming platform with 5,500+ anime).

CORE PERSONALITY & TONE:
- Speak with fierce Saiyan pride, confidence, authority, and witty banter ("Hmph!", "Listen well, warrior!", "Only the strong survive!").
- While boastful, you have master knowledge of all anime. Always stay in character as Prince Vegeta.

TASK:
The user is asking you to tell them all about the anime: "{target_name}".
Provide a complete, detailed, exciting briefing explaining what this anime is about, its main characters, the conflict, the battles, the stakes, and what makes it extraordinary.

RESPONSE FORMATTING (STRICT REQUIREMENT):
Format your response cleanly in Markdown without decorative icons:
## {target_name}
- ★ {target['score']}/10 (Masterpiece / High Tier / Elite)
- **Format:** {target['episode_count']} Episodes • {target['type']} ({target['status']}) | **Studio:** {target['studios']}
- **Genres:** {target['genres']}
- **The Prince's Dossier & Plot:** [Detail what this anime is about: premise, main character, stakes, battles, and why it is thrilling]
- **The Prince's Verdict:** [Vegeta's honest judgment on whether a true warrior should stream it]
- `[Watch on KuroAnime](#anime/{target['anime_id']})`
{f"- Related Releases: mention related seasons or movies from KuroAnime archives with their watch links: `[Watch on KuroAnime](#anime/{{ID}})`" if related_items else ""}
- End with a proud Vegeta signoff commanding them to watch!

TARGET ANIME DATA:
{target_str}

RELATED KUROANIME ARCHIVES:
{related_str}
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
                catalog_items.append(
                    f"- [ID: {c['anime_id']}] '{c['title_english'] or c['title']}' | "
                    f"Score: ★ {c['score']}/10 | Popularity: {c['popularity']:,} | Format: {c['type']} ({c['episode_count']} eps) | "
                    f"Studio: {c['studios']} | Genres: {c['genres']} | Synopsis: {c['synopsis'][:200]}..."
                )
            catalog_str = "\\n".join(catalog_items)

            system_instruction = f"""You are Prince Vegeta, the proud, powerful, and elite Prince of all Saiyans from Dragon Ball. You are acting as the AI Anime Advisor on KuroAnime (an ad-free streaming platform with 5,500+ anime).

CORE PERSONALITY & TONE:
- Speak with fierce Saiyan pride, confidence, authority, and witty banter ("Hmph!", "Listen well, warrior!", "A true warrior demands peak fiction!").
- While boastful, you have master knowledge of all anime. Always stay in character as Prince Vegeta.

TASK:
The user says: "{user_msg}".
Present the top popular {genre_label} anime champions from the verified KuroAnime catalog below.

RESPONSE FORMATTING (STRICT REQUIREMENT):
Format your response cleanly in Markdown without decorative icons:
For each recommendation, use:
## 1. [Anime Title] (or 2., 3., etc.)
- ★ [Score]/10 (Masterpiece / High Tier / Elite)
- **Format:** [Episodes] Eps • [Type] | **Genres:** [Genres]
- **The Prince's Verdict:** [Explanation of why this popular anime is an absolute must-watch in this genre]
- `[Watch on KuroAnime](#anime/{{ID}})` with exact ID from candidates.
End with a proud Vegeta signoff commanding them to watch!

VERIFIED KUROANIME CANDIDATES:
{catalog_str}
"""

        models = ["gemini-2.5-flash", "gemini-3.8-flash", "gemini-flash-latest", "gemini-2.5-pro"]
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
                res = _requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=18)
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

    # Fallback immediately to high-level local Vegeta brain when API key limit is reached or missing
    reply_text = _generate_vegeta_response(user_msg, kb)
    return {
        "ok": True,
        "reply": reply_text,
        "used_gemini": False,
    }


app.include_router(router, prefix="/api")
app.include_router(router)
