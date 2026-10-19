"""
Comprehensive HiAnime Airing & New Anime Scraper
================================================
Scrapes all newly added and currently airing anime from HiAnime:
- /top-airing (pages 1-10)
- /recently-updated (pages 1-16)
- /new-anime (pages 1-25)
- /filter?status=releasing&sort=updated_date (pages 1-16)

Fast, multi-threaded with bounded episode stream fetching so it never stalls.
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests, base64, json, re, time, sqlite3, os, shutil
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(BASE_DIR, "hianime.db")
LOG_FILE   = os.path.join(BASE_DIR, "airing_scrape.log")
BASE_URL   = "https://hianime.at"
API_BASE   = "https://hianime.at/api/theme/"
ANILIST_API= "https://graphql.anilist.co"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://hianime.at/",
}

session = requests.Session()
session.headers.update(HEADERS)
adapter = HTTPAdapter(
    pool_connections=30,
    pool_maxsize=30,
    max_retries=Retry(total=2, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
)
session.mount("https://", adapter)
session.mount("http://", adapter)

rate_lock = Lock()
last_request_time = 0.0

def log(msg):
    line = "[" + time.strftime("%H:%M:%S") + "] " + str(msg)
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def fetch(url, retries=3, timeout=8, as_json=False):
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
    clean = re.sub(r"\(TV\)|\(Dub\)|\(Sub\)|Season \d+|2nd Season|3rd Season|4th Season", "", title).strip()
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

    meta = {"hianime_url": url}
    m = re.search(r"-(\d+)$", url)
    if not m: return None
    meta["anime_id"] = int(m.group(1))

    title_el = soup.select_one(".anisc-detail .film-name")
    meta["title"] = title_el.get_text(strip=True) if title_el else ""

    meta["japanese_name"] = ""
    meta["synonyms"] = ""

    desc_el = soup.select_one(".film-description .text")
    meta["synopsis"] = desc_el.get_text(strip=True) if desc_el else ""

    poster_el = soup.select_one(".anisc-poster .film-poster-img")
    meta["poster_url"] = poster_el.get("src", "") if poster_el else ""

    # Status & types
    meta["status"] = "Currently Airing"
    meta["type"] = "TV"
    meta["aired"] = ""
    meta["premiered"] = ""
    meta["duration"] = ""
    meta["mal_score"] = ""
    meta["studios"] = ""
    meta["producers"] = ""
    meta["genres"] = []

    for item in soup.select(".anisc-info .item"):
        head = item.select_one(".item-head")
        if not head: continue
        htext = head.get_text(strip=True).rstrip(":")
        val_el = item.select_one(".name, .text")
        val = val_el.get_text(strip=True) if val_el else ""
        if htext == "Overview":
            pass
        elif htext == "Japanese":
            meta["japanese_name"] = val
        elif htext == "Synonyms":
            meta["synonyms"] = val
        elif htext == "Aired":
            meta["aired"] = val
        elif htext == "Premiered":
            meta["premiered"] = val
        elif htext == "Duration":
            meta["duration"] = val
        elif htext == "Status":
            meta["status"] = val or "Currently Airing"
        elif htext == "MAL Score":
            meta["mal_score"] = val
        elif htext == "Studios":
            meta["studios"] = val
        elif htext == "Producers":
            meta["producers"] = val
        elif htext == "Genres":
            meta["genres"] = [a.get_text(strip=True) for a in item.select("a")]

    # Check type badge
    type_badge = soup.select_one(".film-stats .item:nth-child(1)")
    if type_badge:
        tb = type_badge.get_text(strip=True).upper()
        if tb in ["TV", "MOVIE", "OVA", "ONA", "SPECIAL"]:
            meta["type"] = tb

    return meta

def get_episodes(anime_id):
    data = fetch(API_BASE + "episode/list/" + str(anime_id), as_json=True)
    if not data or "html" not in data:
        return []
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
    # If episode count is huge (e.g. 500+), scrape first 25 and last 25 episodes
    # so we guarantee streams for early viewers AND latest airing episodes
    if len(episodes) > 50:
        target_eps = episodes[:25] + episodes[-25:]
    else:
        target_eps = episodes

    all_streams = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futs = {executor.submit(fetch_single_episode_servers, ep): ep for ep in target_eps}
        for f in as_completed(futs):
            try:
                res = f.result()
                if res: all_streams.extend(res)
            except Exception:
                pass
    return all_streams

def save_anime_to_db(conn, meta, episodes, streams_data, al):
    c = conn.cursor()
    ts = int(time.time())
    genres_str = ", ".join(meta.get("genres", [])) if isinstance(meta.get("genres"), list) else meta.get("genres", "")

    # Clean status
    raw_status = meta.get("status", "Currently Airing")
    is_airing = "airing" in raw_status.lower() or "releasing" in raw_status.lower() or (al and al.get("anilist_status") == "RELEASING")
    db_status = "Currently Airing" if is_airing else raw_status

    c.execute("""
    INSERT OR REPLACE INTO anime (
        anime_id, hianime_url, title, japanese_name, synonyms, type, status,
        aired, premiered, mal_score, studios, producers, duration, genres,
        synopsis, poster_url, category, listing_page, episode_count,
        anilist_id, title_romaji, title_english, title_native,
        anilist_score, anilist_popularity, poster_large, banner_image,
        anilist_status, al_episodes, season, season_year, al_genres,
        al_description, scraped_at, enriched_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        meta.get("anime_id"),
        meta.get("hianime_url", ""),
        meta.get("title", ""),
        meta.get("japanese_name", ""),
        meta.get("synonyms", ""),
        meta.get("type", "TV"),
        db_status,
        meta.get("aired", ""),
        meta.get("premiered", ""),
        meta.get("mal_score", ""),
        meta.get("studios", ""),
        meta.get("producers", ""),
        meta.get("duration", ""),
        genres_str,
        meta.get("synopsis", ""),
        meta.get("poster_url", ""),
        "airing" if is_airing else "popular",
        1,
        len(episodes),
        al.get("anilist_id") if al else None,
        al.get("title_romaji", "") if al else "",
        al.get("title_english", "") if al else "",
        al.get("title_native", "") if al else "",
        al.get("anilist_score") if al else None,
        al.get("anilist_popularity") if al else None,
        al.get("poster_large", "") if al else "",
        al.get("banner_image", "") if al else "",
        al.get("anilist_status", "RELEASING" if is_airing else "FINISHED") if al else ("RELEASING" if is_airing else None),
        al.get("episodes") if al else None,
        al.get("season", "") if al else "",
        al.get("season_year") if al else None,
        al.get("al_genres", "") if al else "",
        al.get("al_description", "") if al else "",
        ts,
        ts if al else None,
    ))

    for ep in episodes:
        c.execute("""
        INSERT OR IGNORE INTO episodes
            (anime_id, episode_id, episode_number, title, japanese_title, watch_url)
        VALUES (?,?,?,?,?,?)
        """, (
            meta.get("anime_id"),
            ep.get("episode_id", ""),
            ep.get("episode_number", 0),
            ep.get("title", ""),
            ep.get("japanese_title", ""),
            ep.get("watch_url", ""),
        ))

    for s in streams_data:
        c.execute("""
        INSERT OR IGNORE INTO streams
            (anime_id, anime_title, episode_id, episode_number, episode_title,
             type, server_name, provider, embed_url)
        VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            meta.get("anime_id"),
            meta.get("title", ""),
            s.get("episode_id", ""),
            s.get("episode_number", 0),
            s.get("episode_title", ""),
            s.get("type", "sub"),
            s.get("server_name", "ZokoAnime"),
            s.get("provider", ""),
            s.get("embed_url", ""),
        ))

    conn.commit()

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

def collect_all_airing_and_new():
    log("Scanning feeds for all currently airing & newly added anime...")
    targets = {}

    def parse_feed_page(url, priority=1):
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
                        targets[aid] = {"anime_id": aid, "title": a.text.strip(), "url": full_url, "priority": priority}

    # 1. Top Airing (pages 1-10)
    for p in range(1, 11):
        parse_feed_page(f"{BASE_URL}/top-airing?page={p}", priority=1)

    # 2. Recently Updated (pages 1-16)
    for p in range(1, 17):
        parse_feed_page(f"{BASE_URL}/recently-updated?page={p}", priority=2)

    # 3. New Anime (pages 1-25)
    for p in range(1, 26):
        parse_feed_page(f"{BASE_URL}/new-anime?page={p}", priority=3)

    # 4. Filter Airing (pages 1-16)
    for p in range(1, 17):
        parse_feed_page(f"{BASE_URL}/filter?status=releasing&sort=updated_date&page={p}", priority=4)

    log(f"Found {len(targets)} total unique targets across feeds!")
    return list(targets.values())

def main():
    log("=" * 65)
    log("Starting Comprehensive Airing & New Anime Scraping Process")
    log("=" * 65)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")

    # Find existing anime and episode counts
    c = conn.cursor()
    c.execute("SELECT anime_id, episode_count FROM anime")
    existing_eps = dict(c.fetchall())

    targets = collect_all_airing_and_new()
    
    # Filter to anime that are either missing OR have 0 episodes
    to_scrape = []
    for t in targets:
        aid = t["anime_id"]
        if aid not in existing_eps or existing_eps[aid] == 0:
            to_scrape.append(t)

    log(f"Total targets: {len(targets)} | Already present: {len(targets) - len(to_scrape)} | To scrape: {len(to_scrape)}")

    added_count = 0
    for idx, item in enumerate(to_scrape, 1):
        aid = item["anime_id"]
        title = item["title"]
        url = item["url"]

        try:
            meta = scrape_detail_page(url)
            if not meta or not meta.get("title"):
                continue

            al = enrich_anilist(meta["title"])
            eps = get_episodes(aid)
            streams = get_all_episode_streams(eps)

            save_anime_to_db(conn, meta, eps, streams, al)
            added_count += 1
            log(f"[{idx}/{len(to_scrape)}] [ADDED] {meta['title']} ({len(eps)} eps, {len(streams)} streams)")

            if added_count % 15 == 0:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                sync_db_copies()

        except Exception as e:
            log(f"Error scraping {title} ({aid}): {e}")

    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    conn.close()
    sync_db_copies()
    log(f"Backlog complete! Successfully added {added_count} new/airing anime to the catalog.")
    log("Now transitioning to continuous auto-updater daemon mode...")
    
    import daemon_airing_sync
    daemon_airing_sync.run_daemon(poll_interval_sec=300)

if __name__ == "__main__":
    main()
