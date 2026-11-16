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
MUNIB_DB_PATH = os.environ.get("MUNIB_DB") or os.path.join(BASE_DIR, "munib_server.db")
HIANIME_DB_PATH = os.environ.get("ANIME_DB") or os.path.join(BASE_DIR, "hianime.db")

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


def extract_anilist_from_item(item: dict) -> int:
    """Resolve real AniList ID from search payload (including cover image URL bx<id>- fallback)."""
    al = item.get("anilist_id")
    if al and int(al) > 0:
        return int(al)
    cover = item.get("cover_image") or {}
    for k in ("extra_large", "large", "medium"):
        u = str(cover.get(k) or "")
        m = re.search(r"/bx(\d+)-", u)
        if m:
            return int(m.group(1))
    return 0


def resolve_anime_ids(slug: str) -> tuple[int, int, int]:
    """Query /api/v1/anime/{slug} to recover real (anilist_id, mal_id, tmdb_id) when search has anilist_id=0."""
    if not slug:
        return 0, 0, 0
    data = fetch_json(f"https://reanime.to/api/v1/anime/{slug}", retries=2, timeout=8)
    if not isinstance(data, dict):
        return 0, 0, 0
    return (
        int(data.get("anilist_id") or 0),
        int(data.get("mal_id") or 0),
        int(data.get("themoviedb_id") or 0),
    )


def fetch_episode_flix(anilist_id: int, ep_num: int, mal_id: int = 0, tmdb_id: int = 0):
    """Query https://reanime.to/api/flix/{anilist_id}/{ep_num} (or mal/tmdb fallback) for SUB and DUB streams."""
    if anilist_id and int(anilist_id) > 0:
        url = f"{FLIX_API}/{int(anilist_id)}/{ep_num}"
    elif mal_id and int(mal_id) > 0:
        url = f"{FLIX_API}/0/{ep_num}?mal={int(mal_id)}"
    elif tmdb_id and int(tmdb_id) > 0:
        url = f"{FLIX_API}/0/{ep_num}?tmdb={int(tmdb_id)}&season=1"
    else:
        return ep_num, []

    data = fetch_json(url, retries=2, timeout=8)
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
    if os.environ.get("SKIP_GZ_SYNC") or os.environ.get("RENDER"):
        return
    import gzip
    gz_targets = [
        os.path.join(BASE_DIR, "api", "munib_server.db.gz"),
        os.path.join(BASE_DIR, "anime_app", "web", "api", "munib_server.db.gz"),
    ]
    raw_bytes = open(MUNIB_DB_PATH, "rb").read()
    comp_bytes = gzip.compress(raw_bytes, compresslevel=6)
    for t in gz_targets:
        try:
            os.makedirs(os.path.dirname(t), exist_ok=True)
            with open(t, "wb") as f:
                f.write(comp_bytes)
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
    c.execute("SELECT anilist_id, anime_id, episode_count, reanime_slug FROM anime")
    existing_anime = {}
    existing_slugs = {}
    used_anime_ids = {}
    for al_r, aid_r, ep_r, slug_r in c.fetchall():
        aid_int = int(aid_r)
        al_int = int(al_r) if al_r else 0
        ep_int = int(ep_r or 0)
        if al_int > 0:
            existing_anime[al_int] = (aid_int, ep_int)
        if slug_r:
            existing_slugs[str(slug_r)] = (aid_int, al_int, ep_int)
        used_anime_ids[aid_int] = al_int or str(slug_r or aid_int)

    # Existing episodes & stream types per anime_id and anilist_id
    c.execute("SELECT anime_id, anilist_id, episode_number, type FROM streams")
    existing_streams_by_al = {}
    existing_streams_by_aid = {}
    for aid_r, al_id, ep_no, stype in c.fetchall():
        if ep_no is None or not stype:
            continue
        ep_i = int(ep_no)
        if al_id is not None and int(al_id) > 0:
            existing_streams_by_al.setdefault(int(al_id), {}).setdefault(ep_i, set()).add(stype)
        if aid_r is not None:
            existing_streams_by_aid.setdefault(int(aid_r), {}).setdefault(ep_i, set()).add(stype)

    print(f"Initial Munib Server DB: {len(used_anime_ids):,} Anime | {sum(len(v) for v in existing_streams_by_aid.values()):,} Streamed Episodes")

    # 1. Discover full reanime.to catalog (all archive pages + RELEASING + 2026 + Hentai)
    MAX_NEW_ANIME = int(os.environ.get("MAX_NEW_ANIME_PER_RUN", "100"))

    first_cat = fetch_json(f"{SEARCH_API}?offset=0&limit=100", retries=3, timeout=10)
    total_cat = int((first_cat or {}).get("total") or 20872)
    hentai_first = fetch_json(f"{SEARCH_API}?genre=Hentai&offset=0&limit=100", retries=2, timeout=10)
    hentai_total = int((hentai_first or {}).get("total") or 1600)

    queries = [
        f"{SEARCH_API}?status=RELEASING&offset={o}&limit=100" for o in range(0, 400, 100)
    ] + [
        f"{SEARCH_API}?year=2026&offset={o}&limit=100" for o in range(0, 400, 100)
    ] + [
        f"{SEARCH_API}?year=2025&offset={o}&limit=100" for o in range(0, 300, 100)
    ] + [
        f"{SEARCH_API}?offset={o}&limit=100" for o in range(100, total_cat, 100)
    ] + [
        f"{SEARCH_API}?genre=Hentai&offset={o}&limit=100" for o in range(100, hentai_total, 100)
    ]

    # Also include explicit search queries for newly added hianime.db titles missing from munib_server.db
    missing_refs = [info for al_id, info in by_anilist.items() if al_id not in existing_anime and info.get("title")][:30]
    for mref in missing_refs:
        q_str = requests.utils.quote(mref["title"][:40])
        queries.append(f"{SEARCH_API}?q={q_str}&limit=5")

    candidates_by_slug = {}
    for seed_res in (first_cat, hentai_first):
        for item in ((seed_res or {}).get("results") or []):
            slug = str(item.get("anime_id") or "")
            if slug:
                candidates_by_slug[slug] = item

    with ThreadPoolExecutor(max_workers=14) as pool:
        for data in pool.map(fetch_json, queries):
            for item in (data.get("results") if data else []) or []:
                slug = str(item.get("anime_id") or "")
                if slug:
                    candidates_by_slug[slug] = item

    print(f"Scanned {len(candidates_by_slug):,} unique titles across reanime.to for new episodes & new anime.", flush=True)

    # Resolve real AniList / MAL / TMDB IDs for active/streamable items whose search record had anilist_id=0
    zero_al_slugs = []
    for slug, item in candidates_by_slug.items():
        al = extract_anilist_from_item(item)
        if al > 0:
            item["_al_id"] = al
            item["_mal_id"] = 0
            item["_tmdb_id"] = 0
        else:
            sub_cnt = int(item.get("subbed") or 0)
            dub_cnt = int(item.get("dubbed") or 0)
            if max(sub_cnt, dub_cnt) > 0:
                zero_al_slugs.append(slug)

    if zero_al_slugs:
        print(f"Resolving real AniList/MAL IDs for {len(zero_al_slugs)} unindexed reanime.to titles...", flush=True)
        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = {pool.submit(resolve_anime_ids, slug): slug for slug in zero_al_slugs}
            for fut in as_completed(futs):
                slug = futs[fut]
                al_r, mal_r, tmdb_r = fut.result()
                candidates_by_slug[slug]["_al_id"] = al_r
                candidates_by_slug[slug]["_mal_id"] = mal_r
                candidates_by_slug[slug]["_tmdb_id"] = tmdb_r

    # Separate existing-anime episode updates (always run 100%) vs brand-new anime additions (prioritized)
    prepared = []
    for slug, item in candidates_by_slug.items():
        al_id = int(item.get("_al_id") or 0)
        mal_id = int(item.get("_mal_id") or 0)
        tmdb_id = int(item.get("_tmdb_id") or 0)
        if al_id <= 0 and mal_id <= 0 and tmdb_id <= 0:
            continue

        sub_cnt = int(item.get("subbed") or 0)
        dub_cnt = int(item.get("dubbed") or 0)
        max_known = max(sub_cnt, dub_cnt)
        is_rel = (item.get("status") or "").lower() == "releasing"

        existing_info = existing_anime.get(al_id) if al_id > 0 else None
        if not existing_info and slug in existing_slugs:
            aid_s, al_s, ep_s = existing_slugs[slug]
            existing_info = (aid_s, ep_s)
            if al_id <= 0 and al_s > 0:
                al_id = al_s

        have_ep_map = (
            existing_streams_by_aid.get(existing_info[0], {})
            if existing_info
            else existing_streams_by_al.get(al_id, {})
        )
        is_new_anime = (existing_info is None) or (len(have_ep_map) == 0)

        if is_new_anime and max_known == 0 and not is_rel:
            continue

        eps_to_fetch = []
        if max_known > 0:
            if is_new_anime:
                eps_to_fetch = list(range(1, max_known + 1))
            else:
                # Check tail window for newly dropped SUB/DUB episodes + any newly dubbed earlier range
                tail_start = max(1, max_known - 18) if max_known > 30 else 1
                needed = set()
                for ep_no in range(tail_start, max_known + 1):
                    ht = have_ep_map.get(ep_no, set())
                    if (ep_no <= sub_cnt and "sub" not in ht) or (ep_no <= dub_cnt and "dub" not in ht) or not ht:
                        needed.add(ep_no)
                if dub_cnt > 0:
                    have_dub_total = sum(1 for types in have_ep_map.values() if "dub" in types)
                    if dub_cnt > have_dub_total:
                        for ep_no in range(1, dub_cnt + 1):
                            if "dub" not in have_ep_map.get(ep_no, set()):
                                needed.add(ep_no)
                                if len(needed) >= 50:
                                    break
                eps_to_fetch = sorted(needed)
        elif is_new_anime and is_rel:
            eps_to_fetch = [1]

        if not eps_to_fetch:
            continue

        pop = int(item.get("popularity") or 0)
        yr = int(item.get("season_year") or 0)
        prio = (1 if not is_new_anime else 0, 1 if is_rel else 0, 1 if yr >= 2025 else 0, pop)
        prepared.append((prio, is_new_anime, slug, al_id, mal_id, tmdb_id, item, existing_info, have_ep_map, eps_to_fetch))

    prepared.sort(key=lambda x: x[0], reverse=True)

    new_anime_added = 0
    new_episodes_added = 0
    new_streams_added = 0

    for _prio, is_new_anime, slug, al_id, mal_id, tmdb_id, item, existing_info, have_ep_map, eps_to_fetch in prepared:
        if is_new_anime and MAX_NEW_ANIME > 0 and new_anime_added >= MAX_NEW_ANIME:
            continue

        t_obj = item.get("title") or {}
        title_en = t_obj.get("english") or ""
        title_rom = t_obj.get("romaji") or ""
        title_nat = t_obj.get("native") or ""
        display_title = title_en or title_rom or title_nat or f"Anime #{al_id or mal_id}"

        sub_cnt = int(item.get("subbed") or 0)
        dub_cnt = int(item.get("dubbed") or 0)
        max_known = max(sub_cnt, dub_cnt)

        # Preserve existing anime_id or assign unique ID safely (avoid cross-title primary key collision)
        if existing_info:
            anime_id = existing_info[0]
        else:
            ref = by_anilist.get(al_id) if al_id > 0 else None
            if not ref:
                for cand in (title_en, title_rom, title_nat):
                    nt = normalize_title(cand)
                    if nt and nt in by_title:
                        cand_ref = by_title[nt]
                        cand_al = cand_ref.get("anilist_id")
                        if not al_id or not cand_al or int(cand_al) == al_id:
                            ref = cand_ref
                            break
            if ref and ref["anime_id"] not in used_anime_ids:
                anime_id = ref["anime_id"]
            elif al_id > 0:
                anime_id = 1000000 + al_id
            elif mal_id > 0:
                anime_id = 2000000 + mal_id
            else:
                anime_id = 3000000 + tmdb_id
            while anime_id in used_anime_ids and used_anime_ids[anime_id] != (al_id or slug):
                anime_id += 1

        # Skip redundant episode probes if existing anime's missing dubs don't resolve upstream
        ep_results = {}
        with ThreadPoolExecutor(max_workers=min(12, len(eps_to_fetch))) as pool:
            futs = {
                pool.submit(fetch_episode_flix, al_id, ep_no, mal_id, tmdb_id): ep_no
                for ep_no in eps_to_fetch
            }
            for fut in as_completed(futs):
                ep_no, streams = fut.result()
                if streams:
                    ep_results[ep_no] = streams

        if not ep_results:
            continue

        # Check if anything actually new is being inserted before committing
        actually_new_streams = 0
        for ep_no, streams in ep_results.items():
            ht = have_ep_map.get(ep_no, set())
            for stype, _slink in streams:
                if stype not in ht:
                    actually_new_streams += 1
        if not is_new_anime and actually_new_streams == 0:
            continue

        cover = item.get("cover_image") or {}
        poster_large = cover.get("extra_large") or cover.get("large") or ""
        poster_url = cover.get("large") or cover.get("medium") or poster_large
        genres_list = item.get("genres") or []
        genres_str = ", ".join(genres_list) if isinstance(genres_list, list) else str(genres_list)
        score_raw = item.get("average_score")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        prev_ep_cnt = existing_info[1] if existing_info else 0
        final_ep_count = max(max_known, max(ep_results.keys()), prev_ep_cnt)

        db_al_id = al_id if al_id > 0 else None
        c.execute("""
            INSERT OR REPLACE INTO anime (
                anime_id, anilist_id, reanime_slug, title, title_english, title_romaji,
                japanese_name, type, status, episode_count, subbed_count, dubbed_count,
                duration, season, season_year, anilist_score, anilist_popularity,
                genres, poster_url, poster_large, scraped_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            anime_id, db_al_id, slug, display_title,
            title_en, title_rom, title_nat, item.get("format") or "TV",
            "Currently Airing" if (item.get("status") or "").lower() == "releasing" else (item.get("status") or "Finished Airing"),
            final_ep_count, sub_cnt, dub_cnt, item.get("duration") or "",
            item.get("season") or "", item.get("season_year"), int(score_raw) if score_raw else None,
            item.get("popularity") or 0,
            genres_str, poster_url, poster_large, ts,
        ))
        used_anime_ids[anime_id] = al_id or slug

        added_eps_this_anime = 0
        added_streams_this_anime = 0
        for ep_no, streams in sorted(ep_results.items()):
            mapped = ep_map.get((anime_id, ep_no))
            ep_id = mapped[0] if mapped else f"munib_{al_id or mal_id or tmdb_id}_{ep_no}"
            ep_title = mapped[1] if mapped else f"Episode {ep_no}"

            if ep_no not in have_ep_map:
                added_eps_this_anime += 1

            c.execute("""
                INSERT OR REPLACE INTO episodes (
                    anime_id, anilist_id, episode_id, episode_number, title, watch_url
                ) VALUES (?,?,?,?,?,?)
            """, (anime_id, db_al_id, ep_id, ep_no, ep_title, streams[0][1]))

            for stype, slink in streams:
                if stype not in have_ep_map.get(ep_no, set()):
                    added_streams_this_anime += 1
                    have_ep_map.setdefault(ep_no, set()).add(stype)
                c.execute("""
                    INSERT OR REPLACE INTO streams (
                        anime_id, anilist_id, anime_title, episode_id, episode_number,
                        episode_title, type, server_name, provider, embed_url
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (
                    anime_id, db_al_id, display_title, ep_id, ep_no,
                    ep_title, stype, SERVER_NAME, "reanime.to", slink,
                ))

        conn.commit()
        if is_new_anime:
            new_anime_added += 1
            print(f"  [NEW ANIME] {display_title} (AniList {al_id or 'N/A'}) -> +{added_eps_this_anime} eps, +{added_streams_this_anime} streams", flush=True)
        elif added_eps_this_anime > 0 or added_streams_this_anime > 0:
            new_episodes_added += added_eps_this_anime
            print(f"  [NEW EPISODE/STREAM] {display_title} -> +{added_eps_this_anime} new eps, +{added_streams_this_anime} new streams (Now {final_ep_count} eps)", flush=True)
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
