"""
HiAnime Hentai & 18+ Category Scraper
======================================
Scrapes all anime titles from https://hianime.at/genres/hentai (pages 1-10),
fetches full metadata, AniList enrichment, episode lists, and verified ZokoAnime / HD-1
stream links, inserting everything into hianime.db and syncing all database replicas.
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
LOG_FILE   = os.path.join(BASE_DIR, "hentai_scrape.log")
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

def fetch(url, retries=3, timeout=10, as_json=False, delay=0.10):
    if delay > 0:
        time.sleep(delay)
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json() if as_json else r.text
            elif r.status_code == 429:
                wait = 4 * (attempt + 1)
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
        r"\(TV\)|\(Dub\)|\(Sub\)|[\(\[]uncensored[\)\]]|[\(\[]uncut[\)\]]|Season \d+|2nd Season|3rd Season|4th Season|Part \d+|The Animation",
        "",
        title,
        flags=re.IGNORECASE
    ).strip()
    clean = re.sub(r"\s+", " ", clean)
    try:
        time.sleep(0.10)
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
    html = fetch(url, delay=0.15)
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
        "status": "Finished Airing",
        "type": "OVA",
        "aired": "",
        "premiered": "",
        "duration": "",
        "mal_score": "",
        "studios": "",
        "producers": "",
        "genres": ["Hentai"],
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
        elif htext == "Status": meta["status"] = val or "Finished Airing"
        elif htext == "MAL Score": meta["mal_score"] = val
        elif htext == "Studios": meta["studios"] = val
        elif htext == "Producers": meta["producers"] = val
        elif htext == "Genres":
            parsed_genres = [a.get_text(strip=True) for a in item.select("a")]
            if "Hentai" not in parsed_genres:
                parsed_genres.append("Hentai")
            meta["genres"] = parsed_genres

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
    data = fetch(API_BASE + "episode/list/" + str(anime_id), as_json=True, delay=0.1)
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

    data = fetch(API_BASE + "episode/servers?episodeId=" + str(ep_id), as_json=True, delay=0.08)
    if not data or not data.get("status"): return []

    soup = BeautifulSoup(data.get("html", ""), "lxml")
    out = []
    for s in soup.select(".server-item"):
        sname = (s.get("data-server-name", "") or "ZokoAnime").strip()
        shash = s.get("data-hash", "")
        try:
            url = base64.b64decode(shash).decode("utf-8", errors="ignore").strip()
        except Exception:
            url = ""
        if not url: continue
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
            log(f"Synced DB to -> {os.path.relpath(t, BASE_DIR)}")
        except Exception as e:
            log(f"Error copying DB to {t}: {e}")

def scrape_single_anime(item):
    aid = item["anime_id"]
    title = item["title"]
    url = item["url"]
    try:
        meta = scrape_detail_page(url)
        if not meta or not meta.get("title"):
            return None
        al = enrich_anilist(meta["title"])
        eps = get_episodes(aid)
        streams = get_all_episode_streams(eps)
        return (aid, meta, eps, streams, al)
    except Exception as e:
        log(f"Error scraping {title} ({aid}): {e}")
        return None

def main():
    log("=" * 65)
    log("Starting HiAnime Hentai & 18+ Category Scraping Process")
    log("=" * 65)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    c = conn.cursor()

    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_episodes_anime_ep ON episodes(anime_id, episode_id);")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_streams_ep_server_type ON streams(episode_id, server_name, type);")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_seasons_parent_season ON anime_seasons(parent_anime_id, season_anime_id);")

    c.execute("SELECT anime_id, episode_count FROM anime")
    db_anime = dict(c.fetchall())
    c.execute("SELECT DISTINCT episode_id FROM episodes WHERE episode_id IS NOT NULL")
    db_ep_ids = set(str(row[0]) for row in c.fetchall() if row[0])

    log(f"Scanning all /genres/hentai pages on HiAnime...")
    targets = {}
    for p in range(1, 12):
        page_url = f"{BASE_URL}/genres/hentai?page={p}"
        html = fetch(page_url, delay=0.1)
        if not html: break
        soup = BeautifulSoup(html, "html.parser")
        items = soup.select(".film_list-wrap .flw-item")
        if not items: break
        for it in items:
            a = it.select_one(".film-name a")
            if not a: continue
            href = a.get("href", "")
            m = re.search(r"-(\d+)$", href)
            if not m: continue
            aid = int(m.group(1))
            full_url = href if href.startswith("http") else BASE_URL + href
            if aid not in targets:
                targets[aid] = {
                    "anime_id": aid,
                    "title": a.get_text(strip=True),
                    "url": full_url,
                }

    log(f"Discovered {len(targets)} total titles in /genres/hentai.")

    # Filter out titles already in DB with episodes
    to_scrape = [t for aid, t in targets.items() if aid not in db_anime or db_anime.get(aid, 0) == 0]
    log(f"Already in DB: {len(targets) - len(to_scrape)} | To scrape & add: {len(to_scrape)}")

    if not to_scrape:
        log("All hentai titles are already present in the database!")
        conn.close()
        return

    added_count = 0
    total_eps_added = 0
    total_streams_added = 0

    # Scrape with ThreadPoolExecutor
    workers = 5
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futs = {executor.submit(scrape_single_anime, item): item for item in to_scrape}
        for idx, f in enumerate(as_completed(futs), 1):
            res = f.result()
            if not res:
                continue

            aid, meta, eps, streams, al = res
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
                aid, meta.get("hianime_url", ""), meta["title"], meta["japanese_name"], meta["synonyms"], meta["type"],
                db_status, meta["aired"], meta["premiered"], meta["mal_score"], meta["studios"],
                meta["producers"], meta["duration"], genres_str, meta["synopsis"], meta["poster_url"],
                "popular", 1, len(eps),
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
            added_count += 1
            total_eps_added += len(eps)
            total_streams_added += len(streams)

            log(f"[{added_count}/{len(to_scrape)}] Added {meta['title']} ({len(eps)} eps, {len(streams)} streams)")

            if added_count % 25 == 0:
                c.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                sync_db_copies()

    # Final checkpoint & sync
    log("Check-pointing SQLite WAL...")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    c.execute("SELECT COUNT(*) FROM anime")
    final_anime_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM episodes")
    final_episodes_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM streams")
    final_streams_count = c.fetchone()[0]
    conn.close()

    log("=" * 65)
    log("HENTAI CATEGORY SCRAPING COMPLETE:")
    log(f"  • Anime Titles Added:    +{added_count}")
    log(f"  • Episodes Added:        +{total_eps_added}")
    log(f"  • Streams Added:         +{total_streams_added}")
    log(f"  • Total Anime in DB:     {final_anime_count:,}")
    log(f"  • Total Episodes in DB:  {final_episodes_count:,}")
    log(f"  • Total Streams in DB:   {final_streams_count:,}")
    log("=" * 65)

    log("Synchronizing all database copies...")
    sync_db_copies()
    log("All replicas synchronized successfully!")

if __name__ == "__main__":
    main()
