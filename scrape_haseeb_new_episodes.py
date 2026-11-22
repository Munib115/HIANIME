"""
Fast New Anime & Episode Scraper for Server 3 ("Haseeb Server" — https://onianime.eu/home)
========================================================================================
1. Scans recently added, popular, and currently releasing anime on onianime.eu.
2. Detects newly dropped episodes and brand-new anime titles.
3. Scrapes direct HLS stream links under server_name = 'Haseeb' into haseeb_server.db.
4. Checkpoints WAL, sets DELETE journal mode, and syncs haseeb_server.db.gz across API directories.
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

ONIANIME_BASE = "https://onianime.eu"
POPULAR_API = f"{ONIANIME_BASE}/api/animes/popular"
RECOMMENDED_API = f"{ONIANIME_BASE}/api/animes/recommended"
CATALOG_API = f"{ONIANIME_BASE}/api/catalog"
ANIME_API = f"{ONIANIME_BASE}/api/anime"

SERVER_NAME = "Haseeb"
PROVIDER_NAME = "onianime.eu"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://onianime.eu/home",
    "Origin": "https://onianime.eu",
}

session = requests.Session()
session.headers.update(HEADERS)
adapter = HTTPAdapter(
    pool_connections=40,
    pool_maxsize=40,
    max_retries=Retry(total=3, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504]),
)
session.mount("https://", adapter)
session.mount("http://", adapter)


def fetch_json(url: str, params: dict = None, retries: int = 3, timeout: int = 8):
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                time.sleep(1.2 * (attempt + 1))
            elif r.status_code in (404, 400):
                return None
        except Exception:
            time.sleep(0.2)
    return None


def sync_haseeb_copies():
    if not os.path.exists(HASEEB_DB_PATH):
        return
    try:
        conn = sqlite3.connect(HASEEB_DB_PATH)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.execute("PRAGMA journal_mode = DELETE;")
        conn.close()

        with open(HASEEB_DB_PATH, "rb") as f_in:
            data = f_in.read()

        targets = [
            os.path.join(BASE_DIR, "api", "haseeb_server.db.gz"),
            os.path.join(BASE_DIR, "anime_app", "web", "api", "haseeb_server.db.gz"),
        ]
        for t in targets:
            os.makedirs(os.path.dirname(t), exist_ok=True)
            with gzip.open(t, "wb") as f_out:
                f_out.write(data)
    except Exception as e:
        print(f"Error syncing haseeb_server.db archives: {e}")


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


def main():
    print("=" * 70)
    print("Haseeb Server (Server 3) Fast New Anime & Episode Scraper Starting")
    print("=" * 70)

    if not os.path.exists(HASEEB_DB_PATH):
        import haseeb_server_scraper
        haseeb_server_scraper.main()
        return

    conn = sqlite3.connect(HASEEB_DB_PATH, timeout=20)
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM anime")
    init_anime = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM episodes")
    init_eps = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM streams")
    init_streams = c.fetchone()[0]
    print(f"Initial Haseeb Server DB: {init_anime:,} Anime | {init_eps:,} Episodes | {init_streams:,} Streams")

    ref_by_al = load_hianime_reference()

    candidate_items = []
    seen_ids = set()

    pop_data = fetch_json(POPULAR_API) or {}
    for k in ("recentlyAdded", "currentSeason", "sliderMovies"):
        for it in (pop_data.get(k) or []):
            if it.get("id") and it["id"] not in seen_ids:
                seen_ids.add(it["id"])
                candidate_items.append(it)

    rec_data = fetch_json(RECOMMENDED_API) or []
    for it in rec_data:
        if it.get("id") and it["id"] not in seen_ids:
            seen_ids.add(it["id"])
            candidate_items.append(it)

    for p in range(1, 4):
        cat_p = fetch_json(f"{CATALOG_API}?page={p}") or {}
        for it in (cat_p.get("animes") or []):
            if it.get("id") and it["id"] not in seen_ids:
                seen_ids.add(it["id"])
                candidate_items.append(it)

    print(f"Discovered {len(candidate_items)} priority items to check for updates.")

    c.execute("SELECT onianime_id, episode_count FROM anime")
    existing_oni_counts = dict(c.fetchall())

    new_anime_added = 0
    new_eps_added = 0
    new_streams_added = 0

    for item in candidate_items:
        oni_id = item.get("id")
        if not oni_id:
            continue

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

        ep_data = fetch_json(f"{ANIME_API}/{oni_id}/episodes/new")
        if not ep_data or not ep_data.get("episodes"):
            continue

        episodes = ep_data.get("episodes", [])
        if not episodes:
            continue

        prev_count = existing_oni_counts.get(int(oni_id), 0)
        is_new_anime = int(oni_id) not in existing_oni_counts

        c.execute("SELECT episode_number FROM episodes WHERE anime_id = ?", (anime_id,))
        have_eps = set(r[0] for r in c.fetchall())

        ep_insert_list = []
        stream_insert_list = []

        for ep_obj in episodes:
            ep_no = int(ep_obj.get("ep") or 1)
            if ep_no in have_eps and not is_new_anime:
                continue

            ep_title = ep_obj.get("title") or f"Episode {ep_no}"
            ep_id = f"haseeb_{al_id or oni_id}_{ep_no}"

            sub_prov = ep_obj.get("subProviders") or []
            dub_prov = ep_obj.get("dubProviders") or []

            watch_url = ""
            if ep_obj.get("hasSub", True):
                src, prov = resolve_single_stream(oni_id, ep_no, "sub", sub_prov)
                if src:
                    watch_url = src
                    stream_insert_list.append((
                        anime_id, al_id, display_title, ep_id, ep_no,
                        ep_title, "sub", SERVER_NAME, prov or PROVIDER_NAME, src
                    ))

            if ep_obj.get("hasDub", False):
                src, prov = resolve_single_stream(oni_id, ep_no, "dub", dub_prov)
                if src:
                    if not watch_url:
                        watch_url = src
                    stream_insert_list.append((
                        anime_id, al_id, display_title, ep_id, ep_no,
                        ep_title, "dub", SERVER_NAME, prov or PROVIDER_NAME, src
                    ))

            ep_insert_list.append((anime_id, al_id, ep_id, ep_no, ep_title, watch_url))

        if ep_insert_list or is_new_anime:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            c.execute("""
                INSERT OR REPLACE INTO anime (
                    anime_id, onianime_id, anilist_id, title, title_english, title_romaji,
                    japanese_name, type, status, genres, duration, season, season_year,
                    anilist_score, anilist_popularity, poster_url, poster_large,
                    episode_count, subbed_count, dubbed_count, scraped_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                anime_id, int(oni_id), al_id, display_title, title_en, title_nat,
                title_nat, item.get("type") or "TV", item.get("status") or "Finished",
                "", "", item.get("theme") or "", item.get("release_year"),
                None, item.get("watched") or 0, item.get("image") or "", item.get("image") or "",
                len(episodes),
                len(episodes) if any(e.get("hasSub") for e in episodes) else 0,
                len(episodes) if any(e.get("hasDub") for e in episodes) else 0,
                ts,
            ))

            if ep_insert_list:
                c.executemany("""
                    INSERT OR REPLACE INTO episodes (
                        anime_id, anilist_id, episode_id, episode_number, title, watch_url
                    ) VALUES (?,?,?,?,?,?)
                """, ep_insert_list)

            if stream_insert_list:
                c.executemany("""
                    INSERT OR REPLACE INTO streams (
                        anime_id, anilist_id, anime_title, episode_id, episode_number,
                        episode_title, type, server_name, provider, embed_url
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """, stream_insert_list)

            conn.commit()

            if is_new_anime:
                new_anime_added += 1
            new_eps_added += len(ep_insert_list)
            new_streams_added += len(stream_insert_list)
            print(f"  [Update] {display_title[:32]} -> +{len(ep_insert_list)} new eps, +{len(stream_insert_list)} streams")

    print("\nCheckpointing WAL, switching to DELETE journal mode, and compressing archives...")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    conn.execute("PRAGMA journal_mode = DELETE;")
    conn.close()

    sync_haseeb_copies()

    print("=" * 70)
    print("Haseeb Server Update Complete!")
    print(f"New Anime Added:    {new_anime_added}")
    print(f"New Episodes Added: {new_eps_added}")
    print(f"New Streams Added:  {new_streams_added}")
    print("=" * 70)


if __name__ == "__main__":
    main()
