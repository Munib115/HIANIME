"""
HiAnime.at Full Scraper v2 — Sequential, SQLite DB, AniList enrichment
=======================================================================
- Single-threaded (no rate limits hit)
- Saves EVERY anime to SQLite immediately (crash-safe)
- Fetches ratings + posters from AniList (free GraphQL, no API key)
- Resumes from where it left off automatically
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests, base64, json, re, time, sqlite3, os
from bs4 import BeautifulSoup

# ─── Config ───────────────────────────────────────────────────────
BASE_URL   = "https://hianime.at"
API_BASE   = "https://hianime.at/api/theme/"
OUTPUT_DIR = r"C:\Users\Munib Raza\OneDrive\Desktop\hianime_output"
URL_LIST   = os.path.join(OUTPUT_DIR, "anime_url_list.json")
DB_PATH    = os.path.join(OUTPUT_DIR, "hianime.db")
LOG_FILE   = os.path.join(OUTPUT_DIR, "scraper_v2.log")

ANILIST_API = "https://graphql.anilist.co"

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://hianime.at/",
}

DELAY        = 1.2   # seconds between hianime requests
ANILIST_DELAY= 0.8   # seconds between AniList requests

log_fh = None

# ─── Logging ──────────────────────────────────────────────────────
def log(msg):
    line = "[" + time.strftime("%H:%M:%S") + "] " + str(msg)
    print(line, flush=True)
    if log_fh:
        log_fh.write(line + "\n")
        log_fh.flush()

session = requests.Session()
session.headers.update(HEADERS)

# ─── HTTP ─────────────────────────────────────────────────────────
def fetch(url, retries=4, timeout=30, as_json=False, delay=DELAY):
    for attempt in range(retries):
        try:
            time.sleep(delay)
            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json() if as_json else r.text
            elif r.status_code == 429:
                wait = 30 * (attempt + 1)
                log("  [429] Rate limited — sleeping " + str(wait) + "s")
                time.sleep(wait)
            elif r.status_code == 404:
                return None
            else:
                log("  [HTTP " + str(r.status_code) + "] " + url)
                time.sleep(5)
        except Exception as e:
            if attempt == retries - 1:
                log("  FETCH ERR: " + str(e))
            time.sleep(3)
    return None

# ─── AniList GraphQL ──────────────────────────────────────────────
ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    title { romaji english native }
    averageScore
    meanScore
    popularity
    coverImage { large extraLarge }
    bannerImage
    status
    episodes
    season
    seasonYear
    genres
    description(asHtml: false)
  }
}
"""

def anilist_lookup(title):
    """Query AniList for anime by title. Returns dict or None."""
    try:
        time.sleep(ANILIST_DELAY)
        r = session.post(
            ANILIST_API,
            json={"query": ANILIST_QUERY, "variables": {"search": title}},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            media = data.get("data", {}).get("Media")
            if media:
                return {
                    "anilist_id":     media.get("id"),
                    "title_romaji":   media.get("title", {}).get("romaji", ""),
                    "title_english":  media.get("title", {}).get("english", ""),
                    "title_native":   media.get("title", {}).get("native", ""),
                    "anilist_score":  media.get("averageScore"),
                    "anilist_popularity": media.get("popularity"),
                    "poster_large":   media.get("coverImage", {}).get("extraLarge") or media.get("coverImage", {}).get("large", ""),
                    "banner_image":   media.get("bannerImage", ""),
                    "anilist_status": media.get("status", ""),
                    "episodes":       media.get("episodes"),
                    "season":         media.get("season", ""),
                    "season_year":    media.get("seasonYear"),
                    "al_genres":      ", ".join(media.get("genres", [])),
                    "al_description": (media.get("description") or "")[:2000],
                }
        elif r.status_code == 429:
            log("  AniList rate limit hit — sleeping 60s")
            time.sleep(60)
    except Exception as e:
        log("  AniList ERR: " + str(e))
    return None

# ─── SQLite DB Setup ──────────────────────────────────────────────
def init_db(conn):
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS anime (
        anime_id        INTEGER PRIMARY KEY,
        hianime_url     TEXT,
        title           TEXT,
        japanese_name   TEXT,
        synonyms        TEXT,
        type            TEXT,
        status          TEXT,
        aired           TEXT,
        premiered       TEXT,
        mal_score       TEXT,
        studios         TEXT,
        producers       TEXT,
        duration        TEXT,
        genres          TEXT,
        synopsis        TEXT,
        poster_url      TEXT,
        category        TEXT,
        listing_page    INTEGER,
        episode_count   INTEGER DEFAULT 0,
        -- AniList enrichment
        anilist_id      INTEGER,
        title_romaji    TEXT,
        title_english   TEXT,
        title_native    TEXT,
        anilist_score   INTEGER,
        anilist_popularity INTEGER,
        poster_large    TEXT,
        banner_image    TEXT,
        anilist_status  TEXT,
        al_episodes     INTEGER,
        season          TEXT,
        season_year     INTEGER,
        al_genres       TEXT,
        al_description  TEXT,
        seasons_json    TEXT,
        -- meta
        scraped_at      TEXT,
        enriched_at     TEXT
    );

    CREATE TABLE IF NOT EXISTS episodes (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        anime_id        INTEGER,
        episode_id      TEXT,
        episode_number  INTEGER,
        title           TEXT,
        japanese_title  TEXT,
        watch_url       TEXT,
        FOREIGN KEY(anime_id) REFERENCES anime(anime_id)
    );

    CREATE TABLE IF NOT EXISTS streams (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        anime_id        INTEGER,
        anime_title     TEXT,
        episode_id      TEXT,
        episode_number  INTEGER,
        episode_title   TEXT,
        type            TEXT,
        server_name     TEXT,
        provider        TEXT,
        embed_url       TEXT,
        FOREIGN KEY(anime_id) REFERENCES anime(anime_id)
    );

    CREATE TABLE IF NOT EXISTS anime_seasons (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_anime_id INTEGER,
        season_name     TEXT,
        season_anime_id INTEGER,
        season_url      TEXT,
        season_poster   TEXT,
        is_current      INTEGER DEFAULT 0,
        FOREIGN KEY(parent_anime_id) REFERENCES anime(anime_id)
    );

    CREATE INDEX IF NOT EXISTS idx_episodes_anime ON episodes(anime_id);
    CREATE INDEX IF NOT EXISTS idx_streams_anime  ON streams(anime_id);
    CREATE INDEX IF NOT EXISTS idx_streams_ep     ON streams(episode_id);
    CREATE INDEX IF NOT EXISTS idx_seasons_parent ON anime_seasons(parent_anime_id);
    """)
    try:
        c.execute("ALTER TABLE anime ADD COLUMN seasons_json TEXT")
    except Exception:
        pass
    conn.commit()

def get_done_ids(conn):
    c = conn.cursor()
    c.execute("SELECT anime_id FROM anime")
    return {row[0] for row in c.fetchall()}

def save_anime(conn, meta, episodes, streams_data, al):
    c = conn.cursor()
    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    # Upsert anime row
    seasons_json = json.dumps(meta.get("seasons", []))
    c.execute("""
    INSERT OR REPLACE INTO anime (
        anime_id, hianime_url, title, japanese_name, synonyms, type, status,
        aired, premiered, mal_score, studios, producers, duration, genres,
        synopsis, poster_url, category, listing_page, episode_count,
        anilist_id, title_romaji, title_english, title_native,
        anilist_score, anilist_popularity, poster_large, banner_image,
        anilist_status, al_episodes, season, season_year, al_genres,
        al_description, seasons_json, scraped_at, enriched_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        meta.get("anime_id"),
        meta.get("detail_url",""),
        meta.get("title",""),
        meta.get("japanese_name",""),
        meta.get("synonyms",""),
        meta.get("type",""),
        meta.get("status",""),
        meta.get("aired",""),
        meta.get("premiered",""),
        meta.get("mal_score",""),
        meta.get("studios",""),
        meta.get("producers",""),
        meta.get("duration",""),
        ", ".join(meta.get("genres",[])) if isinstance(meta.get("genres"), list) else meta.get("genres",""),
        meta.get("synopsis",""),
        meta.get("poster_url",""),
        meta.get("category",""),
        meta.get("listing_page", 0),
        len(episodes),
        al.get("anilist_id")        if al else None,
        al.get("title_romaji","")   if al else "",
        al.get("title_english","")  if al else "",
        al.get("title_native","")   if al else "",
        al.get("anilist_score")     if al else None,
        al.get("anilist_popularity") if al else None,
        al.get("poster_large","")   if al else "",
        al.get("banner_image","")   if al else "",
        al.get("anilist_status","") if al else "",
        al.get("episodes")          if al else None,
        al.get("season","")         if al else "",
        al.get("season_year")       if al else None,
        al.get("al_genres","")      if al else "",
        al.get("al_description","") if al else "",
        seasons_json,
        ts,
        ts if al else None,
    ))

    # Insert episodes (skip if already there)
    for ep in episodes:
        c.execute("""
        INSERT OR IGNORE INTO episodes
            (anime_id, episode_id, episode_number, title, japanese_title, watch_url)
        VALUES (?,?,?,?,?,?)
        """, (
            meta.get("anime_id"),
            ep.get("episode_id",""),
            ep.get("episode_number", 0),
            ep.get("title",""),
            ep.get("japanese_title",""),
            ep.get("watch_url",""),
        ))

    # Insert streams
    for s in streams_data:
        c.execute("""
        INSERT OR IGNORE INTO streams
            (anime_id, anime_title, episode_id, episode_number, episode_title,
             type, server_name, provider, embed_url)
        VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            meta.get("anime_id"),
            meta.get("title",""),
            s.get("episode_id",""),
            s.get("episode_number", 0),
            s.get("episode_title",""),
            s.get("type",""),
            s.get("server_name",""),
            s.get("provider",""),
            s.get("embed_url",""),
        ))

    # Insert seasons
    for s in meta.get("seasons", []):
        c.execute("""
        INSERT OR IGNORE INTO anime_seasons
            (parent_anime_id, season_name, season_anime_id, season_url, season_poster, is_current)
        VALUES (?,?,?,?,?,?)
        """, (
            meta.get("anime_id"),
            s.get("season_name", ""),
            s.get("season_anime_id"),
            s.get("season_url", ""),
            s.get("season_poster", ""),
            s.get("is_current", 0),
        ))

    conn.commit()

# ─── HiAnime scrapers ─────────────────────────────────────────────
def extract_meta(detail_url, anime_id):
    html = fetch(detail_url)
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    meta = {"anime_id": anime_id, "detail_url": detail_url}

    for sel in ["h2.film-name", ".film-name", "h1", ".dynamic-name"]:
        el = soup.select_one(sel)
        if el:
            meta["title"] = el.get_text(strip=True)
            break

    for sel in [".film-poster img", ".anime-poster img"]:
        el = soup.select_one(sel)
        if el:
            meta["poster_url"] = el.get("data-src") or el.get("src", "")
            break

    for sel in [".film-description .text", ".film-description", ".synopsis"]:
        el = soup.select_one(sel)
        if el:
            meta["synopsis"] = el.get_text(strip=True)
            break

    prefixes = {
        "Japanese:": "japanese_name", "Synonyms:": "synonyms",
        "Aired:": "aired", "Status:": "status", "MAL Score:": "mal_score",
        "Studios:": "studios", "Duration:": "duration", "Type:": "type",
        "Producers:": "producers", "Premiered:": "premiered",
    }
    for item in soup.select(".item, .item-title, .item-list"):
        txt = item.get_text(" ", strip=True)
        for prefix, key in prefixes.items():
            if txt.startswith(prefix):
                meta.setdefault(key, txt.replace(prefix, "", 1).strip())
        if txt.startswith("Genres:"):
            genres_str = txt.replace("Genres:", "").strip()
            meta.setdefault("genres", [g.strip() for g in re.split(r",", genres_str) if g.strip()])

    # Extract all seasons and sequels/prequels
    seasons = []
    block = soup.select_one(".block_area-seasons")
    if block:
        for item in block.select(".os-item"):
            s_title = item.select_one(".title")
            s_name = s_title.get_text(strip=True) if s_title else item.get_text(strip=True)
            s_href = item.get("href", "")
            s_id_match = re.search(r"-(\d+)$", s_href)
            s_id = int(s_id_match.group(1)) if s_id_match else None

            poster_m = re.search(r"url\((.*?)\)", str(item))
            s_poster = poster_m.group(1).strip("'\"") if poster_m else ""

            seasons.append({
                "season_name": s_name,
                "season_anime_id": s_id,
                "season_url": s_href if s_href.startswith("http") else BASE_URL + s_href,
                "season_poster": s_poster,
                "is_current": 1 if "active" in item.get("class", []) else 0,
            })
    meta["seasons"] = seasons

    return meta

def get_episodes(anime_id):
    data = fetch(API_BASE + "episode/list/" + str(anime_id), as_json=True, delay=0.8)
    if not data or "html" not in data:
        return []
    soup = BeautifulSoup(data["html"], "lxml")
    eps = []
    for a in soup.select("a.ep-item"):
        href   = a.get("href","")
        ep_num = a.get("data-number","")
        ep_id  = a.get("data-id","")
        tel    = a.select_one(".ep-name")
        title  = tel.get_text(strip=True) if tel else a.get("title","")
        jname  = tel.get("data-jname","") if tel else ""
        m = re.search(r"[?&]ep=(\d+)", href)
        if m: ep_id = m.group(1)
        eps.append({
            "episode_id":     ep_id,
            "episode_number": int(ep_num) if str(ep_num).isdigit() else 0,
            "title":          title,
            "japanese_title": jname,
            "watch_url":      href if href.startswith("http") else BASE_URL + href,
        })
    return eps

def get_episode_streams(ep_id, ep_num, ep_title, anime_id, anime_title):
    data = fetch(API_BASE + "episode/servers?episodeId=" + str(ep_id), as_json=True, delay=0.8)
    if not data or not data.get("status"):
        return []
    soup = BeautifulSoup(data.get("html",""), "lxml")
    out = []
    for s in soup.select(".server-item"):
        sname = s.get("data-server-name","")
        if sname == "HD-1":
            continue
        shash = s.get("data-hash","")
        try:    url = base64.b64decode(shash).decode("utf-8")
        except: url = ""
        if not url:
            continue
        provider = re.search(r"https?://([^/]+)", url)
        prov_name = provider.group(1) if provider else ""
        if "megaplay" in prov_name:
            continue
        out.append({
            "episode_id":     ep_id,
            "episode_number": ep_num,
            "episode_title":  ep_title,
            "server_name":    sname or "ZokoAnime",
            "type":           s.get("data-type",""),
            "embed_url":      url,
            "provider":       prov_name,
        })
    return out

# ─── Main ─────────────────────────────────────────────────────────
def main():
    global log_fh
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log_fh = open(LOG_FILE, "a", encoding="utf-8")

    log("=" * 60)
    log("HiAnime Full Scraper v2 — Sequential + SQLite + AniList")
    log("Started: " + time.strftime("%Y-%m-%d %H:%M:%S"))
    log("=" * 60)

    # Load URL list
    with open(URL_LIST, "r", encoding="utf-8") as f:
        anime_list = json.load(f)
    log("Loaded " + str(len(anime_list)) + " anime URLs")

    # Init DB
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    init_db(conn)
    done_ids = get_done_ids(conn)
    log("Already in DB: " + str(len(done_ids)) + " anime")

    remaining = [a for a in anime_list if a.get("anime_id") not in done_ids]
    log("Remaining to scrape: " + str(len(remaining)))
    log("=" * 60)

    scraped = 0
    errors  = 0
    total   = len(remaining)

    for i, anime_info in enumerate(remaining, 1):
        anime_id   = anime_info.get("anime_id")
        detail_url = anime_info.get("url")
        fallback_title = anime_info.get("title", "")

        try:
            # 1. Scrape detail page
            meta = extract_meta(detail_url, anime_id)
            if not meta:
                errors += 1
                log("  [" + str(i) + "/" + str(total) + "] SKIP (no meta): " + str(detail_url))
                continue

            meta["category"]     = anime_info.get("category","")
            meta["listing_page"] = anime_info.get("listing_page", 0)
            meta.setdefault("title", fallback_title)
            title = meta.get("title", fallback_title)

            # 2. Episode list
            episodes = get_episodes(anime_id)

            # 3. Stream links for ALL episodes
            all_streams = []
            for ep in episodes:
                ep_id = ep.get("episode_id")
                if ep_id:
                    streams = get_episode_streams(
                        ep_id, ep.get("episode_number",0),
                        ep.get("title",""), anime_id, title
                    )
                    all_streams.extend(streams)

            # 4. AniList enrichment (use english title, then romaji fallback)
            al = anilist_lookup(title)
            if not al and meta.get("japanese_name"):
                al = anilist_lookup(meta["japanese_name"])

            # Use AniList poster if HiAnime poster is missing
            if al and al.get("poster_large") and not meta.get("poster_url"):
                meta["poster_url"] = al["poster_large"]

            # 5. Save to SQLite immediately
            save_anime(conn, meta, episodes, all_streams, al)

            scraped += 1
            al_score = str(al.get("anilist_score","?")) + "/100" if al else "N/A"
            stream_n = len(all_streams)
            ep_n     = len(episodes)
            log("[" + str(i) + "/" + str(total) + "] " + title[:50] + " | " +
                str(ep_n) + " eps | " + str(stream_n) + " streams | AniList: " + al_score)

            # Print running totals every 50
            if scraped % 50 == 0:
                c = conn.cursor()
                c.execute("SELECT COUNT(*) FROM streams")
                total_streams = c.fetchone()[0]
                c.execute("SELECT COUNT(*) FROM episodes")
                total_eps = c.fetchone()[0]
                log("=" * 50)
                log("TOTALS — Anime: " + str(scraped) + " | Episodes: " + str(total_eps) + " | Streams: " + str(total_streams))
                log("=" * 50)

        except Exception as e:
            errors += 1
            log("  ERROR " + str(anime_id) + ": " + str(e))

    # Final stats
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM anime")   ; n_anime = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM episodes") ; n_eps   = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM streams") ; n_st    = c.fetchone()[0]

    log("=" * 60)
    log("COMPLETE!")
    log("  Anime in DB:    " + str(n_anime))
    log("  Episodes in DB: " + str(n_eps))
    log("  Streams in DB:  " + str(n_st))
    log("  Errors:         " + str(errors))
    log("  DB file:        " + DB_PATH)
    log("=" * 60)

    conn.close()
    if log_fh:
        log_fh.close()

if __name__ == "__main__":
    main()

