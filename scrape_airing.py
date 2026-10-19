"""
HiAnime Airing Anime Scraper
============================
Specifically targets and scrapes all currently airing anime from hianime.at:
- /top-airing
- /filter?status=releasing&sort=updated_date (all pages)
- /filter?status=releasing&sort=trending
- /recently-updated

Extracts:
- Complete metadata (Title, synopsis, genres, type, status, premier, duration)
- Episodes & stream embed links (Sub & Dub)
- AniList enrichment (high-res posters, banners, scores)
- Inserts/updates into hianime.db and syncs to api/ directories
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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://hianime.at/",
}

session = requests.Session()
session.headers.update(HEADERS)
adapter = HTTPAdapter(
    pool_connections=25,
    pool_maxsize=25,
    max_retries=Retry(total=3, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
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

def fetch(url, retries=4, timeout=12, as_json=False):
    global last_request_time
    for attempt in range(retries):
        try:
            with rate_lock:
                now = time.time()
                elapsed = now - last_request_time
                if elapsed < 0.15:
                    time.sleep(0.15 - elapsed)
                last_request_time = time.time()

            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json() if as_json else r.text
            elif r.status_code == 429:
                wait = 5 * (attempt + 1)
                log(f"  [429] Rate limited on {url[:60]} — sleeping {wait}s")
                time.sleep(wait)
            elif r.status_code == 404:
                return None
            else:
                time.sleep(1)
        except Exception as e:
            if attempt == retries - 1:
                log(f"  FETCH ERR {url[:60]}: {e}")
            time.sleep(1)
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

last_anilist_time = 0.0

def anilist_lookup(title):
    global last_anilist_time
    try:
        now = time.time()
        elapsed = now - last_anilist_time
        if elapsed < 0.6:
            time.sleep(0.6 - elapsed)
        last_anilist_time = time.time()

        r = session.post(
            ANILIST_API,
            json={"query": ANILIST_QUERY, "variables": {"search": title}},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            media = data.get("data", {}).get("Media")
            if media:
                return {
                    "anilist_id": media.get("id"),
                    "title_romaji": media.get("title", {}).get("romaji", ""),
                    "title_english": media.get("title", {}).get("english", ""),
                    "title_native": media.get("title", {}).get("native", ""),
                    "anilist_score": media.get("averageScore"),
                    "anilist_popularity": media.get("popularity"),
                    "poster_large": media.get("coverImage", {}).get("extraLarge") or media.get("coverImage", {}).get("large", ""),
                    "banner_image": media.get("bannerImage", ""),
                    "anilist_status": media.get("status", ""),
                    "episodes": media.get("episodes"),
                    "season": media.get("season", ""),
                    "season_year": media.get("seasonYear"),
                    "al_genres": ", ".join(media.get("genres", [])),
                    "al_description": (media.get("description") or "")[:2000],
                }
    except Exception:
        pass
    return None

def extract_meta(detail_url, anime_id):
    html = fetch(detail_url)
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    meta = {"anime_id": anime_id, "detail_url": detail_url}

    for sel in ["h2.film-name", ".film-name", "h1", ".dynamic-name"]:
        el = soup.select_one(sel)
        if el:
            meta["title"] = el.get_text(strip=True)
            break

    for sel in [".film-poster img", ".anime-poster img"]:
        el = soup.select_one(sel)
        if el:
            meta["poster_url"] = el.get("data-src") or el.get("src", "")
            break

    for sel in [".film-description .text", ".film-description", ".synopsis"]:
        el = soup.select_one(sel)
        if el:
            meta["synopsis"] = el.get_text(strip=True)
            break

    prefixes = {
        "Japanese:": "japanese_name", "Synonyms:": "synonyms",
        "Aired:": "aired", "Status:": "status", "MAL Score:": "mal_score",
        "Studios:": "studios", "Duration:": "duration", "Type:": "type",
        "Producers:": "producers", "Premiered:": "premiered",
    }
    for item in soup.select(".item, .item-title, .item-list"):
        txt = item.get_text(" ", strip=True)
        for prefix, key in prefixes.items():
            if txt.startswith(prefix):
                meta.setdefault(key, txt.replace(prefix, "", 1).strip())
        if txt.startswith("Genres:"):
            genres_str = txt.replace("Genres:", "").strip()
            meta.setdefault("genres", [g.strip() for g in re.split(r",", genres_str) if g.strip()])

    # Force currently airing status for this pipeline
    if not meta.get("status"):
        meta["status"] = "Currently Airing"

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
    if not ep_id:
        return []

    data = fetch(API_BASE + "episode/servers?episodeId=" + str(ep_id), as_json=True)
    if not data or not data.get("status"):
        return []

    soup = BeautifulSoup(data.get("html", ""), "lxml")
    out = []
    for s in soup.select(".server-item"):
        sname = (s.get("data-server-name", "") or "ZokoAnime").strip()
        if "HD-1" in sname or "HD 1" in sname:
            continue
        shash = s.get("data-hash", "")
        try:
            url = base64.b64decode(shash).decode("utf-8", errors="ignore").strip()
        except Exception:
            url = ""
        if not url:
            continue
        provider = re.search(r"https?://([^/]+)", url)
        prov_name = provider.group(1) if provider else ""
        if "megaplay" in prov_name.lower():
            continue
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

def get_all_episode_streams(episodes, title=""):
    if not episodes:
        return []
    all_streams = []
    workers = min(6, len(episodes))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_single_episode_servers, ep): ep for ep in episodes}
        for f in as_completed(futures):
            try:
                res = f.result()
                if res:
                    all_streams.extend(res)
            except Exception:
                pass
    return all_streams

def save_anime(conn, meta, episodes, streams_data, al):
    c = conn.cursor()
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
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
        meta.get("anime_id"),
        meta.get("detail_url", ""),
        meta.get("title", ""),
        meta.get("japanese_name", ""),
        meta.get("synonyms", ""),
        meta.get("type", ""),
        "Currently Airing",
        meta.get("aired", ""),
        meta.get("premiered", ""),
        meta.get("mal_score", ""),
        meta.get("studios", ""),
        meta.get("producers", ""),
        meta.get("duration", ""),
        ", ".join(meta.get("genres", [])) if isinstance(meta.get("genres"), list) else meta.get("genres", ""),
        meta.get("synopsis", ""),
        meta.get("poster_url", ""),
        "airing",
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
        "RELEASING",
        al.get("episodes") if al else None,
        al.get("season", "") if al else "",
        al.get("season_year") if al else None,
        al.get("al_genres", "") if al else "",
        al.get("al_description", "") if al else "",
        seasons_json,
        ts,
        ts if al else None,
    ))

    # Episodes
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

    # Streams
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
            s.get("type", ""),
            s.get("server_name", ""),
            s.get("provider", ""),
            s.get("embed_url", ""),
        ))

    conn.commit()

def sync_db_copies():
    """Copy fresh hianime.db to all app subdirectories so APIs have immediate access."""
    targets = [
        os.path.join(BASE_DIR, "api", "hianime.db"),
        os.path.join(BASE_DIR, "anime_app", "web", "api", "hianime.db"),
        os.path.join(BASE_DIR, "anime_app", "web", "hianime.db"),
    ]
    for t in targets:
        try:
            os.makedirs(os.path.dirname(t), exist_ok=True)
            shutil.copy2(DB_PATH, t)
        except Exception as e:
            pass

def collect_airing_targets():
    log("Gathering all currently airing anime URLs from HiAnime...")
    targets = {}

    # 1. Top Airing (first 5 pages)
    for p in range(1, 6):
        html = fetch(f"{BASE_URL}/top-airing?page={p}")
        if not html: break
        soup = BeautifulSoup(html, "html.parser")
        for item in soup.select(".film_list-wrap .flw-item"):
            a = item.select_one(".film-name a")
            if a and a.get("href"):
                href = a.get("href")
                m = re.search(r"-(\d+)$", href)
                if m:
                    aid = int(m.group(1))
                    full_url = href if href.startswith("http") else BASE_URL + href
                    targets[aid] = {"anime_id": aid, "title": a.text.strip(), "url": full_url, "priority": 1}

    # 2. Recently Updated Airing (first 16 pages)
    for p in range(1, 17):
        html = fetch(f"{BASE_URL}/filter?status=releasing&sort=updated_date&page={p}")
        if not html: break
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
                        targets[aid] = {"anime_id": aid, "title": a.text.strip(), "url": full_url, "priority": 2}

    log(f"Found {len(targets)} total unique currently airing anime on HiAnime!")
    return list(targets.values())

def main():
    log("=" * 65)
    log("HiAnime Currently Airing Anime Scraper Started")
    log("=" * 65)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")

    # Check existing episode counts
    c = conn.cursor()
    c.execute("SELECT anime_id, episode_count FROM anime")
    existing_eps = dict(c.fetchall())

    anime_list = collect_airing_targets()
    total = len(anime_list)
    log(f"Processing {total} currently airing anime...")

    scraped = 0
    updated = 0
    start_time = time.time()

    for idx, anime in enumerate(anime_list, 1):
        aid = anime["anime_id"]
        url = anime["url"]
        fallback_title = anime["title"]

        try:
            # 1. Fetch metadata
            meta = extract_meta(url, aid)
            if not meta:
                log(f"[{idx}/{total}] SKIP: {fallback_title}")
                continue

            title = meta.get("title", fallback_title)

            # 2. Episode list
            episodes = get_episodes(aid)
            current_ep_count = len(episodes)
            old_ep_count = existing_eps.get(aid, -1)

            # If anime is in DB and episode count hasn't changed, just ensure status is Currently Airing
            if old_ep_count >= current_ep_count and old_ep_count > 0:
                conn.execute("UPDATE anime SET status='Currently Airing', anilist_status='RELEASING' WHERE anime_id=?", (aid,))
                conn.commit()
                continue

            # 3. Stream server embed URLs
            streams = get_all_episode_streams(episodes, title)

            # 4. AniList enrichment
            al = anilist_lookup(title)
            if not al and meta.get("japanese_name"):
                al = anilist_lookup(meta["japanese_name"])

            if al and al.get("poster_large") and not meta.get("poster_url"):
                meta["poster_url"] = al["poster_large"]

            # 5. Save to SQLite
            save_anime(conn, meta, episodes, streams, al)
            scraped += 1

            if old_ep_count > 0:
                updated += 1
                log(f"[{idx}/{total}] [UPDATED NEW EPS] {title} ({len(episodes)} eps, +{len(episodes)-old_ep_count} new)")
            else:
                log(f"[{idx}/{total}] [ADDED NEW AIRING] {title} ({len(episodes)} eps, {len(streams)} streams)")

            # Periodic sync every 10 anime
            if scraped % 10 == 0:
                sync_db_copies()

        except Exception as e:
            log(f"[{idx}/{total}] ERROR {aid} ({fallback_title}): {e}")

    conn.close()
    sync_db_copies()

    elapsed = time.time() - start_time
    log("=" * 65)
    log(f"Airing Scraping Finished in {elapsed/60:.1f} mins! Scraped/Updated: {scraped} anime.")
    log("=" * 65)

if __name__ == "__main__":
    main()
