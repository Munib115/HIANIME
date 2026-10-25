"""
HiAnime High-Performance Smart Sync
===================================
1. Scans listing feed HTML directly to extract current episode badges (.tick-sub, .tick-dub).
2. Zero API overhead: only makes detail/stream API calls for anime that are truly new or have newly dropped episodes.
3. Polite rate-limiting between API calls to avoid Cloudflare 429s.
4. Truncates WAL and syncs DB copies to all web and API paths.
"""
import sys, io, os, time, re, json, shutil, sqlite3
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import requests, base64
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(BASE_DIR, "hianime.db")
LOG_FILE   = os.path.join(BASE_DIR, "smart_sync.log")
BASE_URL   = "https://hianime.at"
API_BASE   = "https://hianime.at/api/theme/"
ANILIST_API= "https://graphql.anilist.co"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://hianime.at/",
}

session = requests.Session()
session.headers.update(HEADERS)
adapter = requests.adapters.HTTPAdapter(
    pool_connections=30,
    pool_maxsize=30,
    max_retries=requests.adapters.Retry(total=2, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
)
session.mount("https://", adapter)
session.mount("http://", adapter)

def log(msg):
    line = "[" + time.strftime("%H:%M:%S") + "] " + str(msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def fetch(url, retries=3, timeout=10, as_json=False, delay=0.12):
    if delay > 0:
        time.sleep(delay)
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json() if as_json else r.text
            elif r.status_code == 429:
                wait = 5 * (attempt + 1)
                log(f"  [429] Rate limit hit on {url[:60]} - sleeping {wait}s")
                time.sleep(wait)
            elif r.status_code == 404:
                return None
            else:
                time.sleep(0.5)
        except Exception:
            time.sleep(0.5)
    return None

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

def enrich_anilist(title):
    clean = re.sub(
        r"\(TV\)|\(Dub\)|\(Sub\)|[\(\[]uncensored[\)\]]|[\(\[]uncut[\)\]]|Season \d+|2nd Season|3rd Season|4th Season|Part \d+",
        "",
        title,
        flags=re.IGNORECASE
    ).strip()
    clean = re.sub(r"\s+", " ", clean)
    try:
        time.sleep(0.12)
        r = requests.post(
            ANILIST_API,
            json={"query": ANILIST_QUERY, "variables": {"search": clean}},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=5
        )
        if r.status_code == 200:
            m = r.json().get("data", {}).get("Media")
            if m:
                t = m.get("title", {})
                cv = m.get("coverImage", {})
                return {
                    "anilist_id": m.get("id"),
                    "title_romaji": t.get("romaji"),
                    "title_english": t.get("english"),
                    "title_native": t.get("native"),
                    "anilist_score": m.get("averageScore"),
                    "anilist_popularity": m.get("popularity"),
                    "poster_large": cv.get("extraLarge") or cv.get("large"),
                    "banner_image": m.get("bannerImage"),
                    "anilist_status": m.get("status"),
                    "episodes": m.get("episodes"),
                    "season": m.get("season"),
                    "season_year": m.get("seasonYear"),
                    "al_genres": ", ".join(m.get("genres", [])) if m.get("genres") else None,
                    "al_description": m.get("description"),
                }
    except Exception:
        pass
    return None

def scrape_detail_page(url):
    html = fetch(url, delay=0.5)
    if not html: return None
    soup = BeautifulSoup(html, "lxml")

    m = re.search(r"-(\d+)$", url)
    if not m: return None
    aid = int(m.group(1))

    title_el = soup.select_one(".anisc-detail .film-name")
    title = title_el.get_text(strip=True) if title_el else ""
    if not title: return None

    meta = {
        "anime_id": aid,
        "hianime_url": url,
        "title": title,
        "japanese_name": "",
        "synonyms": "",
        "synopsis": "",
        "poster_url": "",
        "status": "Currently Airing",
        "type": "TV",
        "aired": "",
        "premiered": "",
        "duration": "",
        "mal_score": "",
        "studios": "",
        "producers": "",
        "genres": [],
        "seasons": [],
    }

    desc_el = soup.select_one(".film-description .text")
    if desc_el: meta["synopsis"] = desc_el.get_text(strip=True)

    poster_el = soup.select_one(".anisc-poster .film-poster-img")
    if poster_el: meta["poster_url"] = poster_el.get("src", "")

    for item in soup.select(".anisc-info .item"):
        head = item.select_one(".item-head")
        if not head: continue
        htext = head.get_text(strip=True).rstrip(":")
        val_el = item.select_one(".name, .text")
        val = val_el.get_text(strip=True) if val_el else ""
        if htext == "Japanese": meta["japanese_name"] = val
        elif htext == "Synonyms": meta["synonyms"] = val
        elif htext == "Aired": meta["aired"] = val
        elif htext == "Premiered": meta["premiered"] = val
        elif htext == "Duration": meta["duration"] = val
        elif htext == "Status": meta["status"] = val or "Currently Airing"
        elif htext == "MAL Score": meta["mal_score"] = val
        elif htext == "Studios": meta["studios"] = val
        elif htext == "Producers": meta["producers"] = val
        elif htext == "Genres":
            meta["genres"] = [a.get_text(strip=True) for a in item.select("a")]

    type_badge = soup.select_one(".film-stats .item:nth-child(1)")
    if type_badge:
        tb = type_badge.get_text(strip=True).upper()
        if tb in ["TV", "MOVIE", "OVA", "ONA", "SPECIAL"]:
            meta["type"] = tb

    block = soup.select_one(".block_area-seasons")
    if block:
        for item in block.select(".os-item"):
            s_title = item.select_one(".title")
            s_name = s_title.get_text(strip=True) if s_title else item.get_text(strip=True)
            s_href = item.get("href", "")
            s_id_m = re.search(r"-(\d+)$", s_href)
            s_id = int(s_id_m.group(1)) if s_id_m else None
            poster_m = re.search(r"url\((.*?)\)", str(item))
            s_poster = poster_m.group(1).strip("'\"") if poster_m else ""
            meta["seasons"].append({
                "season_name": s_name,
                "season_anime_id": s_id,
                "season_url": s_href,
                "season_poster": s_poster,
                "is_current": 1 if "active" in item.get("class", []) else 0
            })

    return meta

def get_episodes(anime_id):
    data = fetch(API_BASE + "episode/list/" + str(anime_id), as_json=True, delay=0.5)
    if not data or "html" not in data: return []
    soup = BeautifulSoup(data["html"], "lxml")
    eps = []
    for a in soup.select("a.ep-item"):
        href = a.get("href", "")
        ep_num = a.get("data-number", "")
        ep_id = a.get("data-id", "")
        tel = a.select_one(".ep-name")
        title = tel.get_text(strip=True) if tel else a.get("title", "")
        jname = tel.get("data-jname", "") if tel else ""
        m = re.search(r"[?&]ep=(\d+)", href)
        if m: ep_id = m.group(1)
        eps.append({
            "episode_id": ep_id,
            "episode_number": int(ep_num) if str(ep_num).isdigit() else 0,
            "title": title,
            "japanese_title": jname,
            "watch_url": href if href.startswith("http") else BASE_URL + href,
        })
    return eps

def fetch_single_episode_servers(ep):
    ep_id = ep.get("episode_id")
    ep_num = ep.get("episode_number", 0)
    ep_title = ep.get("title", "")
    if not ep_id: return []

    data = fetch(API_BASE + "episode/servers?episodeId=" + str(ep_id), as_json=True, delay=0.1)
    if not data or not data.get("status"): return []

    soup = BeautifulSoup(data.get("html", ""), "lxml")
    out = []
    for s in soup.select(".server-item"):
        sname = (s.get("data-server-name", "") or "ZokoAnime").strip()
        if "HD-1" in sname or "HD 1" in sname: continue
        shash = s.get("data-hash", "")
        try:
            url = base64.b64decode(shash).decode("utf-8", errors="ignore").strip()
        except Exception:
            url = ""
        if not url: continue
        provider = re.search(r"https?://([^/]+)", url)
        prov_name = provider.group(1) if provider else ""
        if "megaplay" in prov_name.lower(): continue
        out.append({
            "episode_id": ep_id,
            "episode_number": ep_num,
            "episode_title": ep_title,
            "server_name": sname or "ZokoAnime",
            "type": (s.get("data-type", "") or "sub").strip().lower(),
            "embed_url": url,
            "provider": prov_name,
        })
    if not out:
        for s in soup.select(".server-item"):
            sname = (s.get("data-server-name", "") or "Server").strip()
            shash = s.get("data-hash", "")
            try:
                url = base64.b64decode(shash).decode("utf-8", errors="ignore").strip()
            except Exception:
                url = ""
            if not url or not url.startswith("http"): continue
            provider = re.search(r"https?://([^/]+)", url)
            prov_name = provider.group(1) if provider else ""
            out.append({
                "episode_id": ep_id,
                "episode_number": ep_num,
                "episode_title": ep_title,
                "server_name": sname,
                "type": (s.get("data-type", "") or "sub").strip().lower(),
                "embed_url": url,
                "provider": prov_name,
            })
    return out

def get_all_episode_streams(episodes):
    if not episodes: return []
    if len(episodes) > 50:
        target_eps = episodes[:25] + episodes[-25:]
    else:
        target_eps = episodes

    all_streams = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(fetch_single_episode_servers, ep): ep for ep in target_eps}
        for f in as_completed(futs):
            try:
                res = f.result()
                if res:
                    all_streams.extend(res)
            except Exception:
                pass
    return all_streams

def sync_db_copies():
    targets = [
        os.path.join(BASE_DIR, "api", "hianime.db"),
        os.path.join(BASE_DIR, "anime_app", "web", "api", "hianime.db"),
        os.path.join(BASE_DIR, "anime_app", "web", "hianime.db"),
    ]
    for t in targets:
        try:
            os.makedirs(os.path.dirname(t), exist_ok=True)
            shutil.copy2(DB_PATH, t)
            log(f"Synced DB to -> {os.path.relpath(t, BASE_DIR)}")
        except Exception as e:
            log(f"Error copying DB to {t}: {e}")

def run():
    log("=" * 65)
    log("Starting HiAnime Smart Scraper Sync")
    log("=" * 65)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    c = conn.cursor()

    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_episodes_anime_ep ON episodes(anime_id, episode_id);")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_streams_ep_server_type ON streams(episode_id, server_name, type);")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_seasons_parent_season ON anime_seasons(parent_anime_id, season_anime_id);")

    c.execute("SELECT COUNT(*) FROM anime")
    init_anime_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM episodes")
    init_episodes_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM streams")
    init_streams_count = c.fetchone()[0]
    log(f"Starting DB stats: {init_anime_count:,} anime, {init_episodes_count:,} episodes, {init_streams_count:,} streams")

    c.execute("SELECT anime_id, episode_count FROM anime")
    db_anime = dict(c.fetchall())

    c.execute("SELECT DISTINCT episode_id FROM episodes WHERE episode_id IS NOT NULL")
    db_ep_ids = set(str(row[0]) for row in c.fetchall() if row[0])

    targets = {}
    target_lock = Lock()

    def scan_page(url):
        time.sleep(0.15)
        try:
            r = session.get(url, timeout=10)
            if r.status_code != 200: return
            soup = BeautifulSoup(r.text, "html.parser")
            for item in soup.select(".film_list-wrap .flw-item"):
                a = item.select_one(".film-name a")
                if not a: continue
                m = re.search(r"-(\d+)$", a.get("href", ""))
                if not m: continue
                aid = int(m.group(1))
                full_url = a.get("href", "")
                if not full_url.startswith("http"):
                    full_url = BASE_URL + full_url

                sub = item.select_one(".tick-sub")
                dub = item.select_one(".tick-dub")
                sub_cnt = int(sub.get_text(strip=True)) if sub and sub.get_text(strip=True).isdigit() else 0
                dub_cnt = int(dub.get_text(strip=True)) if dub and dub.get_text(strip=True).isdigit() else 0
                card_cnt = max(sub_cnt, dub_cnt)

                with target_lock:
                    if aid not in targets or card_cnt > targets[aid]["card_cnt"]:
                        targets[aid] = {
                            "anime_id": aid,
                            "title": a.get_text(strip=True),
                            "url": full_url,
                            "card_cnt": card_cnt,
                        }
        except Exception:
            pass

    feed_urls = []
    # 1. Recently updated & new anime releases
    for p in range(1, 16): feed_urls.append(f"{BASE_URL}/recently-updated?page={p}")
    for p in range(1, 26): feed_urls.append(f"{BASE_URL}/new-anime?page={p}")
    # 2. Currently Airing anime
    for p in range(1, 11): feed_urls.append(f"{BASE_URL}/top-airing?page={p}")
    for p in range(1, 16): feed_urls.append(f"{BASE_URL}/filter?status=releasing&sort=updated_date&page={p}")
    for p in range(1, 6):  feed_urls.append(f"{BASE_URL}/filter?status=releasing&sort=trending&page={p}")
    # 3. Uncensored searches
    for p in range(1, 4):  feed_urls.append(f"{BASE_URL}/search?keyword=uncensored&page={p}")
    for p in range(1, 3):  feed_urls.append(f"{BASE_URL}/search?keyword=uncensor&page={p}")
    # 4. Hentai & 18+ category
    for p in range(1, 10): feed_urls.append(f"{BASE_URL}/genres/hentai?page={p}")

    log(f"Scanning {len(feed_urls)} catalog feed and search pages...")
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(scan_page, feed_urls))

    log(f"Discovered {len(targets)} unique titles from feed scans.")

    # 4. Explicitly ensure all uncensored anime from anime_url_list.json are loaded
    url_list_path = os.path.join(BASE_DIR, "anime_url_list.json")
    if os.path.exists(url_list_path):
        try:
            with open(url_list_path, "r", encoding="utf-8") as f:
                url_data = json.load(f)
            uncensored_injected = 0
            for item in url_data:
                u_title = item.get("title", "")
                u_url = item.get("url", "")
                if "uncensored" in u_title.lower() or "uncensored" in u_url.lower() or "uncut" in u_title.lower():
                    m = re.search(r"-(\d+)$", u_url)
                    if m:
                        aid = int(m.group(1))
                        full_u_url = u_url if u_url.startswith("http") else BASE_URL + u_url
                        cur_cnt = db_anime.get(aid, 0)
                        if aid not in targets:
                            targets[aid] = {
                                "anime_id": aid,
                                "title": u_title,
                                "url": full_u_url,
                                "card_cnt": 999 if cur_cnt == 0 else cur_cnt,
                            }
                            uncensored_injected += 1
                        elif cur_cnt == 0 and targets[aid]["card_cnt"] == 0:
                            targets[aid]["card_cnt"] = 999
            log(f"Verified uncensored titles ({uncensored_injected} added from catalog archive).")
        except Exception as e:
            log(f"Notice loading archive uncensored list: {e}")

    new_titles = [t for aid, t in targets.items() if aid not in db_anime or db_anime.get(aid, 0) == 0]
    need_eps   = [t for aid, t in targets.items() if aid in db_anime and db_anime[aid] < t["card_cnt"]]

    log(f"Identified {len(new_titles)} brand new anime to add.")
    log(f"Identified {len(need_eps)} existing anime with new episode drops.")

    new_anime_count = 0
    updated_anime_count = 0
    total_new_eps_added = 0
    total_new_streams_added = 0

    # 1. Process brand new anime
    for idx, item in enumerate(new_titles, 1):
        aid = item["anime_id"]
        title = item["title"]
        url = item["url"]
        log(f"[{idx}/{len(new_titles)}] [NEW TITLE] Scraping {title} ({aid})...")

        try:
            meta = scrape_detail_page(url)
            if not meta or not meta.get("title"):
                continue

            al = enrich_anilist(meta["title"])
            eps = get_episodes(aid)
            streams = get_all_episode_streams(eps)

            ts = int(time.time())
            genres_str = ", ".join(meta.get("genres", []))
            is_airing = "airing" in meta.get("status", "").lower() or (al and al.get("anilist_status") == "RELEASING")
            db_status = "Currently Airing" if is_airing else meta.get("status", "Finished Airing")
            seasons_json = json.dumps(meta.get("seasons", [])) if meta.get("seasons") else None

            c.execute("""
            INSERT OR REPLACE INTO anime (
                anime_id, hianime_url, title, japanese_name, synonyms, type, status,
                aired, premiered, mal_score, studios, producers, duration, genres,
                synopsis, poster_url, category, listing_page, episode_count,
                anilist_id, title_romaji, title_english, title_native,
                anilist_score, anilist_popularity, poster_large, banner_image,
                anilist_status, al_episodes, season, season_year, al_genres,
                al_description, scraped_at, enriched_at, seasons_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                aid, url, meta["title"], meta["japanese_name"], meta["synonyms"], meta["type"],
                db_status, meta["aired"], meta["premiered"], meta["mal_score"], meta["studios"],
                meta["producers"], meta["duration"], genres_str, meta["synopsis"], meta["poster_url"],
                "airing" if is_airing else "popular", 1, len(eps),
                al.get("anilist_id") if al else None, al.get("title_romaji", "") if al else "",
                al.get("title_english", "") if al else "", al.get("title_native", "") if al else "",
                al.get("anilist_score") if al else None, al.get("anilist_popularity") if al else None,
                al.get("poster_large", "") if al else "", al.get("banner_image", "") if al else "",
                al.get("anilist_status") if al else None, al.get("episodes") if al else None,
                al.get("season", "") if al else "", al.get("season_year") if al else None,
                al.get("al_genres", "") if al else "", al.get("al_description", "") if al else "",
                ts, ts if al else None, seasons_json
            ))

            if meta.get("seasons"):
                for s in meta["seasons"]:
                    c.execute("""
                    INSERT OR IGNORE INTO anime_seasons
                    (parent_anime_id, season_name, season_anime_id, season_url, season_poster, is_current)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """, (aid, s.get("season_name"), s.get("season_anime_id"), s.get("season_url"), s.get("season_poster"), s.get("is_current", 0)))

            for ep in eps:
                c.execute("""
                INSERT OR IGNORE INTO episodes (anime_id, episode_id, episode_number, title, japanese_title, watch_url)
                VALUES (?,?,?,?,?,?)
                """, (aid, ep.get("episode_id"), ep.get("episode_number", 0), ep.get("title", ""), ep.get("japanese_title", ""), ep.get("watch_url", "")))
                if ep.get("episode_id"):
                    db_ep_ids.add(str(ep.get("episode_id")))

            for s in streams:
                c.execute("""
                INSERT OR IGNORE INTO streams (anime_id, anime_title, episode_id, episode_number, episode_title, type, server_name, provider, embed_url)
                VALUES (?,?,?,?,?,?,?,?,?)
                """, (aid, meta["title"], s.get("episode_id"), s.get("episode_number", 0), s.get("episode_title", ""), s.get("type", "sub"), s.get("server_name", "ZokoAnime"), s.get("provider", ""), s.get("embed_url", "")))

            conn.commit()
            db_anime[aid] = len(eps)
            new_anime_count += 1
            total_new_eps_added += len(eps)
            total_new_streams_added += len(streams)
            log(f"  -> Added {meta['title']} with {len(eps)} episodes & {len(streams)} stream links")

        except Exception as e:
            log(f"  -> Error adding {title}: {e}")

    # 2. Process anime needing new episodes
    for idx, item in enumerate(need_eps, 1):
        aid = item["anime_id"]
        title = item["title"]
        target_cnt = item["card_cnt"]
        cur_cnt = db_anime.get(aid, 0)
        log(f"[{idx}/{len(need_eps)}] [EPISODE UPDATE] {title}: DB has {cur_cnt}, new card shows {target_cnt}")

        try:
            remote_eps = get_episodes(aid)
            if not remote_eps:
                continue

            new_eps = [ep for ep in remote_eps if ep.get("episode_id") and str(ep.get("episode_id")) not in db_ep_ids]
            if not new_eps:
                c.execute("UPDATE anime SET episode_count = ? WHERE anime_id = ?", (len(remote_eps), aid))
                conn.commit()
                db_anime[aid] = len(remote_eps)
                continue

            # Fetch streams for the newly dropped episodes
            streams = []
            with ThreadPoolExecutor(max_workers=5) as ex:
                futs = {ex.submit(fetch_single_episode_servers, ep): ep for ep in new_eps}
                for f in as_completed(futs):
                    try:
                        res = f.result()
                        if res: streams.extend(res)
                    except Exception:
                        pass

            for ep in new_eps:
                c.execute("""
                INSERT OR IGNORE INTO episodes (anime_id, episode_id, episode_number, title, japanese_title, watch_url)
                VALUES (?,?,?,?,?,?)
                """, (aid, ep.get("episode_id"), ep.get("episode_number", 0), ep.get("title", ""), ep.get("japanese_title", ""), ep.get("watch_url", "")))
                db_ep_ids.add(str(ep.get("episode_id")))

            for s in streams:
                c.execute("""
                INSERT OR IGNORE INTO streams (anime_id, anime_title, episode_id, episode_number, episode_title, type, server_name, provider, embed_url)
                VALUES (?,?,?,?,?,?,?,?,?)
                """, (aid, title, s.get("episode_id"), s.get("episode_number", 0), s.get("episode_title", ""), s.get("type", "sub"), s.get("server_name", "ZokoAnime"), s.get("provider", ""), s.get("embed_url", "")))

            c.execute("""
                UPDATE anime 
                SET episode_count = ?, 
                    scraped_at = ?,
                    status = CASE WHEN status = 'Not yet aired' THEN 'Currently Airing' ELSE status END
                WHERE anime_id = ?
            """, (len(remote_eps), int(time.time()), aid))
            conn.commit()
            db_anime[aid] = len(remote_eps)
            updated_anime_count += 1
            total_new_eps_added += len(new_eps)
            total_new_streams_added += len(streams)
            log(f"  -> Added {len(new_eps)} new episodes and {len(streams)} streams for {title} (now {len(remote_eps)} eps)")

        except Exception as e:
            log(f"  -> Error updating episodes for {title}: {e}")

    # 3. Check for any uncensored anime in DB missing AniList enrichment
    c.execute("SELECT anime_id, title FROM anime WHERE (instr(lower(title), 'uncensored') OR instr(lower(title), 'uncut')) AND anilist_id IS NULL")
    missing_enrichment = c.fetchall()
    if missing_enrichment:
        log(f"Found {len(missing_enrichment)} uncensored anime in DB without AniList data. Enriching...")
        for aid, title in missing_enrichment:
            try:
                al = enrich_anilist(title)
                if al and al.get("anilist_id"):
                    c.execute("""
                        UPDATE anime
                        SET anilist_id = ?,
                            title_romaji = ?,
                            title_english = ?,
                            title_native = ?,
                            anilist_score = ?,
                            anilist_popularity = ?,
                            poster_large = COALESCE(NULLIF(poster_large, ''), ?),
                            banner_image = COALESCE(NULLIF(banner_image, ''), ?),
                            anilist_status = ?,
                            al_episodes = ?,
                            season = ?,
                            season_year = ?,
                            al_genres = ?,
                            al_description = ?,
                            enriched_at = ?
                        WHERE anime_id = ?
                    """, (
                        al.get("anilist_id"),
                        al.get("title_romaji", ""),
                        al.get("title_english", ""),
                        al.get("title_native", ""),
                        al.get("anilist_score"),
                        al.get("anilist_popularity"),
                        al.get("poster_large", ""),
                        al.get("banner_image", ""),
                        al.get("anilist_status"),
                        al.get("episodes"),
                        al.get("season", ""),
                        al.get("season_year"),
                        al.get("al_genres", ""),
                        al.get("al_description", ""),
                        int(time.time()),
                        aid
                    ))
                    conn.commit()
                    log(f"  -> Successfully enriched {title} (AniList #{al.get('anilist_id')})")
            except Exception as e:
                log(f"  -> Error enriching uncensored {title}: {e}")

    # Checkpoint and sync
    log("Check-pointing SQLite WAL to persist all changes...")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE);")

    c.execute("SELECT COUNT(*) FROM anime")
    final_anime_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM episodes")
    final_episodes_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM streams")
    final_streams_count = c.fetchone()[0]

    conn.close()

    log("=" * 65)
    log("SMART SYNC COMPLETE SUMMARY:")
    log(f"  • New Anime Titles Added:    +{new_anime_count}")
    log(f"  • Anime Updated with Drops:   {updated_anime_count}")
    log(f"  • Total New Episodes Added:  +{total_new_eps_added}")
    log(f"  • Total New Streams Added:   +{total_new_streams_added}")
    log(f"  • Total Anime in DB:          {final_anime_count:,} (was {init_anime_count:,})")
    log(f"  • Total Episodes in DB:       {final_episodes_count:,} (was {init_episodes_count:,})")
    log(f"  • Total Streams in DB:        {final_streams_count:,} (was {init_streams_count:,})")
    log("=" * 65)

    log("Synchronizing database copies across web and API directories...")
    sync_db_copies()
    log("All database replicas synchronized successfully!")

def run_daemon(poll_interval_sec=300):
    log("=" * 65)
    log(f"HiAnime Continuous Smart Scraper Daemon (polling every {poll_interval_sec}s)")
    log("=" * 65)
    try:
        while True:
            try:
                run()
            except Exception as e:
                log(f"Daemon error during sync cycle: {e}")
            log(f"Sleeping for {poll_interval_sec}s before next check...")
            time.sleep(poll_interval_sec)
    except KeyboardInterrupt:
        log("Daemon terminated by user.")

if __name__ == "__main__":
    if "--daemon" in sys.argv:
        idx = sys.argv.index("--daemon")
        interval = int(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 and sys.argv[idx + 1].isdigit() else 300
        run_daemon(interval)
    else:
        run()
