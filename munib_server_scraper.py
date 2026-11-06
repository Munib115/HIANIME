"""
Munib Server Turbo Scraper — Independent Server 2 Engine (https://reanime.to/home)
==================================================================================
- Completely independent SQLite database: munib_server.db
- Never modifies or mixes data with hianime.db (ZokoAnime / HD-1)
- Multi-worker parallel episode stream extraction (ThreadPoolExecutor)
- Extracts both SUB and DUB high-speed stream links under server_name = 'Munib Server'
- Prioritizes https://reanime.to/home & active catalog titles first, then full reanime.to archive
- Auto-resumes cleanly if restarted
"""
import sys
import io
import os
import re
import time
import json
import sqlite3
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ─── Configuration ───────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MUNIB_DB_PATH = os.path.join(BASE_DIR, "munib_server.db")
HIANIME_DB_PATH = os.path.join(BASE_DIR, "hianime.db")
LOG_FILE = os.path.join(BASE_DIR, "munib_scraper.log")

REANIME_BASE = "https://reanime.to"
SEARCH_API = f"{REANIME_BASE}/api/v1/search"
FLIX_API = f"{REANIME_BASE}/api/flix"

SERVER_NAME = "Munib Server"
PROVIDER_NAME = "reanime.to"
EPISODE_WORKERS = 16

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json,text/html,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://reanime.to/home",
}

# ─── High-Throughput HTTP Session ────────────────────────────────────────────
session = requests.Session()
session.headers.update(HEADERS)
adapter = HTTPAdapter(
    pool_connections=40,
    pool_maxsize=40,
    max_retries=Retry(total=3, backoff_factor=0.25, status_forcelist=[500, 502, 503, 504]),
)
session.mount("https://", adapter)
session.mount("http://", adapter)

log_fh = None
rate_lock = Lock()
last_request_time = 0.0
MIN_INTERVAL = 0.025  # ~40 requests/sec across threads


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if log_fh:
        try:
            log_fh.write(line + "\n")
            log_fh.flush()
        except Exception:
            pass


def normalize_title(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]+", "", s.lower()).strip()


def fetch_json(url: str, retries: int = 3, timeout: int = 12):
    global last_request_time
    for attempt in range(retries):
        try:
            with rate_lock:
                now = time.time()
                elapsed = now - last_request_time
                if elapsed < MIN_INTERVAL:
                    time.sleep(MIN_INTERVAL - elapsed)
                last_request_time = time.time()

            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                wait = 3 * (attempt + 1)
                log(f"  [429] Rate limited on {url[:65]} — sleeping {wait}s")
                time.sleep(wait)
            elif r.status_code == 404:
                return None
            else:
                time.sleep(0.4)
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(0.4)
    return None


# ─── Independent Munib Server Database Setup ─────────────────────────────────
def init_munib_db(conn: sqlite3.Connection):
    c = conn.cursor()
    c.execute("PRAGMA journal_mode = WAL;")
    c.execute("PRAGMA synchronous = NORMAL;")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS anime (
        anime_id            INTEGER PRIMARY KEY,
        reanime_slug        TEXT,
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

    CREATE INDEX IF NOT EXISTS idx_munib_anime_al       ON anime(anilist_id);
    CREATE INDEX IF NOT EXISTS idx_munib_ep_anime       ON episodes(anime_id);
    CREATE INDEX IF NOT EXISTS idx_munib_ep_id          ON episodes(episode_id);
    CREATE INDEX IF NOT EXISTS idx_munib_streams_ep     ON streams(episode_id);
    CREATE INDEX IF NOT EXISTS idx_munib_streams_a_ep   ON streams(anime_id, episode_number);
    CREATE INDEX IF NOT EXISTS idx_munib_streams_al_ep  ON streams(anilist_id, episode_number);
    """)
    conn.commit()


def load_reference_catalog():
    """
    Read-only lookup from hianime.db so anime_id and episode_id in munib_server.db
    align seamlessly with the frontend catalog when present, without ever modifying hianime.db.
    """
    by_anilist = {}
    by_title = {}
    ep_map = {}  # (anime_id, episode_number) -> (episode_id, title)

    if not os.path.exists(HIANIME_DB_PATH):
        return by_anilist, by_title, ep_map

    try:
        conn = sqlite3.connect(HIANIME_DB_PATH, timeout=10)
        c = conn.cursor()
        c.execute("""
            SELECT anime_id, anilist_id, title, title_english, title_romaji, episode_count, anilist_popularity
            FROM anime
            ORDER BY anilist_popularity DESC NULLS LAST
        """)
        for a_id, al_id, t, t_en, t_rom, ep_cnt, pop in c.fetchall():
            info = {
                "anime_id": a_id,
                "anilist_id": al_id,
                "title": t or t_en or t_rom or "",
                "episode_count": ep_cnt or 0,
                "popularity": pop or 0,
            }
            if al_id and al_id not in by_anilist:
                by_anilist[int(al_id)] = info
            for cand in (t, t_en, t_rom):
                nt = normalize_title(cand)
                if nt and nt not in by_title:
                    by_title[nt] = info

        c.execute("SELECT anime_id, episode_number, episode_id, title FROM episodes")
        for a_id, ep_num, ep_id, ep_title in c.fetchall():
            if a_id is not None and ep_num is not None:
                ep_map[(int(a_id), int(ep_num))] = (ep_id, ep_title or f"Episode {ep_num}")
        conn.close()
    except Exception as e:
        log(f"Warning reading reference catalog: {e}")

    return by_anilist, by_title, ep_map


def get_existing_episode_map(conn: sqlite3.Connection):
    c = conn.cursor()
    c.execute("SELECT anilist_id, episode_number FROM streams WHERE anilist_id IS NOT NULL")
    ep_map = {}
    for al_id, ep_no in c.fetchall():
        if al_id not in ep_map:
            ep_map[al_id] = set()
        ep_map[al_id].add(ep_no)
    c.execute("SELECT anilist_id FROM anime WHERE anilist_id IS NOT NULL")
    probed_anime = {r[0] for r in c.fetchall()}
    return ep_map, probed_anime


def discover_reanime_catalog():
    """
    Crawl https://reanime.to/home and https://reanime.to/api/v1/search (plus genre=Hentai)
    to collect all anime on reanime.to, including titles where search metadata says subbed=0
    but /api/flix/{anilist_id}/{ep} actually has live streams.
    """
    log("Discovering catalog from https://reanime.to/home and /api/v1/search ...")
    first_page = fetch_json(f"{SEARCH_API}?offset=0&limit=100")
    if not first_page:
        log("Failed to fetch initial search page from reanime.to!")
        return []

    total = int(first_page.get("total") or 20833)
    offsets = list(range(0, total, 100))
    log(f"Total catalog entries on reanime.to: {total:,} ({len(offsets)} pages of 100)")

    catalog_by_al = {}

    def process_results(items):
        for item in items or []:
            al_id = item.get("anilist_id")
            if not al_id:
                continue
            catalog_by_al[int(al_id)] = item

    process_results(first_page.get("results", []))

    def fetch_offset(off):
        data = fetch_json(f"{SEARCH_API}?offset={off}&limit=100")
        return data.get("results", []) if data else []

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(fetch_offset, off): off for off in offsets[1:]}
        done_pages = 1
        for fut in as_completed(futures):
            res = fut.result()
            process_results(res)
            done_pages += 1
            if done_pages % 40 == 0 or done_pages == len(offsets):
                log(f"  Catalog discovery: {done_pages}/{len(offsets)} pages scanned ({len(catalog_by_al):,} streamable anime found)")

    # Explicitly discover the Hentai genre catalog (filtered out by default /api/v1/search)
    log("Discovering Hentai genre catalog from https://reanime.to/api/v1/search?genre=Hentai ...")
    hentai_first = fetch_json(f"{SEARCH_API}?genre=Hentai&offset=0&limit=100")
    if hentai_first:
        hentai_total = int(hentai_first.get("total") or 0)
        hentai_offsets = list(range(0, hentai_total, 100))
        before_cnt = len(catalog_by_al)
        process_results(hentai_first.get("results", []))

        def fetch_hentai_offset(off):
            data = fetch_json(f"{SEARCH_API}?genre=Hentai&offset={off}&limit=100")
            return data.get("results", []) if data else []

        if len(hentai_offsets) > 1:
            with ThreadPoolExecutor(max_workers=12) as pool:
                h_futures = {pool.submit(fetch_hentai_offset, off): off for off in hentai_offsets[1:]}
                for fut in as_completed(h_futures):
                    process_results(fut.result())

        added_hentai = len(catalog_by_al) - before_cnt
        log(f"  Hentai discovery complete: {hentai_total:,} entries scanned ({added_hentai:,} streamable Hentai titles added)")

    return list(catalog_by_al.values())


def fetch_episode_flix(anilist_id: int, ep_num: int):
    """
    Query https://reanime.to/api/flix/{anilist_id}/{ep_num} and extract the SUB and DUB
    stream links for Munib Server.
    """
    url = f"{FLIX_API}/{anilist_id}/{ep_num}"
    data = fetch_json(url, retries=3, timeout=10)
    if not data or not data.get("success") or not data.get("servers"):
        return ep_num, []

    servers = data.get("servers") or []
    sub_link = None
    dub_link = None

    # Prefer HD-1, then HD-2, then H-SUB
    for s in servers:
        dtype = (s.get("dataType") or "").lower()
        dlink = (s.get("dataLink") or "").strip()
        if not dlink:
            continue
        if dtype == "sub" and not sub_link:
            sub_link = dlink
        elif dtype in ("dub", "s-dub") and not dub_link:
            # Append &a=1 (or ?a=1) so flixcloud.cc defaults to audio track 1 (English Dub)
            sep = "&" if "?" in dlink else "?"
            dub_link = f"{dlink}{sep}a=1" if "a=1" not in dlink else dlink

    extracted = []
    if sub_link:
        extracted.append(("sub", sub_link))
    if dub_link:
        extracted.append(("dub", dub_link))

    return ep_num, extracted


def main():
    global log_fh
    log_fh = open(LOG_FILE, "a", encoding="utf-8", errors="replace")

    log("=" * 72)
    log("MUNIB SERVER TURBO SCRAPER — CONTINUOUS 24/7 MODE")
    log(f"Independent Database: {MUNIB_DB_PATH}")
    log(f"Server Name:          {SERVER_NAME}")
    log(f"Source:               https://reanime.to/home")
    log("=" * 72)

    conn = sqlite3.connect(MUNIB_DB_PATH, timeout=30)
    init_munib_db(conn)

    by_anilist, by_title, ep_map = load_reference_catalog()
    log(f"Loaded reference mapping: {len(by_anilist):,} AniList IDs, {len(ep_map):,} episode IDs")

    pass_number = 1
    while True:
        log("=" * 72)
        log(f"STARTING PASS #{pass_number} — Scraping all anime & genres from https://reanime.to/home")
        log("=" * 72)

        try:
            existing_ep_map, probed_anime = get_existing_episode_map(conn)
            log(f"Currently in munib_server.db: {len(existing_ep_map):,} anime with streams ({len(probed_anime):,} total probed)")

            raw_catalog = discover_reanime_catalog()
            if not raw_catalog:
                log("Failed to discover catalog, retrying in 30 seconds...")
                time.sleep(30)
                continue

            # Sort so Hentai titles and reference catalog / highest popularity scrape FIRST
            def sort_key(item):
                al_id = int(item.get("anilist_id") or 0)
                genres_list = item.get("genres") or []
                is_hentai = 1 if "Hentai" in genres_list else 0
                t_obj = item.get("title") or {}
                in_ref = (
                    al_id in by_anilist
                    or normalize_title(t_obj.get("english")) in by_title
                    or normalize_title(t_obj.get("romaji")) in by_title
                )
                has_streams = 1 if ((item.get("subbed") or 0) > 0 or (item.get("dubbed") or 0) > 0) else 0
                pop = int(item.get("popularity") or 0)
                return (is_hentai, has_streams, 1 if in_ref else 0, pop)

            raw_catalog.sort(key=sort_key, reverse=True)

            # Filter targets:
            # 1) Anime with subbed/dubbed > 0 that have missing episodes
            # 2) Any unprobed anime (including subbed=0 Hentai & general titles that still have live /api/flix streams)
            targets = []
            for it in raw_catalog:
                al_id = int(it.get("anilist_id") or 0)
                subbed_cnt = int(it.get("subbed") or 0)
                dubbed_cnt = int(it.get("dubbed") or 0)
                max_ep = max(subbed_cnt, dubbed_cnt)
                done_eps = existing_ep_map.get(al_id, set())

                if max_ep > 0:
                    if len(done_eps) >= max_ep:
                        continue
                    targets.append(it)
                else:
                    # If search metadata says subbed=0, probe once if not yet probed in munib_server.db
                    if al_id not in probed_anime:
                        targets.append(it)

            log(f"Anime queued for stream extraction in Pass #{pass_number}: {len(targets):,}")

            start_time = time.time()
            total_streams_added = 0
            total_eps_added = 0
            processed_anime = 0

            for idx, item in enumerate(targets, 1):
                al_id = int(item["anilist_id"])
                slug = item.get("anime_id") or f"reanime-{al_id}"
                t_obj = item.get("title") or {}
                t_eng = (t_obj.get("english") or "").strip()
                t_rom = (t_obj.get("romaji") or "").strip()
                t_nat = (t_obj.get("native") or "").strip()
                primary_title = t_eng or t_rom or t_nat or slug

                # Match against reference catalog for seamless anime_id alignment
                ref = by_anilist.get(al_id)
                if not ref:
                    ref = by_title.get(normalize_title(t_eng)) or by_title.get(normalize_title(t_rom))

                if ref:
                    anime_id = int(ref["anime_id"])
                    ref_ep_count = int(ref.get("episode_count") or 0)
                else:
                    anime_id = 1000000 + al_id
                    ref_ep_count = 0

                subbed_cnt = int(item.get("subbed") or 0)
                dubbed_cnt = int(item.get("dubbed") or 0)
                item_eps = int(item.get("episodes") or 0)

                max_ep = max(subbed_cnt, dubbed_cnt)
                is_unlisted_probe = (max_ep <= 0)
                if max_ep <= 0:
                    max_ep = max(item_eps, ref_ep_count, 4)
                    max_ep = min(max_ep, 26)
                if max_ep > 1500:
                    max_ep = min(max_ep, max(subbed_cnt, dubbed_cnt, 1200))

                done_eps = existing_ep_map.get(al_id, set())

                # Fast probe for unlisted (subbed=0) titles: check Episode 1 first before probing 2..max_ep
                ep_rows_to_insert = []
                stream_rows_to_insert = []

                if is_unlisted_probe and 1 not in done_eps:
                    _, ep1_streams = fetch_episode_flix(al_id, 1)
                    if not ep1_streams:
                        # No streams on ep 1 -> mark as probed in memory and skip remaining episodes
                        probed_anime.add(al_id)
                        processed_anime += 1
                        continue
                    # Episode 1 HAS streams! Record ep 1 and probe remaining episodes 2..max_ep
                    mapped_ep = ep_map.get((anime_id, 1))
                    ep_id, ep_title = mapped_ep if mapped_ep else (f"munib-{anime_id}-ep-1", "Episode 1")
                    ep_rows_to_insert.append((anime_id, al_id, ep_id, 1, ep_title, f"{REANIME_BASE}/watch/{slug}?ep=1"))
                    for stype, embed_url in ep1_streams:
                        stream_rows_to_insert.append((
                            anime_id, al_id, primary_title, ep_id, 1, ep_title,
                            stype, SERVER_NAME, PROVIDER_NAME, embed_url
                        ))
                    done_eps = set(done_eps) | {1}

                ep_numbers = [ep_no for ep_no in range(1, max_ep + 1) if ep_no not in done_eps]

                if ep_numbers:
                    with ThreadPoolExecutor(max_workers=EPISODE_WORKERS) as ep_pool:
                        futs = {ep_pool.submit(fetch_episode_flix, al_id, ep_no): ep_no for ep_no in ep_numbers}
                        for fut in as_completed(futs):
                            ep_no, streams_found = fut.result()
                            if not streams_found:
                                continue

                            mapped_ep = ep_map.get((anime_id, ep_no))
                            if mapped_ep:
                                ep_id, ep_title = mapped_ep
                            else:
                                ep_id = f"munib-{anime_id}-ep-{ep_no}"
                                ep_title = f"Episode {ep_no}"

                            watch_url = f"{REANIME_BASE}/watch/{slug}?ep={ep_no}"
                            ep_rows_to_insert.append((
                                anime_id, al_id, ep_id, ep_no, ep_title, watch_url
                            ))

                            for stype, embed_url in streams_found:
                                stream_rows_to_insert.append((
                                    anime_id, al_id, primary_title, ep_id, ep_no, ep_title,
                                    stype, SERVER_NAME, PROVIDER_NAME, embed_url
                                ))

                if not ep_rows_to_insert and not stream_rows_to_insert and is_unlisted_probe:
                    probed_anime.add(al_id)
                    processed_anime += 1
                    continue

                actual_ep_cnt = max(max_ep, len(ep_rows_to_insert))
                cover = item.get("cover_image") or {}
                poster_large = cover.get("extra_large") or cover.get("large") or ""
                poster_url = cover.get("large") or poster_large
                genres_str = ", ".join(item.get("genres") or [])

                # Save anime record in munib_server.db
                c = conn.cursor()
                c.execute("""
                    INSERT OR REPLACE INTO anime (
                        anime_id, reanime_slug, anilist_id, title, title_english, title_romaji,
                        japanese_name, type, status, genres, duration, season, season_year,
                        anilist_score, anilist_popularity, poster_url, poster_large,
                        episode_count, subbed_count, dubbed_count, scraped_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """, (
                    anime_id, slug, al_id, primary_title, t_eng or primary_title, t_rom or primary_title,
                    t_nat, item.get("format") or "TV", item.get("status") or "", genres_str,
                    item.get("duration") or "24m", item.get("season") or "", item.get("season_year"),
                    item.get("average_score"), item.get("popularity"), poster_url, poster_large,
                    actual_ep_cnt, max(subbed_cnt, len(ep_rows_to_insert)), dubbed_cnt,
                ))

                if ep_rows_to_insert:
                    c.executemany("""
                        INSERT OR REPLACE INTO episodes (
                            anime_id, anilist_id, episode_id, episode_number, title, watch_url
                        ) VALUES (?, ?, ?, ?, ?, ?)
                    """, ep_rows_to_insert)

                if stream_rows_to_insert:
                    c.executemany("""
                        INSERT OR REPLACE INTO streams (
                            anime_id, anilist_id, anime_title, episode_id, episode_number,
                            episode_title, type, server_name, provider, embed_url
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, stream_rows_to_insert)

                conn.commit()
                probed_anime.add(al_id)

                # Update in-memory set so subsequent passes recognize these episodes
                if al_id not in existing_ep_map:
                    existing_ep_map[al_id] = set()
                for ep_no in ep_numbers:
                    existing_ep_map[al_id].add(ep_no)

                processed_anime += 1
                total_eps_added += len(ep_rows_to_insert)
                total_streams_added += len(stream_rows_to_insert)

                elapsed_min = max((time.time() - start_time) / 60.0, 0.01)
                rate = processed_anime / elapsed_min
                rem_anime = len(targets) - idx
                eta_min = rem_anime / max(rate, 0.1)

                log(
                    f"[{idx}/{len(targets)}] {primary_title[:38]:38s} | "
                    f"ID:{anime_id:<7d} | Eps:{len(ep_rows_to_insert):<4d} | "
                    f"+{len(stream_rows_to_insert):<4d} streams (Total: {total_streams_added:,}) | "
                    f"ETA: {eta_min/60:.1f}h"
                )

            log("=" * 72)
            log(f"PASS #{pass_number} COMPLETE! Added {total_streams_added:,} streams across {processed_anime:,} anime.")
            log("Sleeping 10 minutes before next continuous update cycle...")
            log("=" * 72)

        except Exception as e:
            log(f"Error in pass #{pass_number}: {e}")
            time.sleep(30)

        pass_number += 1
        time.sleep(600)  # Continuous cycle: 10 minute cooldown, then rescans catalog & home releases


if __name__ == "__main__":
    main()
