"""
KuroAnime API — Vercel Serverless & Standalone FastAPI Backend
Provides JSON API over SQLite hianime.db + Direct Ad-Free HLS Stream Resolver & Proxy
"""
import os
import sqlite3
import re
import base64
import json
import urllib.parse
from typing import Optional

import requests as _requests
from requests.adapters import HTTPAdapter
from fastapi import FastAPI, HTTPException, Query, APIRouter
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
                # Rewrite URI="..." inside #EXT-X-KEY or #EXT-X-MAP if present
                if 'URI="' in line:
                    def _repl_uri(match):
                        u = urllib.parse.urljoin(url, match.group(1))
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


# Mount both /api and root routes so that rewrites work seamlessly
app.include_router(router, prefix="/api")
app.include_router(router)
