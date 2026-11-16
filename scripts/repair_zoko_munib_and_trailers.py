"""
Repair Database Collisions, Enforce ZokoAnime + Munib Server Streams, Backfill Missed One Piece & Anime Episodes,
and Replace Wrong Netflix Live-Action One Piece Trailers with Real Official Anime PVs.
"""
import base64
import gzip
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIANIME_DB = os.path.join(BASE_DIR, "hianime.db")
MUNIB_DB = os.path.join(BASE_DIR, "munib_server.db")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
})
adapter = HTTPAdapter(pool_connections=40, pool_maxsize=40, max_retries=Retry(total=2, backoff_factor=0.25))
session.mount("https://", adapter)
session.mount("http://", adapter)


def fetch_json(url, referer="", timeout=10):
    try:
        hdrs = {"Referer": referer} if referer else {}
        r = session.get(url, headers=hdrs, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def sync_gz(db_path: str, filename: str):
    gz_name = f"{filename}.gz"
    targets = [
        os.path.join(BASE_DIR, "api", gz_name),
        os.path.join(BASE_DIR, "anime_app", "web", "api", gz_name),
    ]
    raw = open(db_path, "rb").read()
    comp = gzip.compress(raw, compresslevel=6)
    for t in targets:
        os.makedirs(os.path.dirname(t), exist_ok=True)
        with open(t, "wb") as f:
            f.write(comp)
        print(f"Synced {t} ({len(comp):,} bytes)")


def repair_hianime_db(conn_h: sqlite3.Connection):
    c = conn_h.cursor()
    print("--- 1. Repairing hianime.db (ZokoAnime normalization & AniList collision fixes) ---")

    # A) Convert all zokoanime.video streams wrongly labeled HD-1 / HD-2 / Hd-1 -> ZokoAnime
    rows_zoko_alt = c.execute("""
        SELECT id, episode_id, type, server_name
        FROM streams
        WHERE (provider LIKE '%zokoanime%' OR embed_url LIKE '%zokoanime.video%')
          AND server_name != 'ZokoAnime'
    """).fetchall()
    existing_z = set(c.execute("SELECT episode_id, type FROM streams WHERE server_name = 'ZokoAnime'").fetchall())
    updated_z = 0
    deleted_dup = 0
    for row_id, ep_id, stype, _sname in rows_zoko_alt:
        if (ep_id, stype) in existing_z:
            c.execute("DELETE FROM streams WHERE id = ?", (row_id,))
            deleted_dup += 1
        else:
            c.execute("UPDATE streams SET server_name = 'ZokoAnime' WHERE id = ?", (row_id,))
            existing_z.add((ep_id, stype))
            updated_z += 1
    conn_h.commit()
    print(f"  Converted {updated_z} HD-1/HD-2 zokoanime.video streams -> ZokoAnime (removed {deleted_dup} dups)")

    # B) Fix One Piece Movie 1 (4164) & missing One Piece AniList IDs
    c.execute("""
        UPDATE anime
        SET anilist_id = 459, title_english = 'ONE PIECE: The Movie', type = 'MOVIE'
        WHERE anime_id = 4164
    """)
    for aid, true_al in [(1431, 4155), (1432, 12859), (1433, 105143), (1466, 20835), (4361, 465)]:
        c.execute("UPDATE anime SET anilist_id = ? WHERE anime_id = ? AND (anilist_id IS NULL OR anilist_id != ?)", (true_al, aid, true_al))
    conn_h.commit()

    # C) Resolve duplicate anilist_id collisions where a 1-2 ep Special/Movie/Recap stole a >10 ep TV flagship's anilist_id
    dups = c.execute("""
        SELECT anilist_id
        FROM anime
        WHERE anilist_id IS NOT NULL AND anilist_id > 0
        GROUP BY anilist_id HAVING COUNT(*) > 1
    """).fetchall()
    cleared_cols = 0
    for (al_id,) in dups:
        group = c.execute("""
            SELECT anime_id, title, title_english, COALESCE(episode_count, 0),
                   (SELECT COUNT(*) FROM episodes e WHERE e.anime_id = a.anime_id) AS ep_rows
            FROM anime a WHERE anilist_id = ?
            ORDER BY ep_rows DESC, COALESCE(episode_count, 0) DESC, anime_id ASC
        """, (al_id,)).fetchall()
        if not group:
            continue
        top_aid, top_t, top_en, top_ec, top_rows = group[0]
        top_max = max(top_ec or 0, top_rows or 0)
        for aid, t, t_en, ec, ep_r in group[1:]:
            cur_max = max(ec or 0, ep_r or 0)
            if top_max >= 10 and cur_max <= 2:
                if t_en and top_en and t_en.strip().lower() == top_en.strip().lower():
                    c.execute("UPDATE anime SET anilist_id = NULL, title_english = '' WHERE anime_id = ?", (aid,))
                else:
                    c.execute("UPDATE anime SET anilist_id = NULL WHERE anime_id = ?", (aid,))
                cleared_cols += 1
    conn_h.commit()
    print(f"  Cleared {cleared_cols} false spin-off/recap AniList ID collisions in hianime.db")

    # D) Backfill ZokoAnime streams for any anime in hianime.db that lacks ZokoAnime streams (starting with One Piece + others)
    missing_zoko_eps = c.execute("""
        SELECT a.anime_id, a.title, a.anilist_id, e.episode_id, e.episode_number, e.title
        FROM anime a
        JOIN episodes e ON e.anime_id = a.anime_id
        WHERE a.anime_id NOT IN (SELECT DISTINCT anime_id FROM streams WHERE server_name = 'ZokoAnime')
          AND (lower(a.title) LIKE '%one piece%' OR e.episode_number <= 3)
        ORDER BY CASE WHEN lower(a.title) LIKE '%one piece%' THEN 0 ELSE 1 END, a.anilist_popularity DESC NULLS LAST
        LIMIT 800
    """).fetchall()
    print(f"  Probing HiAnime/ZokoAnime streams for {len(missing_zoko_eps)} episodes lacking ZokoAnime...")

    def _probe_hi_ep(rec):
        aid, title, al_id, ep_id, ep_no, ep_title = rec
        data = fetch_json(f"https://hianime.at/api/theme/episode/servers?episodeId={ep_id}", referer="https://hianime.at/", timeout=7)
        found = []
        if data and data.get("status"):
            soup = BeautifulSoup(data.get("html") or "", "html.parser")
            seen_t = set()
            for s in soup.select(".server-item"):
                sh = s.get("data-hash") or ""
                try:
                    u = base64.b64decode(sh).decode("utf-8", errors="ignore").strip()
                except Exception:
                    u = ""
                if not u or "zokoanime" not in u.lower():
                    continue
                stype = (s.get("data-type") or "sub").strip().lower()
                if stype in seen_t:
                    continue
                seen_t.add(stype)
                found.append((aid, title, ep_id, ep_no, ep_title or f"Episode {ep_no}", stype, "ZokoAnime", "zokoanime.video", u))
        return found

    ins_z = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        for res in pool.map(_probe_hi_ep, missing_zoko_eps):
            for row_s in res:
                c.execute("""
                    INSERT OR IGNORE INTO streams
                    (anime_id, anime_title, episode_id, episode_number, episode_title, type, server_name, provider, embed_url)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, row_s)
                ins_z += c.rowcount
    conn_h.commit()
    print(f"  Backfilled {ins_z} new ZokoAnime stream rows in hianime.db")


def repair_and_scrape_munib(conn_h: sqlite3.Connection, conn_m: sqlite3.Connection):
    print("--- 2. Realigning munib_server.db IDs & scraping missed One Piece titles ---")
    ch = conn_h.cursor()
    cm = conn_m.cursor()

    # Build hianime episode_id map for One Piece (id=1) & canonical anilist -> anime_id map
    h_by_al = {
        int(al): int(aid)
        for aid, al in ch.execute("SELECT anime_id, anilist_id FROM anime WHERE anilist_id IS NOT NULL AND anilist_id > 0").fetchall()
    }
    h_ep_ids = {
        (int(aid), int(ep_no)): (str(ep_id), str(ep_t or f"Episode {ep_no}"))
        for aid, ep_id, ep_no, ep_t in ch.execute("SELECT anime_id, episode_id, episode_number, title FROM episodes").fetchall()
        if ep_no is not None
    }

    # Fix ONE PIECE (al=21) sitting on wrong anime_id=4164 -> move to anime_id=1 cleanly!
    row_21 = cm.execute("SELECT anime_id FROM anime WHERE anilist_id = 21").fetchone()
    if row_21 and int(row_21[0]) != 1:
        old_aid = int(row_21[0])
        print(f"  Moving ONE PIECE (AniList 21) in munib_server.db from anime_id={old_aid} -> 1")
        cm.execute("DELETE FROM anime WHERE anime_id = 1")
        cm.execute("DELETE FROM episodes WHERE anime_id = 1")
        cm.execute("DELETE FROM streams WHERE anime_id = 1")
        cm.execute("UPDATE anime SET anime_id = 1 WHERE anilist_id = 21")
        cm.execute("UPDATE episodes SET anime_id = 1 WHERE anilist_id = 21 OR anime_id = ?", (old_aid,))
        cm.execute("UPDATE streams SET anime_id = 1 WHERE anilist_id = 21 OR anime_id = ?", (old_aid,))
        # Sync episode_ids on anime_id=1 to match hianime.db episode_ids cleanly
        for ep_no, (real_ep_id, real_t) in [(k[1], v) for k, v in h_ep_ids.items() if k[0] == 1]:
            cm.execute("UPDATE episodes SET episode_id = ?, title = COALESCE(NULLIF(?, ''), title) WHERE anime_id = 1 AND episode_number = ?", (real_ep_id, real_t, ep_no))
            cm.execute("UPDATE streams SET episode_id = ?, episode_title = ? WHERE anime_id = 1 AND episode_number = ?", (real_ep_id, real_t, ep_no))
        conn_m.commit()

    # Realign any other Munib entries whose anime_id disagrees with hianime.db canonical anime_id (where target ID is free)
    existing_m_aids = {int(r[0]) for r in cm.execute("SELECT anime_id FROM anime").fetchall()}
    realigned = 0
    for aid_m, al_m in cm.execute("SELECT anime_id, anilist_id FROM anime WHERE anilist_id > 0").fetchall():
        aid_m, al_m = int(aid_m), int(al_m)
        target_aid = h_by_al.get(al_m)
        if target_aid and target_aid != aid_m and target_aid not in existing_m_aids:
            cm.execute("UPDATE anime SET anime_id = ? WHERE anime_id = ?", (target_aid, aid_m))
            cm.execute("UPDATE episodes SET anime_id = ? WHERE anime_id = ?", (target_aid, aid_m))
            cm.execute("UPDATE streams SET anime_id = ? WHERE anime_id = ?", (target_aid, aid_m))
            existing_m_aids.discard(aid_m)
            existing_m_aids.add(target_aid)
            realigned += 1
    conn_m.commit()
    print(f"  Realigned {realigned} additional Munib Server anime_ids to match hianime.db")

    # Scrape ALL One Piece titles found on reanime.to into munib_server.db!
    search_data = fetch_json("https://reanime.to/api/v1/search?q=One%20Piece&limit=35", referer="https://reanime.to/home")
    re_items = (search_data or {}).get("results") or []
    existing_m_al = {int(r[0]): int(r[1]) for r in cm.execute("SELECT anilist_id, anime_id FROM anime WHERE anilist_id > 0").fetchall()}

    def _flix(al_id, ep_no):
        d = fetch_json(f"https://reanime.to/api/flix/{al_id}/{ep_no}", referer="https://reanime.to/home", timeout=8)
        if not d or not d.get("success"):
            return []
        sub_l, dub_l = None, None
        for s in d.get("servers") or []:
            dt = (s.get("dataType") or "").lower()
            dl = (s.get("dataLink") or "").strip()
            if not dl:
                continue
            if dt == "sub" and not sub_l:
                sub_l = dl
            elif dt in ("dub", "s-dub") and not dub_l:
                sep = "&" if "?" in dl else "?"
                dub_l = f"{dl}{sep}a=1" if "a=1" not in dl else dl
        out = []
        if sub_l: out.append(("sub", sub_l))
        if dub_l: out.append(("dub", dub_l))
        return out

    added_m_titles = 0
    added_m_streams = 0
    for item in re_items:
        al_id = int(item.get("anilist_id") or 0)
        if al_id <= 0 or al_id == 10033:  # skip Toriko cameo
            continue
        t_obj = item.get("title") or {}
        t_en = t_obj.get("english") or ""
        t_rom = t_obj.get("romaji") or ""
        t_nat = t_obj.get("native") or ""
        disp = t_en or t_rom or t_nat
        if "one piece" not in (disp + " " + t_rom).lower():
            continue
        sub_cnt, dub_cnt = int(item.get("subbed") or 0), int(item.get("dubbed") or 0)
        max_ep = max(sub_cnt, dub_cnt)
        if max_ep <= 0:
            continue

        # Target anime_id: matching hianime.db ID if available, else existing munib ID, else 1_000_000 + al_id
        target_aid = existing_m_al.get(al_id) or h_by_al.get(al_id) or (1000000 + al_id)
        have_map = {}
        for ep_n, st in cm.execute("SELECT episode_number, type FROM streams WHERE anime_id = ?", (target_aid,)).fetchall():
            have_map.setdefault(int(ep_n), set()).add(st)

        to_fetch = [ep for ep in range(1, max_ep + 1) if (ep <= sub_cnt and "sub" not in have_map.get(ep, set())) or (ep <= dub_cnt and "dub" not in have_map.get(ep, set()))]
        if not to_fetch:
            continue

        print(f"  Scraping missing Munib episodes for {disp} (al={al_id} -> aid={target_aid}): {len(to_fetch)} eps")
        ep_res = {}
        with ThreadPoolExecutor(max_workers=min(10, len(to_fetch))) as pool:
            futs = {pool.submit(_flix, al_id, ep): ep for ep in to_fetch}
            for fut in as_completed(futs):
                ep = futs[fut]
                strms = fut.result()
                if strms:
                    ep_res[ep] = strms
        if not ep_res:
            continue

        cover = item.get("cover_image") or {}
        p_lg = cover.get("extra_large") or cover.get("large") or ""
        p_sm = cover.get("large") or cover.get("medium") or p_lg
        genres = ", ".join(item.get("genres") or []) if isinstance(item.get("genres"), list) else ""
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        cm.execute("""
            INSERT OR REPLACE INTO anime (
                anime_id, anilist_id, reanime_slug, title, title_english, title_romaji,
                japanese_name, type, status, episode_count, subbed_count, dubbed_count,
                duration, season, season_year, anilist_score, anilist_popularity,
                genres, poster_url, poster_large, scraped_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            target_aid, al_id, str(item.get("anime_id") or ""), disp, t_en, t_rom, t_nat,
            item.get("format") or "MOVIE", "Currently Airing" if (item.get("status") or "").lower() == "releasing" else "Finished Airing",
            max(max_ep, max(ep_res)), sub_cnt, dub_cnt, item.get("duration") or "",
            item.get("season") or "", item.get("season_year"), item.get("average_score"),
            item.get("popularity") or 0, genres, p_sm, p_lg, ts
        ))
        if al_id not in existing_m_al:
            added_m_titles += 1
            existing_m_al[al_id] = target_aid

        for ep, strms in sorted(ep_res.items()):
            mapped = h_ep_ids.get((target_aid, ep))
            ep_id = mapped[0] if mapped else f"munib_{al_id}_{ep}"
            ep_t = mapped[1] if mapped else f"Episode {ep}"
            cm.execute("""
                INSERT OR REPLACE INTO episodes (anime_id, anilist_id, episode_id, episode_number, title, watch_url)
                VALUES (?,?,?,?,?,?)
            """, (target_aid, al_id, ep_id, ep, ep_t, strms[0][1]))
            for stype, slink in strms:
                if stype not in have_map.get(ep, set()):
                    added_m_streams += 1
                cm.execute("""
                    INSERT OR REPLACE INTO streams (anime_id, anilist_id, anime_title, episode_id, episode_number, episode_title, type, server_name, provider, embed_url)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (target_aid, al_id, disp, ep_id, ep, ep_t, stype, "Munib Server", "reanime.to", slink))
        conn_m.commit()

    print(f"  Added {added_m_titles} One Piece titles & {added_m_streams} streams to Munib Server")


def fix_one_piece_trailers(conn_h: sqlite3.Connection, conn_m: sqlite3.Connection):
    print("--- 3. Replacing wrong Netflix live-action trailer (Ades3pQbeh8) with official anime PVs ---")
    for conn in (conn_h, conn_m):
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS anime_trailers (
                anime_id INTEGER PRIMARY KEY,
                trailer_id TEXT,
                site TEXT DEFAULT 'youtube',
                source TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Purge Netflix live-action trailer Ades3pQbeh8 anywhere in anime_trailers
        c.execute("DELETE FROM anime_trailers WHERE trailer_id = 'Ades3pQbeh8'")
        # Cache official Crunchyroll One Piece anime trailer on anime_id=1
        c.execute("""
            INSERT OR REPLACE INTO anime_trailers (anime_id, trailer_id, site, source)
            VALUES (1, '3NUizzImhig', 'youtube', 'official_anime')
        """)
        # Clear 4164 stale trailer so Movie 1 resolves clean anime movie trailer dynamically
        c.execute("DELETE FROM anime_trailers WHERE anime_id = 4164")
        conn.commit()


def main():
    conn_h = sqlite3.connect(HIANIME_DB, timeout=30)
    conn_h.execute("PRAGMA journal_mode = WAL;")
    repair_hianime_db(conn_h)

    conn_m = sqlite3.connect(MUNIB_DB, timeout=30)
    conn_m.execute("PRAGMA journal_mode = WAL;")
    repair_and_scrape_munib(conn_h, conn_m)

    fix_one_piece_trailers(conn_h, conn_m)

    print("--- 4. Checkpointing WAL -> DELETE and syncing .db.gz copies ---")
    for conn in (conn_h, conn_m):
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.execute("PRAGMA journal_mode = DELETE;")
        conn.close()

    sync_gz(HIANIME_DB, "hianime.db")
    sync_gz(MUNIB_DB, "munib_server.db")
    print("Repair & One Piece sync complete!")


if __name__ == "__main__":
    main()
