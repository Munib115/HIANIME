"""
Fast Scraper for New Episodes and Brand New Anime Titles
========================================================
1. Scrapes newly dropped episodes on updated airing anime.
2. Scrapes all 44+ brand-new anime titles discovered on HiAnime.
3. Checkpoints WAL to hianime.db and syncs to all API directories.
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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DB_PATH     = os.path.join(BASE_DIR, "hianime.db")
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
    pool_connections=40,
    pool_maxsize=40,
    max_retries=Retry(total=3, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
)
session.mount("https://", adapter)
session.mount("http://", adapter)

def fetch(url, retries=3, timeout=10, as_json=False):
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json() if as_json else r.text
            elif r.status_code == 429:
                time.sleep(3 * (attempt + 1))
            elif r.status_code == 404:
                return None
            else:
                time.sleep(0.3)
        except Exception:
            time.sleep(0.3)
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
        "status": "Finished Airing",
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
            elif htext == "Status": meta["status"] = val or "Finished Airing"
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
    # Cap stream extraction to avoid getting stuck on massive 1000-ep shows
    target_eps = episodes if len(episodes) <= 50 else (episodes[:25] + episodes[-25:])
    all_streams = []
    workers = min(6, len(target_eps))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futs = {executor.submit(fetch_single_episode_servers, ep): ep for ep in target_eps}
        for f in as_completed(futs):
            try:
                res = f.result()
                if res: all_streams.extend(res)
            except Exception:
                pass
    return all_streams

def sync_db_copies():
    import gzip
    targets = [
        os.path.join(BASE_DIR, "api", "hianime.db.gz"),
        os.path.join(BASE_DIR, "anime_app", "web", "api", "hianime.db.gz"),
    ]
    raw_bytes = open(DB_PATH, "rb").read()
    comp_bytes = gzip.compress(raw_bytes, compresslevel=6)
    for t in targets:
        try:
            os.makedirs(os.path.dirname(t), exist_ok=True)
            with open(t, "wb") as f:
                f.write(comp_bytes)
        except Exception:
            pass

def main():
    print("=" * 65)
    print("KuroAnime Fast New Anime & Episode Scraper Starting")
    print("=" * 65)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")

    c = conn.cursor()
    c.execute("SELECT anime_id, episode_count FROM anime")
    db_anime = dict(c.fetchall())

    c.execute("SELECT DISTINCT episode_id FROM episodes")
    db_ep_ids = set(r[0] for r in c.fetchall() if r[0])

    print(f"Initial DB Stats: {len(db_anime)} Anime, {len(db_ep_ids)} Episodes")

    # ─────────────────────────────────────────────────────────────
    # STEP 1: Discover newly dropped episodes on airing/updated anime
    # ─────────────────────────────────────────────────────────────
    print("\nScanning /recently-updated and /top-airing for episode drops...")
    updated_anime_targets = []
    seen_target_ids = set()

    scan_sources = [
        (f"{BASE_URL}/recently-updated?page={{}}", 20),
        (f"{BASE_URL}/top-airing?page={{}}", 8),
        (f"{BASE_URL}/movie?page={{}}", 8),
        (f"{BASE_URL}/tv?page={{}}", 8),
    ]

    for url_pattern, max_pages in scan_sources:
        for p in range(1, max_pages + 1):
            html = fetch(url_pattern.format(p))
            if not html: break
            soup = BeautifulSoup(html, "html.parser")
            for item in soup.select(".film_list-wrap .flw-item"):
                a = item.select_one(".film-name a")
                if not a or not a.get("href"): continue
                m = re.search(r"-(\d+)$", a.get("href"))
                if not m: continue
                aid = int(m.group(1))
                if aid in seen_target_ids: continue

                ep_sub = item.select_one(".tick-sub")
                ep_num = int(ep_sub.text.strip()) if (ep_sub and ep_sub.text.strip().isdigit()) else 0
                old_ep = db_anime.get(aid, 0)
                if aid in db_anime and ep_num > old_ep:
                    seen_target_ids.add(aid)
                    updated_anime_targets.append({
                        "anime_id": aid,
                        "title": a.text.strip(),
                        "old_count": old_ep,
                        "expected_count": ep_num,
                    })

    print(f"Found {len(updated_anime_targets)} anime with newly dropped episodes.")

    total_new_episodes = 0
    for idx, target in enumerate(updated_anime_targets, 1):
        aid = target["anime_id"]
        title = target["title"]
        try:
            remote_eps = get_episodes(aid)
            new_eps = [ep for ep in remote_eps if ep.get("episode_id") and ep.get("episode_id") not in db_ep_ids]
            if not new_eps: continue

            print(f"  [{idx}/{len(updated_anime_targets)}] [NEW EPISODES] {title}: +{len(new_eps)} episodes (Now {len(remote_eps)})")
            streams = []
            workers = min(5, len(new_eps))
            with ThreadPoolExecutor(max_workers=workers) as ex:
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
            total_new_episodes += len(new_eps)
            db_anime[aid] = len(remote_eps)
        except Exception as e:
            print(f"  Error updating episodes for {title}: {e}")

    # ─────────────────────────────────────────────────────────────
    # STEP 2: Discover & Scrape Brand New Anime Titles from /new-anime
    # ─────────────────────────────────────────────────────────────
    print("\nScanning /new-anime for brand-new anime titles...")
    new_anime_targets = []
    seen_new_ids = set()

    for p in range(1, 25):
        html = fetch(f"{BASE_URL}/new-anime?page={p}")
        if not html: break
        soup = BeautifulSoup(html, "html.parser")
        for item in soup.select(".film_list-wrap .flw-item"):
            a = item.select_one(".film-name a")
            if not a or not a.get("href"): continue
            m = re.search(r"-(\d+)$", a.get("href"))
            if not m: continue
            aid = int(m.group(1))
            if (aid not in db_anime or db_anime.get(aid, 0) == 0) and aid not in seen_new_ids:
                seen_new_ids.add(aid)
                full_url = a.get("href") if a.get("href").startswith("http") else BASE_URL + a.get("href")
                new_anime_targets.append({
                    "anime_id": aid,
                    "title": a.text.strip(),
                    "url": full_url,
                })

    print(f"Discovered {len(new_anime_targets)} brand new anime titles to scrape.")

    total_new_anime = 0
    for idx, target in enumerate(new_anime_targets, 1):
        aid = target["anime_id"]
        title = target["title"]
        url = target["url"]

        try:
            meta = scrape_detail_page(url)
            if not meta or not meta.get("title"):
                continue

            al = enrich_anilist(meta["title"])
            eps = get_episodes(aid)
            if not eps:
                print(f"  [{idx}/{len(new_anime_targets)}] [SKIPPING 0-EP] {meta['title']} (no episodes uploaded on HiAnime yet)")
                continue
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
            total_new_anime += 1
            total_new_episodes += len(eps)
            print(f"  [{idx}/{len(new_anime_targets)}] [NEW ANIME] {meta['title']} ({len(eps)} eps, {len(streams)} streams)")

        except Exception as e:
            print(f"  Error scraping new anime {title} ({aid}): {e}")

    # Step 3: Checkpoint and sync
    print("\nCheckpointing WAL and syncing database files across project...")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    conn.execute("PRAGMA journal_mode = DELETE;")
    conn.close()

    sync_db_copies()

    print("=" * 65)
    print(f"Scrape Complete!")
    print(f"New Anime Titles Added: {total_new_anime}")
    print(f"New Episodes Added: {total_new_episodes}")
    print("=" * 65)

if __name__ == "__main__":
    main()

