"""
Haseeb Server (Server 3) Direct ID Probing & Continuous Ingestion Engine
========================================================================
- Dedicated independent SQLite database: haseeb_server.db
- Probes all onianime.eu anime IDs directly via /api/anime/{id}/episodes/new
- Bypasses any catalog/search downtime on upstream
- Cross-references AniList IDs against hianime.db for canonical titles & metadata
- High-throughput parallel ingestion (36 workers)
- Probes and stores direct HLS streams (SUB & DUB)
- Online non-locking SQLite backups using sqlite3.backup()
- Continuously runs in the background
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
ANIME_API = f"{ONIANIME_BASE}/api/anime"
POPULAR_API = f"{ONIANIME_BASE}/api/animes/popular"
RECOMMENDED_API = f"{ONIANIME_BASE}/api/animes/recommended"

SERVER_NAME = "Haseeb"
PROVIDER_NAME = "onianime.eu"
EXTRACT_WORKERS = 16
MAX_ONI_ID = 6500

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
    max_retries=Retry(total=2, backoff_factor=0.1, status_forcelist=[502, 503, 504]),
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


def fetch_json_status(url: str, params: dict = None, retries: int = 2, timeout: int = 8):
    """Fetch JSON and return (payload, reason) where reason is 'ok', 'empty', or 'timeout'."""
    last_reason = "timeout"
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json(), "ok"
            elif r.status_code in (404, 400, 500):
                return None, "empty"
            elif r.status_code == 429:
                last_reason = "timeout"
                time.sleep(1.2 * (attempt + 1))
        except Exception:
            last_reason = "timeout"
            time.sleep(0.15)
    return None, last_reason


def fetch_json(url: str, params: dict = None, retries: int = 2, timeout: int = 8):
    data, _ = fetch_json_status(url, params=params, retries=retries, timeout=timeout)
    return data


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

    CREATE TABLE IF NOT EXISTS probed_oni_ids (
        onianime_id INTEGER PRIMARY KEY,
        status      TEXT,
        anilist_id  INTEGER,
        checked_at  TEXT
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
        comp_data = gzip.compress(data, compresslevel=6, mtime=0)
        for t in targets:
            os.makedirs(os.path.dirname(t), exist_ok=True)
            with open(t, "wb") as f_out:
                f_out.write(comp_data)
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
        c.execute("""
            SELECT anime_id, anilist_id, title, title_english, title_romaji, japanese_name,
                   type, status, genres, season_year, poster_url, anilist_score, anilist_popularity
            FROM anime
        """)
        for row in c.fetchall():
            a_id, al_id, t, t_en, t_rom, t_jp, typ, stat, gen, yr, post, sc, pop = row
            if al_id:
                by_anilist[int(al_id)] = {
                    "anime_id": int(a_id),
                    "anilist_id": int(al_id),
                    "title": t or t_en or t_rom or "",
                    "title_english": t_en or t or "",
                    "title_romaji": t_rom or "",
                    "japanese_name": t_jp or "",
                    "type": typ or "TV",
                    "status": stat or "Finished",
                    "genres": gen or "",
                    "season_year": yr,
                    "poster_url": post or "",
                    "anilist_score": sc,
                    "anilist_popularity": pop,
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
            timeout=4,
        )
        if p and p.get("sources"):
            sources = p.get("sources", [])
            if sources and sources[0].get("src"):
                return sources[0]["src"], prov
    return None, None


def probe_and_extract_by_id(oni_id: int, ref_by_al: dict):
    ep_data, reason = fetch_json_status(f"{ANIME_API}/{oni_id}/episodes/new", retries=1, timeout=6)
    if not ep_data:
        return reason, None

    episodes = ep_data.get("episodes", [])
    if not episodes:
        return "empty", None

    meta = ep_data.get("meta") or {}
    al_raw = meta.get("anilist") or meta.get("anilistId")
    al_id = int(al_raw) if al_raw and str(al_raw).isdigit() else None

    # Check canonical anime reference
    ref = ref_by_al.get(al_id) if al_id else None
    if ref:
        anime_id = ref["anime_id"]
        title = ref["title"]
        title_en = ref["title_english"]
        title_rom = ref["title_romaji"]
        title_jp = ref["japanese_name"]
        atype = ref["type"]
        astatus = ref["status"]
        genres = ref["genres"]
        year = ref["season_year"]
        poster = ref["poster_url"]
        score = ref["anilist_score"]
        pop = ref["anilist_popularity"]
    else:
        anime_id = (2000000 + al_id) if al_id else (3000000 + int(oni_id))
        raw_name = meta.get("name") or meta.get("eng_name") or f"Anime {oni_id}"
        title = raw_name
        title_en = meta.get("eng_name") or raw_name
        title_rom = raw_name
        title_jp = ""
        atype = "TV"
        astatus = "Finished"
        genres = ""
        year = None
        poster = meta.get("image") or ""
        score = None
        pop = 0

    display_title = title_en or title or f"Anime {oni_id}"
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
        anime_id, int(oni_id), al_id, display_title, title_en, title_rom,
        title_jp, atype, astatus, genres, "", "", year,
        score, pop, poster, poster,
        len(episodes),
        sub_count or (len(episodes) if any(e.get("hasSub") for e in episodes) else 0),
        dub_count or (len(episodes) if any(e.get("hasDub") for e in episodes) else 0),
        ts,
    )

    return "ok", (int(oni_id), anime_id, al_id, anime_tuple, episodes_list, streams_list, display_title)


def run_full_probe_cycle(conn: sqlite3.Connection, ref_by_al: dict):
    c = conn.cursor()
    c.execute("SELECT anime_id, onianime_id, anilist_id FROM anime")
    existing_anime_ids = set()
    existing_oni = set()
    existing_al = set()
    for aid, oid, alid in c.fetchall():
        if aid is not None:
            existing_anime_ids.add(int(aid))
        if oid is not None:
            existing_oni.add(int(oid))
        if alid is not None:
            existing_al.add(int(alid))

    c.execute("SELECT onianime_id, status FROM probed_oni_ids WHERE status = 'duplicate' OR (status = 'empty' AND checked_at >= datetime('now', 'localtime', '-12 hours') AND onianime_id < 6050)")
    skip_oni = set(int(r[0]) for r in c.fetchall())

    all_target_ids = set(range(1, MAX_ONI_ID + 1))
    missing_ids = sorted(list(all_target_ids - existing_oni - skip_oni))

    log(f"Current DB: {len(existing_anime_ids):,} anime ({len(skip_oni):,} known duplicates/recent-empty skipped). Probing {len(missing_ids):,} candidate onianime IDs...")
    if not missing_ids:
        log("All candidate IDs in range are already accounted for.")
        return 0, 0, 0

    total_added_anime = 0
    total_added_episodes = 0
    total_added_streams = 0

    batch_size = 20
    probed_count = 0
    with ThreadPoolExecutor(max_workers=EXTRACT_WORKERS) as pool:
        futures = {pool.submit(probe_and_extract_by_id, oid, ref_by_al): oid for oid in missing_ids}
        for idx, fut in enumerate(as_completed(futures), 1):
            oid = futures[fut]
            ts_now = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                status, payload = fut.result()
                probed_count += 1
                if status != "ok" or not payload:
                    if status == "empty":
                        c.execute("INSERT OR REPLACE INTO probed_oni_ids (onianime_id, status, anilist_id, checked_at) VALUES (?,?,?,?)", (oid, "empty", None, ts_now))
                        if probed_count % 200 == 0:
                            conn.commit()
                    continue

                p_oid, anime_id, al_id, anime_tuple, ep_list, st_list, title = payload

                # Prevent overwriting existing canonical anime entry when onianime.eu has duplicate IDs for same AniList ID
                is_duplicate = (anime_id in existing_anime_ids) or (al_id is not None and al_id in existing_al)
                if is_duplicate:
                    c.executemany("""
                        INSERT OR IGNORE INTO episodes (
                            anime_id, anilist_id, episode_id, episode_number, title, watch_url
                        ) VALUES (?,?,?,?,?,?)
                    """, ep_list)
                    ep_added = max(0, c.rowcount) if c.rowcount and c.rowcount > 0 else 0
                    st_added = 0
                    if st_list:
                        c.executemany("""
                            INSERT OR IGNORE INTO streams (
                                anime_id, anilist_id, anime_title, episode_id, episode_number,
                                episode_title, type, server_name, provider, embed_url
                            ) VALUES (?,?,?,?,?,?,?,?,?,?)
                        """, st_list)
                        st_added = max(0, c.rowcount) if c.rowcount and c.rowcount > 0 else 0
                    total_added_episodes += ep_added
                    total_added_streams += st_added
                    c.execute("INSERT OR REPLACE INTO probed_oni_ids (onianime_id, status, anilist_id, checked_at) VALUES (?,?,?,?)", (p_oid, "duplicate", al_id, ts_now))
                    if probed_count % 50 == 0:
                        conn.commit()
                    continue

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

                c.execute("INSERT OR REPLACE INTO probed_oni_ids (onianime_id, status, anilist_id, checked_at) VALUES (?,?,?,?)", (p_oid, "ok", al_id, ts_now))

                existing_anime_ids.add(anime_id)
                existing_oni.add(p_oid)
                if al_id is not None:
                    existing_al.add(al_id)

                total_added_anime += 1
                total_added_episodes += len(ep_list)
                total_added_streams += len(st_list)

                if total_added_anime % 15 == 0 or total_added_anime <= 10:
                    log(f"  [Found #{total_added_anime}] [Haseeb] {title[:32]} -> +{len(ep_list)} eps, +{len(st_list)} streams (Total DB: {len(existing_anime_ids):,} anime)")

                if total_added_anime % batch_size == 0:
                    conn.commit()

                if total_added_anime % 80 == 0:
                    conn.commit()
                    sync_haseeb_copies()

            except Exception:
                pass

    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
    if total_added_anime > 0 or total_added_episodes > 0 or total_added_streams > 0:
        sync_haseeb_copies()

    return total_added_anime, total_added_episodes, total_added_streams


def main():
    global log_fh
    log_fh = open(LOG_FILE, "a", encoding="utf-8")

    log("=" * 70)
    log("Haseeb Server (Server 3) Direct ID Probing Engine Starting")
    log("=" * 70)

    conn = sqlite3.connect(HASEEB_DB_PATH, timeout=30)
    init_haseeb_db(conn)

    ref_by_al = load_hianime_reference()
    log(f"Loaded {len(ref_by_al):,} reference anime mappings from hianime.db")

    while True:
        try:
            a, e, s = run_full_probe_cycle(conn, ref_by_al)
            log(f"Cycle finished: +{a:,} new anime, +{e:,} episodes, +{s:,} streams added to Haseeb Server.")
        except Exception as err:
            log(f"Cycle error: {err}")

        log("Waiting 3 minutes before next continuous discovery sweep...")
        time.sleep(180)


if __name__ == "__main__":
    main()
