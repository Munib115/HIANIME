"""
HiAnime Turbo Scraper v3 — Multi-Worker Parallel Engine (~2 Hours)
===================================================================
- Concurrent episode server fetching (10 parallel workers per anime)
- SQLite with WAL mode & NORMAL sync (high-speed transactional writes)
- Full Season & Sequel relations extraction
- Sub & Dub stream link extraction decoded from base64 data-hash
- AniList rating & poster enrichment with rate-limit protection
- Auto-resumes from last saved anime
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests, base64, json, re, time, sqlite3, os, shutil
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─── Configuration ────────────────────────────────────────────────
BASE_URL      = "https://hianime.at"
API_BASE      = "https://hianime.at/api/theme/"
OUTPUT_DIR    = r"C:\Users\Munib Raza\OneDrive\Desktop\hianime_output"
URL_LIST      = os.path.join(OUTPUT_DIR, "anime_url_list.json")

def sync_db_copies():
    for dest in [
        os.path.join(OUTPUT_DIR, "api", "hianime.db"),
        os.path.join(OUTPUT_DIR, "anime_app", "web", "api", "hianime.db"),
        os.path.join(OUTPUT_DIR, "anime_app", "web", "hianime.db"),
    ]:
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(DB_PATH, dest)
        except Exception:
            pass
DB_PATH       = os.path.join(OUTPUT_DIR, "hianime.db")
LOG_FILE      = os.path.join(OUTPUT_DIR, "turbo_scrape.log")

EPISODE_WORKERS = 10     # parallel workers for episode server calls
ANILIST_API     = "https://graphql.anilist.co"

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://hianime.at/",
}

# ─── High-Throughput HTTP Session ──────────────────────────────────
session = requests.Session()
session.headers.update(HEADERS)
adapter = HTTPAdapter(
    pool_connections=30,
    pool_maxsize=30,
    max_retries=Retry(total=3, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
)
session.mount("https://", adapter)
session.mount("http://", adapter)

log_fh = None
rate_lock = Lock()
last_request_time = 0.0

def log(msg):
    line = "[" + time.strftime("%H:%M:%S") + "] " + str(msg)
    print(line, flush=True)
    if log_fh:
        log_fh.write(line + "\n")
        log_fh.flush()

def fetch(url, retries=4, timeout=12, as_json=False):
    global last_request_time
    for attempt in range(retries):
        try:
            with rate_lock:
                now = time.time()
                elapsed = now - last_request_time
                if elapsed < 0.18:  # 0.18s = ~5.5 requests/sec safe limit
                    time.sleep(0.18 - elapsed)
                last_request_time = time.time()

            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json() if as_json else r.text
            elif r.status_code == 429:
                wait = 6 * (attempt + 1)
                log(f"  [429] Rate limited on {url[:60]} — sleeping {wait}s")
                time.sleep(wait)
            elif r.status_code == 404:
                return None
            else:
                time.sleep(1)
        except Exception as e:
            if attempt == retries - 1:
                log(f"  FETCH ERR {url[:60]}: {e}")
            time.sleep(1)
    return None

# ─── AniList Enrichment ───────────────────────────────────────────
ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    title { romaji english native }
    averageScore
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

last_anilist_time = 0

def anilist_lookup(title):
    global last_anilist_time
    try:
        now = time.time()
        elapsed = now - last_anilist_time
        if elapsed < 0.7:
            time.sleep(0.7 - elapsed)
        last_anilist_time = time.time()

        r = session.post(
            ANILIST_API,
            json={"query": ANILIST_QUERY, "variables": {"search": title}},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            media = data.get("data", {}).get("Media")
            if media:
                return {
                    "anilist_id":         media.get("id"),
                    "title_romaji":       media.get("title", {}).get("romaji", ""),
                    "title_english":      media.get("title", {}).get("english", ""),
                    "title_native":       media.get("title", {}).get("native", ""),
                    "anilist_score":      media.get("averageScore"),
                    "anilist_popularity": media.get("popularity"),
                    "poster_large":       media.get("coverImage", {}).get("extraLarge") or media.get("coverImage", {}).get("large", ""),
                    "banner_image":       media.get("bannerImage", ""),
                    "anilist_status":     media.get("status", ""),
                    "episodes":           media.get("episodes"),
                    "season":             media.get("season", ""),
                    "season_year":        media.get("seasonYear"),
                    "al_genres":          ", ".join(media.get("genres", [])),
                    "al_description":     (media.get("description") or "")[:2000],
                }
    except Exception:
        pass
    return None

# ─── SQLite DB Setup ──────────────────────────────────────────────
def init_db(conn):
    c = conn.cursor()
    c.execute("PRAGMA journal_mode = WAL;")
    c.execute("PRAGMA synchronous = NORMAL;")
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
    conn.commit()

def get_done_ids(conn):
    c = conn.cursor()
    c.execute("SELECT anime_id FROM anime")
    return {row[0] for row in c.fetchall()}

def save_anime(conn, meta, episodes, streams_data, al):
    c = conn.cursor()
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
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

    # Episodes
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

    # Streams
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

    # Seasons
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

# ─── HiAnime Extraction ───────────────────────────────────────────
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
    data = fetch(API_BASE + "episode/list/" + str(anime_id), as_json=True)
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

def fetch_single_episode_servers(ep):
    ep_id    = ep.get("episode_id")
    ep_num   = ep.get("episode_number", 0)
    ep_title = ep.get("title", "")
    if not ep_id:
        return []

    data = fetch(API_BASE + "episode/servers?episodeId=" + str(ep_id), as_json=True)
    if not data or not data.get("status"):
        return []

    soup = BeautifulSoup(data.get("html",""), "lxml")
    out = []
    for s in soup.select(".server-item"):
        sname = (s.get("data-server-name","") or "ZokoAnime").strip()
        if "HD-1" in sname or "HD 1" in sname:
            continue
        shash = s.get("data-hash","")
        try:
            url = base64.b64decode(shash).decode("utf-8", errors="ignore").strip()
        except Exception:
            url = ""
        if not url:
            continue
        provider = re.search(r"https?://([^/]+)", url)
        prov_name = provider.group(1) if provider else ""
        if "megaplay" in prov_name.lower():
            continue
        out.append({
            "episode_id":     ep_id,
            "episode_number": ep_num,
            "episode_title":  ep_title,
            "server_name":    sname or "ZokoAnime",
            "type":           (s.get("data-type","") or "sub").strip().lower(),
            "embed_url":      url,
            "provider":       prov_name,
        })
    return out



def get_all_episode_streams_concurrent(episodes, title=""):
    if not episodes:
        return []
    all_streams = []
    workers = min(4, len(episodes))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_single_episode_servers, ep): ep for ep in episodes}
        done_cnt = 0
        for f in as_completed(futures):
            done_cnt += 1
            if len(episodes) > 50 and done_cnt % 50 == 0:
                log(f"    ... {title[:30]}: {done_cnt}/{len(episodes)} episodes fetched")
            try:
                res = f.result()
                if res:
                    all_streams.extend(res)
            except Exception:
                pass
    return all_streams

# ─── Main Turbo Runner ────────────────────────────────────────────
def main():
    global log_fh
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log_fh = open(LOG_FILE, "a", encoding="utf-8")

    log("=" * 65)
    log("HiAnime TURBO Scraper v3 — 10 Parallel Workers Engine (~2hr pace)")
    log("Started: " + time.strftime("%Y-%m-%d %H:%M:%S"))
    log("=" * 65)

    with open(URL_LIST, "r", encoding="utf-8") as f:
        anime_list = json.load(f)
    log(f"Loaded {len(anime_list):,} total anime target URLs")

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    init_db(conn)
    done_ids = get_done_ids(conn)
    log(f"Already in DB: {len(done_ids):,} anime (skipping)")

    remaining = [a for a in anime_list if a.get("anime_id") not in done_ids]
    log(f"Remaining to scrape: {len(remaining):,}")
    log("=" * 65)

    scraped_anime = 0
    scraped_eps   = 0
    scraped_st    = 0
    errors        = 0
    total         = len(remaining)
    start_time    = time.time()

    for i, anime_info in enumerate(remaining, 1):
        anime_id       = anime_info.get("anime_id")
        detail_url     = anime_info.get("url")
        fallback_title = anime_info.get("title", "")
        anime_start    = time.time()

        try:
            # 1. Detail page
            meta = extract_meta(detail_url, anime_id)
            if not meta:
                errors += 1
                log(f"  [{i}/{total}] SKIP: {detail_url}")
                continue

            meta["category"]     = anime_info.get("category","")
            meta["listing_page"] = anime_info.get("listing_page", 0)
            meta.setdefault("title", fallback_title)
            title = meta.get("title", fallback_title)

            # 2. Episode list
            episodes = get_episodes(anime_id)

            # 3. Parallel episode streams
            streams = get_all_episode_streams_concurrent(episodes, title)

            # 4. AniList enrichment
            al = anilist_lookup(title)
            if not al and meta.get("japanese_name"):
                al = anilist_lookup(meta["japanese_name"])

            if al and al.get("poster_large") and not meta.get("poster_url"):
                meta["poster_url"] = al["poster_large"]

            # 5. Immediate SQLite write
            save_anime(conn, meta, episodes, streams, al)

            scraped_anime += 1
            scraped_eps   += len(episodes)
            scraped_st    += len(streams)
            elapsed       = time.time() - start_time
            anime_sec     = time.time() - anime_start

            if scraped_anime % 20 == 0:
                try:
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
                    sync_db_copies()
                except Exception:
                    pass

            # Calculate live speed & ETA
            eps_per_sec   = scraped_eps / max(elapsed, 1)
            rem_anime     = total - i
            est_sec_left  = (elapsed / scraped_anime) * rem_anime if scraped_anime else 0
            eta_hours     = est_sec_left / 3600

            al_str = f"AniList: {al.get('anilist_score','?')}/100" if al else "AniList: N/A"
            log(f"[{i}/{total}] {title[:35]} | {len(episodes)} eps | {len(streams)} streams | in {anime_sec:.1f}s | {eps_per_sec:.1f} eps/s | ETA: {eta_hours:.1f}h")

        except Exception as e:
            errors += 1
            log(f"  ERROR {anime_id}: {e}")

    log("=" * 65)
    log("TURBO SCRAPE FINISHED!")
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except Exception:
        pass
    conn.close()
    sync_db_copies()
    if log_fh:
        log_fh.close()

if __name__ == "__main__":
    main()
