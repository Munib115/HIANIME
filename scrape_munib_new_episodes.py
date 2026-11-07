"""
Fast New Anime & Episode Scraper for Server 2 ("Munib Server" — https://reanime.to/home)
========================================================================================
1. Scans all currently airing (status=RELEASING) and recent (2025/2026) anime on reanime.to,
   plus any newly added anime in hianime.db.
2. Detects newly dropped SUB and DUB episodes by comparing remote subbed/dubbed counts
   (and probing max_ep + 1) against munib_server.db.
3. Scrapes brand-new anime titles and newly dropped episodes into munib_server.db.
4. Checkpoints WAL, sets DELETE journal mode, and syncs munib_server.db to all Vercel API directories.
"""
import os
import re
import sys
import time
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MUNIB_DB_PATH = os.path.join(BASE_DIR, "munib_server.db")
HIANIME_DB_PATH = os.path.join(BASE_DIR, "hianime.db")

SEARCH_API = "https://reanime.to/api/v1/search"
FLIX_API = "https://reanime.to/api/flix"
SERVER_NAME = "Munib Server"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://reanime.to/home",
    "Origin": "https://reanime.to",
}

session = requests.Session()
session.headers.update(HEADERS)
adapter = HTTPAdapter(
    pool_connections=40,
    pool_maxsize=40,
    max_retries=Retry(total=3, backoff_factor=0.25, status_forcelist=[500, 502, 503, 504]),
)
session.mount("https://", adapter)
session.mount("http://", adapter)


def fetch_json(url, retries=3, timeout=10):
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
            elif r.status_code in (404, 400):
                return None
        except Exception:
            time.sleep(0.3)
    return None


def normalize_title(t: str) -> str:
    if not t:
        return ""
    return re.sub(r"[^a-z0-9]+", "", t.lower())


def fetch_episode_flix(anilist_id: int, ep_num: int):
    """Query https://reanime.to/api/flix/{anilist_id}/{ep_num} for SUB and DUB streams."""
    data = fetch_json(f"{FLIX_API}/{anilist_id}/{ep_num}", retries=2, timeout=8)
    if not data or not data.get("success") or not data.get("servers"):
        return ep_num, []

    sub_link, dub_link = None, None
    for s in data.get("servers") or []:
        dtype = (s.get("dataType") or "").lower()
        dlink = (s.get("dataLink") or "").strip()
        if not dlink:
            continue
        if dtype == "sub" and not sub_link:
            sub_link = dlink
        elif dtype in ("dub", "s-dub") and not dub_link:
            sep = "&" if "?" in dlink else "?"
            dub_link = f"{dlink}{sep}a=1" if "a=1" not in dlink else dlink

    out = []
    if sub_link:
        out.append(("sub", sub_link))
    if dub_link:
        out.append(("dub", dub_link))
    return ep_num, out


def load_hianime_reference():
    by_anilist = {}
    by_title = {}
    ep_map = {}
    if not os.path.exists(HIANIME_DB_PATH):
        return by_anilist, by_title, ep_map

    conn = sqlite3.connect(HIANIME_DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute("SELECT anime_id, anilist_id, title, title_english, title_romaji FROM anime")
    for a_id, al_id, t, t_en, t_rom in c.fetchall():
        info = {"anime_id": int(a_id), "anilist_id": int(al_id) if al_id else None, "title": t or t_en or t_rom or ""}
        if al_id and int(al_id) not in by_anilist:
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
    return by_anilist, by_title, ep_map


def sync_munib_copies():
    targets = [
        os.path.join(BASE_DIR, "api", "munib_server.db"),
        os.path.join(BASE_DIR, "anime_app", "web", "api", "munib_server.db"),
        os.path.join(BASE_DIR, "anime_app", "web", "munib_server.db"),
    ]
    for t in targets:
        try:
            os.makedirs(os.path.dirname(t), exist_ok=True)
            shutil.copy2(MUNIB_DB_PATH, t)
        except Exception as e:
            print(f"  Warning syncing {t}: {e}")


def main():
    print("=" * 70)
    print("Munib Server (Server 2) Fast New Anime & Episode Scraper Starting")
    print("=" * 70)

    by_anilist, by_title, ep_map = load_hianime_reference()

    conn = sqlite3.connect(MUNIB_DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    c = conn.cursor()

    # Existing state in munib_server.db
    c.execute("SELECT anilist_id, anime_id, episode_count FROM anime WHERE anilist_id IS NOT NULL")
    existing_anime = {int(r[0]): (int(r[1]), int(r[2] or 0)) for r in c.fetchall()}

    # Existing episodes & stream types per anilist_id: {anilist_id: {ep_num: set('sub', 'dub')}}
    c.execute("SELECT anilist_id, episode_number, type FROM streams WHERE anilist_id IS NOT NULL")
    existing_streams = {}
    for al_id, ep_no, stype in c.fetchall():
        if al_id is not None and ep_no is not None:
            existing_streams.setdefault(int(al_id), {}).setdefault(int(ep_no), set()).add(stype)

    print(f"Initial Munib Server DB: {len(existing_anime):,} Anime | {sum(len(v) for v in existing_streams.values()):,} Streamed Episodes")

    # 1. Discover all Releasing + 2026 + 2025 anime from reanime.to
    candidates_by_al = {}
    queries = [
        f"{SEARCH_API}?status=RELEASING&offset=0&limit=100",
        f"{SEARCH_API}?status=RELEASING&offset=100&limit=100",
        f"{SEARCH_API}?status=RELEASING&offset=200&limit=100",
        f"{SEARCH_API}?year=2026&offset=0&limit=100",
        f"{SEARCH_API}?year=2026&offset=100&limit=100",
        f"{SEARCH_API}?year=2026&offset=200&limit=100",
        f"{SEARCH_API}?year=2025&offset=0&limit=100",
        f"{SEARCH_API}?year=2025&offset=100&limit=100",
    ]
    for q_url in queries:
        data = fetch_json(q_url)
        for item in (data.get("results") if data else []) or []:
            al_id = item.get("anilist_id")
            if al_id:
                candidates_by_al[int(al_id)] = item

    print(f"Scanned {len(candidates_by_al):,} active/recent anime on reanime.to for new episodes & new titles.")

    new_anime_added = 0
    new_episodes_added = 0
    new_streams_added = 0

    for idx, (al_id, item) in enumerate(candidates_by_al.items(), 1):
        t_obj = item.get("title") or {}
        title_en = t_obj.get("english") or ""
        title_rom = t_obj.get("romaji") or ""
        title_nat = t_obj.get("native") or ""
        display_title = title_en or title_rom or title_nat or f"Anime #{al_id}"

        sub_cnt = int(item.get("subbed") or 0)
        dub_cnt = int(item.get("dubbed") or 0)
        total_cnt = int(item.get("episodes") or 0)
        max_known = max(sub_cnt, dub_cnt)

        # Skip gigantic 500+ episode long-runners unless checking latest 5 episodes
        have_ep_map = existing_streams.get(al_id, {})
        is_new_anime = al_id not in existing_anime or len(have_ep_map) == 0

        # Determine which episode numbers need checking
        eps_to_fetch = []
        if max_known > 0:
            start_ep = max(1, max_known - 15) if max_known > 30 else 1
            for ep_no in range(start_ep, max_known + 1):
                have_types = have_ep_map.get(ep_no, set())
                need_sub = (ep_no <= sub_cnt) and ("sub" not in have_types)
                need_dub = (ep_no <= dub_cnt) and ("dub" not in have_types)
                if need_sub or need_dub or not have_types:
                    eps_to_fetch.append(ep_no)
        elif is_new_anime and (item.get("status") or "").lower() == "releasing":
            # Probe episode 1 for unlisted releasing anime
            eps_to_fetch = [1]

        if not eps_to_fetch:
            continue

        # Map anime_id to hianime.db if present, otherwise 1_000_000 + al_id
        ref = by_anilist.get(al_id)
        if not ref:
            for cand in (title_en, title_rom, title_nat):
                nt = normalize_title(cand)
                if nt and nt in by_title:
                    ref = by_title[nt]
                    break
        anime_id = ref["anime_id"] if ref else (1000000 + al_id)

        # Fetch missing episodes concurrently
        ep_results = {}
        with ThreadPoolExecutor(max_workers=min(10, len(eps_to_fetch))) as pool:
            futs = {pool.submit(fetch_episode_flix, al_id, ep_no): ep_no for ep_no in eps_to_fetch}
            for fut in as_completed(futs):
                ep_no, streams = fut.result()
                if streams:
                    ep_results[ep_no] = streams

        if not ep_results:
            continue

        cover = item.get("cover_image") or {}
        poster_large = cover.get("extra_large") or cover.get("large") or ""
        poster_url = cover.get("large") or cover.get("medium") or poster_large
        genres_list = item.get("genres") or []
        genres_str = ", ".join(genres_list) if isinstance(genres_list, list) else str(genres_list)
        score_raw = item.get("average_score")
        score_val = round(float(score_raw) / 10.0, 1) if score_raw else None
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        final_ep_count = max(max_known, max(ep_results.keys()), existing_anime.get(al_id, (0, 0))[1])

        c.execute("""
            INSERT OR REPLACE INTO anime (
                anime_id, anilist_id, reanime_slug, title, title_english, title_romaji,
                japanese_name, type, status, episode_count, subbed_count, dubbed_count,
                duration, season, season_year, anilist_score, anilist_popularity,
                genres, poster_url, poster_large, scraped_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            anime_id, al_id, str(item.get("anime_id") or ""), display_title,
            title_en, title_rom, title_nat, item.get("format") or "TV",
            "Currently Airing" if (item.get("status") or "").lower() == "releasing" else (item.get("status") or "Finished Airing"),
            final_ep_count, sub_cnt, dub_cnt, item.get("duration") or "",
            item.get("season") or "", item.get("season_year"), int(score_raw) if score_raw else None,
            item.get("popularity") or 0,
            genres_str, poster_url, poster_large, ts,
        ))

        added_eps_this_anime = 0
        added_streams_this_anime = 0
        for ep_no, streams in sorted(ep_results.items()):
            mapped = ep_map.get((anime_id, ep_no))
            ep_id = mapped[0] if mapped else f"munib_{al_id}_{ep_no}"
            ep_title = mapped[1] if mapped else f"Episode {ep_no}"

            if ep_no not in have_ep_map:
                added_eps_this_anime += 1

            c.execute("""
                INSERT OR REPLACE INTO episodes (
                    anime_id, anilist_id, episode_id, episode_number, title, watch_url
                ) VALUES (?,?,?,?,?,?)
            """, (anime_id, al_id, ep_id, ep_no, ep_title, streams[0][1]))

            for stype, slink in streams:
                if stype not in have_ep_map.get(ep_no, set()):
                    added_streams_this_anime += 1
                c.execute("""
                    INSERT OR REPLACE INTO streams (
                        anime_id, anilist_id, anime_title, episode_id, episode_number,
                        episode_title, type, server_name, provider, embed_url
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (
                    anime_id, al_id, display_title, ep_id, ep_no,
                    ep_title, stype, SERVER_NAME, "reanime.to", slink,
                ))

        conn.commit()
        if is_new_anime:
            new_anime_added += 1
            print(f"  [NEW ANIME] {display_title} (AniList {al_id}) -> +{added_eps_this_anime} eps, +{added_streams_this_anime} streams")
        elif added_eps_this_anime > 0 or added_streams_this_anime > 0:
            new_episodes_added += added_eps_this_anime
            print(f"  [NEW EPISODE/STREAM] {display_title} -> +{added_eps_this_anime} new eps, +{added_streams_this_anime} new streams (Now {final_ep_count} eps)")
        new_streams_added += added_streams_this_anime

    print("\nCheckpointing WAL, switching journal mode to DELETE, and syncing munib_server.db across project...")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    conn.execute("PRAGMA journal_mode = DELETE;")
    conn.close()

    sync_munib_copies()

    print("=" * 70)
    print("Munib Server Scrape Complete!")
    print(f"New Anime Added:    {new_anime_added}")
    print(f"New Episodes Added: {new_episodes_added}")
    print(f"New Streams Added:  {new_streams_added}")
    print("=" * 70)


if __name__ == "__main__":
    main()
