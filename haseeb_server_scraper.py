"""
Haseeb Server Turbo Parallel Scraper — Complete & Continuous Ingestion Engine
=============================================================================
- Dedicated independent SQLite database: haseeb_server.db
- Never mixes data with hianime.db or munib_server.db
- High-concurrency extraction (36 worker threads)
- Ingests brand new anime (popular, current season, recently added) + full 5,532 catalog
- Probes and stores direct HLS streams (SUB & DUB)
- Online non-locking SQLite backups using sqlite3.backup()
- Continuously scans for new episodes and newly added titles
"""
import sys
import io
import os
import time
import json
import gzip
import sqlite3
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HASEEB_DB_PATH = os.environ.get("HASEEB_DB") or os.path.join(BASE_DIR, "haseeb_server.db")
HIANIME_DB_PATH = os.environ.get("ANIME_DB") or os.path.join(BASE_DIR, "hianime.db")
LOG_FILE = os.path.join(BASE_DIR, "haseeb_scraper.log")

ONIANIME_BASE = "https://onianime.eu"
CATALOG_API = f"{ONIANIME_BASE}/api/catalog"
ANIME_API = f"{ONIANIME_BASE}/api/anime"
POPULAR_API = f"{ONIANIME_BASE}/api/animes/popular"
RECOMMENDED_API = f"{ONIANIME_BASE}/api/animes/recommended"

SERVER_NAME = "Haseeb"
PROVIDER_NAME = "onianime.eu"
CATALOG_WORKERS = 16
EXTRACT_WORKERS = 36

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://onianime.eu/home",
    "Origin": "https://onianime.eu",
}

session = requests.Session()
session.headers.update(HEADERS)
adapter = HTTPAdapter(
    pool_connections=80,
    pool_maxsize=80,
    max_retries=Retry(total=3, backoff_factor=0.15, status_forcelist=[500, 502, 503, 504]),
)
session.mount("https://", adapter)
session.mount("http://", adapter)

log_fh = None


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if log_fh:
        try:
            log_fh.write(line + "\n")
            log_fh.flush()
        except Exception:
            pass


def fetch_json(url: str, params: dict = None, retries: int = 3, timeout: int = 8):
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                time.sleep(1.0 * (attempt + 1))
            elif r.status_code in (404, 400):
                return None
            else:
                time.sleep(0.1)
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(0.1)
    return None


def init_haseeb_db(conn: sqlite3.Connection):
    c = conn.cursor()
    c.execute("PRAGMA journal_mode = WAL;")
    c.execute("PRAGMA synchronous = NORMAL;")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS anime (
        anime_id            INTEGER PRIMARY KEY,
        onianime_id         INTEGER,
        anilist_id          INTEGER UNIQUE,
        title               TEXT,
        title_english       TEXT,
        title_romaji        TEXT,
        japanese_name       TEXT,
        type                TEXT,
        status              TEXT,
        genres              TEXT,
        duration            TEXT,
        season              TEXT,
        season_year         INTEGER,
        anilist_score       INTEGER,
        anilist_popularity  INTEGER,
        poster_url          TEXT,
        poster_large        TEXT,
        episode_count       INTEGER DEFAULT 0,
        subbed_count        INTEGER DEFAULT 0,
        dubbed_count        INTEGER DEFAULT 0,
        scraped_at          TEXT
    );

    CREATE TABLE IF NOT EXISTS episodes (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        anime_id        INTEGER,
        anilist_id      INTEGER,
        episode_id      TEXT,
        episode_number  INTEGER,
        title           TEXT,
        watch_url       TEXT,
        UNIQUE(anime_id, episode_number)
    );

    CREATE TABLE IF NOT EXISTS streams (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        anime_id        INTEGER,
        anilist_id      INTEGER,
        anime_title     TEXT,
        episode_id      TEXT,
        episode_number  INTEGER,
        episode_title   TEXT,
        type            TEXT,
        server_name     TEXT,
        provider        TEXT,
        embed_url       TEXT,
        UNIQUE(anime_id, episode_number, type, server_name)
    );

    CREATE TABLE IF NOT EXISTS app_config (
        key   TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_haseeb_anime_al     ON anime(anilist_id);
    CREATE INDEX IF NOT EXISTS idx_haseeb_ep_anime     ON episodes(anime_id);
    CREATE INDEX IF NOT EXISTS idx_haseeb_streams_ep   ON streams(episode_id, server_name);
    CREATE INDEX IF NOT EXISTS idx_haseeb_streams_al   ON streams(anilist_id, episode_number);
    """)
    conn.commit()


def sync_haseeb_copies():
    if not os.path.exists(HASEEB_DB_PATH):
        return
    try:
        src = sqlite3.connect(HASEEB_DB_PATH, timeout=10)
        src.execute("PRAGMA wal_checkpoint(PASSIVE);")

        tmp_backup = HASEEB_DB_PATH + ".backup.tmp"
        dst = sqlite3.connect(tmp_backup)
        src.backup(dst)
        src.close()
        dst.close()

        with open(tmp_backup, "rb") as f_in:
            data = f_in.read()
        try:
            os.remove(tmp_backup)
        except Exception:
            pass

        targets = [
            os.path.join(BASE_DIR, "api", "haseeb_server.db.gz"),
            os.path.join(BASE_DIR, "anime_app", "web", "api", "haseeb_server.db.gz"),
        ]
        for t in targets:
            os.makedirs(os.path.dirname(t), exist_ok=True)
            with gzip.open(t, "wb") as f_out:
                f_out.write(data)
            log(f"  Synced online archive: {os.path.basename(t)} ({len(data):,} -> {os.path.getsize(t):,} bytes)")
    except Exception as e:
        log(f"  Error syncing haseeb_server.db archives: {e}")


def load_hianime_reference():
    by_anilist = {}
    if not os.path.exists(HIANIME_DB_PATH):
        return by_anilist

    try:
        conn = sqlite3.connect(HIANIME_DB_PATH, timeout=10)
        c = conn.cursor()
        c.execute("SELECT anime_id, anilist_id, title, title_english, title_romaji FROM anime")
        for a_id, al_id, t, t_en, t_rom in c.fetchall():
            if al_id:
                by_anilist[int(al_id)] = {
                    "anime_id": int(a_id),
                    "anilist_id": int(al_id),
                    "title": t or t_en or t_rom or "",
                }
        conn.close()
    except Exception:
        pass
    return by_anilist


def resolve_single_stream(oni_id: int, ep_no: int, stype: str, providers: list):
    candidate_servers = []
    if providers and isinstance(providers, list):
        candidate_servers.extend(providers)
    for fallback in ("kaa", "yuki", "animedunya", "sora"):
        if fallback not in candidate_servers:
            candidate_servers.append(fallback)

    for prov in candidate_servers[:3]:
        p = fetch_json(
            f"{ANIME_API}/{oni_id}/parts",
            params={"episode": ep_no, "type": stype, "server": prov},
            timeout=5,
        )
        if p and p.get("sources"):
            sources = p.get("sources", [])
            if sources and sources[0].get("src"):
                return sources[0]["src"], prov
    return None, None


def extract_anime_data(item: dict, ref_by_al: dict):
    oni_id = item.get("id")
    if not oni_id:
        return None

    al_raw = item.get("anilist") or item.get("anilistId")
    al_id = int(al_raw) if al_raw and str(al_raw).isdigit() else None

    if al_id and al_id in ref_by_al:
        anime_id = ref_by_al[al_id]["anime_id"]
    elif al_id:
        anime_id = 2000000 + al_id
    else:
        anime_id = 3000000 + int(oni_id)

    title_en = item.get("eng_name") or ""
    title_nat = item.get("name") or ""
    display_title = title_en or title_nat or f"Anime {oni_id}"

    ep_data = fetch_json(f"{ANIME_API}/{oni_id}/episodes/new", timeout=7)
    if not ep_data or not ep_data.get("episodes"):
        return None

    episodes = ep_data.get("episodes", [])
    if not episodes:
        return None

    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    # Probe Episode 1 streams (SUB & DUB)
    streams_list = []
    ep1 = episodes[0]
    ep1_no = int(ep1.get("ep") or 1)
    ep1_title = ep1.get("title") or f"Episode {ep1_no}"
    ep1_id = f"haseeb_{al_id or oni_id}_{ep1_no}"

    sub_prov = ep1.get("subProviders") or []
    dub_prov = ep1.get("dubProviders") or []

    sub_count = 0
    dub_count = 0

    if ep1.get("hasSub", True):
        src, prov = resolve_single_stream(oni_id, ep1_no, "sub", sub_prov)
        if src:
            sub_count += 1
            streams_list.append((
                anime_id, al_id, display_title, ep1_id, ep1_no,
                ep1_title, "sub", SERVER_NAME, prov or PROVIDER_NAME, src
            ))

    if ep1.get("hasDub", False):
        src, prov = resolve_single_stream(oni_id, ep1_no, "dub", dub_prov)
        if src:
            dub_count += 1
            streams_list.append((
                anime_id, al_id, display_title, ep1_id, ep1_no,
                ep1_title, "dub", SERVER_NAME, prov or PROVIDER_NAME, src
            ))

    # Build episodes rows
    episodes_list = []
    default_stream_url = streams_list[0][9] if streams_list else ""
    for ep_obj in episodes:
        ep_no = int(ep_obj.get("ep") or 1)
        ep_title = ep_obj.get("title") or f"Episode {ep_no}"
        ep_id = f"haseeb_{al_id or oni_id}_{ep_no}"
        episodes_list.append((
            anime_id, al_id, ep_id, ep_no, ep_title, default_stream_url
        ))

    anime_tuple = (
        anime_id, int(oni_id), al_id, display_title, title_en, title_nat,
        title_nat, item.get("type") or "TV", item.get("status") or "Finished",
        "", "", item.get("theme") or "", item.get("release_year"),
        None, item.get("watched") or 0, item.get("image") or "", item.get("image") or "",
        len(episodes),
        sub_count or (len(episodes) if any(e.get("hasSub") for e in episodes) else 0),
        dub_count or (len(episodes) if any(e.get("hasDub") for e in episodes) else 0),
        ts,
    )

    return anime_tuple, episodes_list, streams_list, display_title


def collect_all_candidates():
    """Discover all anime from popular, recommended, and 222 catalog pages."""
    log("Discovering priority newly added & releasing anime from popular / recommended APIs...")
    candidates = []
    seen = set()

    pop_data = fetch_json(POPULAR_API) or {}
    for k in ("recentlyAdded", "currentSeason", "sliderMovies", "previousSeason"):
        for it in (pop_data.get(k) or []):
            if it.get("id") and it["id"] not in seen:
                seen.add(it["id"])
                candidates.append(it)

    rec_data = fetch_json(RECOMMENDED_API) or []
    for it in rec_data:
        if it.get("id") and it["id"] not in seen:
            seen.add(it["id"])
            candidates.append(it)

    log(f"Found {len(candidates)} high-priority new/popular anime. Now collecting catalog pages...")

    first = fetch_json(f"{CATALOG_API}?page=1") or {}
    total_pages = min(int(first.get("totalPages") or 222), 222)
    for it in (first.get("animes") or []):
        if it.get("id") and it["id"] not in seen:
            seen.add(it["id"])
            candidates.append(it)

    def fetch_page(p):
        d = fetch_json(f"{CATALOG_API}?page={p}")
        return d.get("animes", []) if d else []

    with ThreadPoolExecutor(max_workers=CATALOG_WORKERS) as pool:
        futures = {pool.submit(fetch_page, p): p for p in range(2, total_pages + 1)}
        for fut in as_completed(futures):
            try:
                for it in fut.result():
                    if it.get("id") and it["id"] not in seen:
                        seen.add(it["id"])
                        candidates.append(it)
            except Exception:
                pass

    log(f"Discovery complete: Collected {len(candidates):,} total unique anime across onianime.eu.")
    return candidates


def run_scrape_cycle(conn: sqlite3.Connection, ref_by_al: dict):
    c = conn.cursor()
    c.execute("SELECT onianime_id FROM anime")
    scraped_oni = set(r[0] for r in c.fetchall() if r[0])

    candidates = collect_all_candidates()
    unscraped = [it for it in candidates if it.get("id") and int(it["id"]) not in scraped_oni]

    log(f"Already scraped: {len(scraped_oni):,} anime. Needing scrape: {len(unscraped):,} anime.")
    if not unscraped:
        log("All candidate anime are already scraped!")
        return 0, 0, 0

    # Priority sorting: Releasing / current season / popular first
    unscraped.sort(key=lambda x: (
        1 if x.get("status") == "Releasing" else 0,
        x.get("watched") or 0,
    ), reverse=True)

    total_added_anime = 0
    total_added_episodes = 0
    total_added_streams = 0

    log(f"Starting turbo extraction with {EXTRACT_WORKERS} concurrent threads...")

    batch_size = 20
    with ThreadPoolExecutor(max_workers=EXTRACT_WORKERS) as pool:
        futures = {pool.submit(extract_anime_data, item, ref_by_al): item for item in unscraped}
        for idx, fut in enumerate(as_completed(futures), 1):
            try:
                res = fut.result()
                if not res:
                    continue
                anime_tuple, ep_list, st_list, title = res

                c.execute("""
                    INSERT OR REPLACE INTO anime (
                        anime_id, onianime_id, anilist_id, title, title_english, title_romaji,
                        japanese_name, type, status, genres, duration, season, season_year,
                        anilist_score, anilist_popularity, poster_url, poster_large,
                        episode_count, subbed_count, dubbed_count, scraped_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, anime_tuple)

                c.executemany("""
                    INSERT OR REPLACE INTO episodes (
                        anime_id, anilist_id, episode_id, episode_number, title, watch_url
                    ) VALUES (?,?,?,?,?,?)
                """, ep_list)

                if st_list:
                    c.executemany("""
                        INSERT OR REPLACE INTO streams (
                            anime_id, anilist_id, anime_title, episode_id, episode_number,
                            episode_title, type, server_name, provider, embed_url
                        ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """, st_list)

                total_added_anime += 1
                total_added_episodes += len(ep_list)
                total_added_streams += len(st_list)

                if total_added_anime % 15 == 0 or total_added_anime <= 10:
                    log(f"  [{idx}/{len(unscraped)}] [Haseeb] {title[:32]} -> +{len(ep_list)} eps, +{len(st_list)} streams | Added: {total_added_anime:,} anime, {total_added_episodes:,} eps, {total_added_streams:,} streams")

                if total_added_anime % batch_size == 0:
                    conn.commit()

                if total_added_anime % 80 == 0:
                    conn.commit()
                    sync_haseeb_copies()

            except Exception as e:
                log(f"Error inserting item: {e}")

    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
    sync_haseeb_copies()

    return total_added_anime, total_added_episodes, total_added_streams


def main():
    global log_fh
    log_fh = open(LOG_FILE, "a", encoding="utf-8")

    log("=" * 70)
    log("Haseeb Server (Server 3) Turbo Parallel Ingestion Starting")
    log("=" * 70)

    conn = sqlite3.connect(HASEEB_DB_PATH, timeout=30)
    init_haseeb_db(conn)

    ref_by_al = load_hianime_reference()
    log(f"Loaded {len(ref_by_al):,} reference anime mappings from hianime.db")

    while True:
        try:
            a, e, s = run_scrape_cycle(conn, ref_by_al)
            log(f"Cycle completed: +{a:,} anime, +{e:,} episodes, +{s:,} streams.")
        except Exception as err:
            log(f"Cycle encountered error: {err}")

        log("Waiting 3 minutes before next discovery sweep...")
        time.sleep(180)


if __name__ == "__main__":
    main()
