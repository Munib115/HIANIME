"""
HiAnime Continuous Airing & New Episodes Sync Daemon
====================================================
Runs continuously in the background to keep the catalog fresh:
1. Monitors /recently-updated, /new-anime, and /top-airing.
2. Detects newly added anime and scrapes them completely.
3. Detects newly dropped episodes on existing airing anime, adds the new episodes and extracts verified streams.
4. Checkpoints WAL and syncs DB copies across web and API directories automatically.
5. Sleeps for interval (default 5 minutes) and repeats indefinitely.
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

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(BASE_DIR, "hianime.db")
LOG_FILE   = os.path.join(BASE_DIR, "airing_daemon.log")
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
    pool_connections=25,
    pool_maxsize=25,
    max_retries=Retry(total=2, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
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
                if elapsed < 0.12:
                    time.sleep(0.12 - elapsed)
                last_request_time = time.time()

            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json() if as_json else r.text
            elif r.status_code == 429:
                wait = 5 * (attempt + 1)
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
    if len(episodes) > 50:
        target_eps = episodes[:25] + episodes[-25:]
    else:
        target_eps = episodes

    all_streams = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futs = {executor.submit(fetch_single_episode_servers, ep): ep for ep in target_eps}
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

def sync_cycle(conn):
    log("Checking HiAnime for new releases & episode updates...")
    c = conn.cursor()
    c.execute("SELECT anime_id, episode_count FROM anime")
    db_anime = dict(c.fetchall())

    c.execute("SELECT DISTINCT episode_id FROM episodes")
    db_ep_ids = set(row[0] for row in c.fetchall() if row[0])

    recent_targets = {}
    def scan_feed(url):
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
                    if aid not in recent_targets:
                        recent_targets[aid] = {"anime_id": aid, "title": a.text.strip(), "url": full_url}

    for p in range(1, 10): scan_feed(f"{BASE_URL}/recently-updated?page={p}")
    for p in range(1, 10): scan_feed(f"{BASE_URL}/new-anime?page={p}")
    for p in range(1, 6): scan_feed(f"{BASE_URL}/top-airing?page={p}")
    for p in range(1, 4): scan_feed(f"{BASE_URL}/search?keyword=uncensored&page={p}")
    for p in range(1, 3): scan_feed(f"{BASE_URL}/search?keyword=uncensor&page={p}")
    for p in range(1, 10): scan_feed(f"{BASE_URL}/genres/hentai?page={p}")

    log(f"Feed scan complete: {len(recent_targets)} titles inspected.")
    updated_count = 0
    new_anime_count = 0

    for aid, item in recent_targets.items():
        title = item["title"]
        url = item["url"]

        if aid in db_anime and db_anime[aid] > 0:
            # Check for new episodes
            remote_eps = get_episodes(aid)
            if not remote_eps: continue
            
            new_eps = [ep for ep in remote_eps if ep.get("episode_id") and ep.get("episode_id") not in db_ep_ids]
            if new_eps:
                log(f"[EPISODE DROP] {title}: {len(new_eps)} new episodes found! (Total: {len(remote_eps)})")
                streams = []
                with ThreadPoolExecutor(max_workers=5) as ex:
                    futs = {ex.submit(fetch_single_episode_servers, ep): ep for ep in new_eps}
                    for f in as_completed(futs):
                        try:
                            res = f.result()
                            if res: streams.extend(res)
                        except Exception:
                            pass

                # Insert new episodes & streams
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

                c.execute("UPDATE anime SET episode_count = ?, scraped_at = ? WHERE anime_id = ?", (len(remote_eps), int(time.time()), aid))
                conn.commit()
                db_anime[aid] = len(remote_eps)
                updated_count += 1
        elif aid not in db_anime or db_anime.get(aid, 0) == 0:
            # Brand new anime
            try:
                meta = scrape_detail_page(url)
                if not meta: continue
                al = enrich_anilist(meta["title"])
                eps = get_episodes(aid)
                streams = get_all_episode_streams(eps)

                ts = int(time.time())
                genres_str = ", ".join(meta.get("genres", []))
                is_airing = "airing" in meta.get("status", "").lower() or (al and al.get("anilist_status") == "RELEASING")
                db_status = "Currently Airing" if is_airing else meta.get("status", "Finished Airing")

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
                    ts, ts if al else None,
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
                new_anime_count += 1
                log(f"[NEW ANIME ADDED] {meta['title']} ({len(eps)} eps, {len(streams)} streams)")
            except Exception as e:
                log(f"Error adding {title}: {e}")

    if updated_count > 0 or new_anime_count > 0:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        sync_db_copies()
        log(f"Cycle completed: {new_anime_count} new anime added, {updated_count} existing anime updated with new episodes.")
    else:
        log("Cycle completed: No new episodes or anime detected.")

def run_daemon(poll_interval_sec=300):
    log("=" * 65)
    log(f"HiAnime Airing & New Episodes Sync Daemon Running (polling every {poll_interval_sec}s)")
    log("=" * 65)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")

    try:
        while True:
            try:
                sync_cycle(conn)
            except Exception as e:
                log(f"Cycle error: {e}")
            time.sleep(poll_interval_sec)
    except KeyboardInterrupt:
        log("Daemon terminated by user.")
    finally:
        conn.close()

if __name__ == "__main__":
    interval = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 300
    run_daemon(interval)
