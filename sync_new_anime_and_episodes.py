"""
KuroAnime Airing & New Episodes Scraper
=======================================
Scrapes all newly released episodes and new anime titles from HiAnime:
- /recently-updated (pages 1-15)
- /new-anime (pages 1-15)
- /top-airing (pages 1-8)
- /filter?status=releasing&sort=updated_date (pages 1-8)

Updates SQLite database, checkpoints WAL, and syncs across all API directories.
"""
import sys, io
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import requests, base64, json, re, time, sqlite3, os, shutil
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DB_PATH     = os.path.join(BASE_DIR, "hianime.db")
LOG_FILE    = os.path.join(BASE_DIR, "scraper_sync.log")
BASE_URL    = "https://hianime.at"
API_BASE    = "https://hianime.at/api/theme/"
ANILIST_API = "https://graphql.anilist.co"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://hianime.at/",
}

session = requests.Session()
session.headers.update(HEADERS)
adapter = HTTPAdapter(
    pool_connections=35,
    pool_maxsize=35,
    max_retries=Retry(total=3, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
)
session.mount("https://", adapter)
session.mount("http://", adapter)

rate_lock = Lock()
last_request_time = 0.0

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

def fetch(url, retries=3, timeout=9, as_json=False):
    global last_request_time
    for attempt in range(retries):
        try:
            with rate_lock:
                now = time.time()
                elapsed = now - last_request_time
                if elapsed < 0.10:
                    time.sleep(0.10 - elapsed)
                last_request_time = time.time()

            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json() if as_json else r.text
            elif r.status_code == 429:
                wait = 4 * (attempt + 1)
                log(f"  [429] Rate limited on {url[:60]} - sleep {wait}s")
                time.sleep(wait)
            elif r.status_code == 404:
                return None
            else:
                time.sleep(0.4)
        except Exception:
            time.sleep(0.4)
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
        r"\(TV\)|\(Dub\)|\(Sub\)|\(Uncensored\)|\(UNCENSORED\)|\[Uncensored\]|\[UNCENSORED\]|Season \d+|2nd Season|3rd Season|4th Season|Part \d+",
        "",
        title,
        flags=re.IGNORECASE
    ).strip()
    clean = re.sub(r"\s+", " ", clean)
    try:
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
    html = fetch(url)
    if not html: return None
    soup = BeautifulSoup(html, "lxml")

    m = re.search(r"-(\d+)$", url)
    if not m: return None
    aid = int(m.group(1))

    title_el = soup.select_one(".anisc-detail .film-name, h2.film-name, .film-name")
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

    desc_el = soup.select_one(".film-description .text, .film-description, .synopsis")
    if desc_el: meta["synopsis"] = desc_el.get_text(strip=True)

    poster_el = soup.select_one(".anisc-poster .film-poster-img, .film-poster img")
    if poster_el: meta["poster_url"] = poster_el.get("data-src") or poster_el.get("src", "")

    for item in soup.select(".anisc-info .item, .item"):
        head = item.select_one(".item-head")
        if head:
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

    # Seasons
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

    type_badge = soup.select_one(".film-stats .item:nth-child(1)")
    if type_badge:
        tb = type_badge.get_text(strip=True).upper()
        if tb in ["TV", "MOVIE", "OVA", "ONA", "SPECIAL"]:
            meta["type"] = tb

    return meta

def get_episodes(anime_id):
    data = fetch(API_BASE + "episode/list/" + str(anime_id), as_json=True)
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

    data = fetch(API_BASE + "episode/servers?episodeId=" + str(ep_id), as_json=True)
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
    return out

def get_all_episode_streams(episodes):
    if not episodes: return []
    all_streams = []
    workers = min(6, len(episodes))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futs = {executor.submit(fetch_single_episode_servers, ep): ep for ep in episodes}
        for f in as_completed(futs):
            try:
                res = f.result()
                if res: all_streams.extend(res)
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
        except Exception:
            pass

def collect_feed_targets():
    log("Scanning HiAnime feeds for latest releases and updated episodes...")
    targets = {}

    def scan_page(url):
        html = fetch(url)
        if not html: return
        soup = BeautifulSoup(html, "html.parser")
        for item in soup.select(".film_list-wrap .flw-item"):
            a = item.select_one(".film-name a")
            if a and a.get("href"):
                href = a.get("href")
                m = re.search(r"-(\d+)$", href)
                if m:
                    aid = int(m.group(1))
                    full_url = href if href.startswith("http") else BASE_URL + href
                    if aid not in targets:
                        targets[aid] = {"anime_id": aid, "title": a.text.strip(), "url": full_url}

    # 1. Recently updated (Pages 1 to 15)
    for p in range(1, 16):
        scan_page(f"{BASE_URL}/recently-updated?page={p}")

    # 2. Top Airing (Pages 1 to 8)
    for p in range(1, 9):
        scan_page(f"{BASE_URL}/top-airing?page={p}")

    # 3. New Anime (Pages 1 to 15)
    for p in range(1, 16):
        scan_page(f"{BASE_URL}/new-anime?page={p}")

    # 4. Filter Airing (Pages 1 to 8)
    for p in range(1, 9):
        scan_page(f"{BASE_URL}/filter?status=releasing&sort=updated_date&page={p}")

    log(f"Discovered {len(targets)} unique anime titles across HiAnime feeds.")
    return targets

def run_sync():
    log("=" * 65)
    log("KuroAnime Scraper Sync: Discovering New Episodes & Anime")
    log("=" * 65)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")

    c = conn.cursor()
    c.execute("SELECT anime_id, episode_count FROM anime")
    db_anime = dict(c.fetchall())

    c.execute("SELECT DISTINCT episode_id FROM episodes")
    db_ep_ids = set(row[0] for row in c.fetchall() if row[0])

    targets = collect_feed_targets()

    new_episodes_added = 0
    updated_anime_list = []
    new_anime_added = 0
    new_anime_list = []

    total_targets = len(targets)
    log(f"Checking {total_targets} feed anime against database...")

    for idx, (aid, item) in enumerate(targets.items(), 1):
        title = item["title"]
        url = item["url"]

        if aid in db_anime and db_anime[aid] > 0:
            # Existing anime -> check if new episodes dropped
            remote_eps = get_episodes(aid)
            if not remote_eps:
                continue

            new_eps = [ep for ep in remote_eps if ep.get("episode_id") and ep.get("episode_id") not in db_ep_ids]
            if new_eps:
                log(f"[{idx}/{total_targets}] [NEW EPISODES] {title}: +{len(new_eps)} new eps (Total: {len(remote_eps)})")
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
                    db_ep_ids.add(ep.get("episode_id"))

                for s in streams:
                    c.execute("""
                    INSERT OR IGNORE INTO streams (anime_id, anime_title, episode_id, episode_number, episode_title, type, server_name, provider, embed_url)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """, (aid, title, s.get("episode_id"), s.get("episode_number", 0), s.get("episode_title", ""), s.get("type", "sub"), s.get("server_name", "ZokoAnime"), s.get("provider", ""), s.get("embed_url", "")))

                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                c.execute("UPDATE anime SET episode_count = ?, status = 'Currently Airing', scraped_at = ? WHERE anime_id = ?", (len(remote_eps), ts, aid))
                conn.commit()
                db_anime[aid] = len(remote_eps)
                new_episodes_added += len(new_eps)
                updated_anime_list.append({"anime_id": aid, "title": title, "new_eps": len(new_eps), "total_eps": len(remote_eps)})

        elif aid not in db_anime or db_anime.get(aid, 0) == 0:
            # Brand new anime title -> scrape full metadata, episodes, and streams
            try:
                meta = scrape_detail_page(url)
                if not meta or not meta.get("title"):
                    continue

                al = enrich_anilist(meta["title"])
                eps = get_episodes(aid)
                streams = get_all_episode_streams(eps)

                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                genres_str = ", ".join(meta.get("genres", []))
                is_airing = "airing" in meta.get("status", "").lower() or (al and al.get("anilist_status") == "RELEASING")
                db_status = "Currently Airing" if is_airing else meta.get("status", "Finished Airing")
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
                    seasons_json, ts, ts if al else None,
                ))

                for ep in eps:
                    c.execute("""
                    INSERT OR IGNORE INTO episodes (anime_id, episode_id, episode_number, title, japanese_title, watch_url)
                    VALUES (?,?,?,?,?,?)
                    """, (aid, ep.get("episode_id"), ep.get("episode_number", 0), ep.get("title", ""), ep.get("japanese_title", ""), ep.get("watch_url", "")))
                    if ep.get("episode_id"): db_ep_ids.add(ep.get("episode_id"))

                for s in streams:
                    c.execute("""
                    INSERT OR IGNORE INTO streams (anime_id, anime_title, episode_id, episode_number, episode_title, type, server_name, provider, embed_url)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """, (aid, meta["title"], s.get("episode_id"), s.get("episode_number", 0), s.get("episode_title", ""), s.get("type", "sub"), s.get("server_name", "ZokoAnime"), s.get("provider", ""), s.get("embed_url", "")))

                conn.commit()
                db_anime[aid] = len(eps)
                new_anime_added += 1
                new_anime_list.append({"anime_id": aid, "title": meta["title"], "episodes": len(eps), "streams": len(streams)})
                log(f"[{idx}/{total_targets}] [NEW ANIME ADDED] {meta['title']} ({len(eps)} eps, {len(streams)} streams)")

            except Exception as e:
                log(f"[{idx}/{total_targets}] Error adding {title} ({aid}): {e}")

    # Checkpoint WAL to make sure database is 100% committed to file
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    conn.close()

    # Sync fresh database copies
    sync_db_copies()

    log("=" * 65)
    log(f"Scraper Run Finished!")
    log(f"New anime added: {new_anime_added}")
    log(f"New episodes added: {new_episodes_added}")
    log(f"Anime updated with new episodes: {len(updated_anime_list)}")
    log("=" * 65)

    return {
        "new_anime_added": new_anime_added,
        "new_anime_list": new_anime_list,
        "new_episodes_added": new_episodes_added,
        "updated_anime_list": updated_anime_list,
    }

if __name__ == "__main__":
    run_sync()

