"""
HiAnime.at FULL Scraper with Stream Links
==========================================
Scrapes ALL anime from hianime.at including:
  - Anime metadata (title, synopsis, genres, status, score, etc.)
  - Episode list with titles
  - Stream servers per episode (Sub/Dub) with decoded embed URLs
  - Download links per episode

APIs used:
  /api/theme/episode/list/{anime_id}         -> episode list
  /api/theme/episode/servers?episodeId={id}  -> server list (base64 embed URLs)
  /api/theme/episode/downloads?episodeId={id}-> download links

Output files:
  hianime_anime.json        - Full data with episodes + streams
  hianime_anime.csv         - Flat anime info (one row per anime)
  hianime_episodes.csv      - Flat episodes (one row per episode)
  hianime_streams.csv       - All stream links (one row per server/episode)
  hianime_checkpoint.json   - Resume checkpoint (auto-saved every 100 anime)
  detail_scrape.log         - Progress log
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests, base64, json, csv, os, re, time
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# ─── Configuration ────────────────────────────────────────────────
BASE_URL     = "https://hianime.at"
API_BASE     = "https://hianime.at/api/theme/"
OUTPUT_DIR   = r"C:\Users\Munib Raza\OneDrive\Desktop\hianime_output"
URL_LIST     = os.path.join(OUTPUT_DIR, "anime_url_list.json")
CHECKPOINT   = os.path.join(OUTPUT_DIR, "hianime_checkpoint.json")
FINAL_JSON   = os.path.join(OUTPUT_DIR, "hianime_anime.json")
FINAL_CSV    = os.path.join(OUTPUT_DIR, "hianime_anime.csv")
EPISODES_CSV = os.path.join(OUTPUT_DIR, "hianime_episodes.csv")
STREAMS_CSV  = os.path.join(OUTPUT_DIR, "hianime_streams.csv")
LOG_FILE     = os.path.join(OUTPUT_DIR, "detail_scrape.log")

MAX_WORKERS      = 5    # concurrent anime scrape threads
CHECKPOINT_EVERY = 100  # save checkpoint every N anime
REQUEST_DELAY    = 0.3  # seconds delay per request (be polite)

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://hianime.at/",
}

# ─── Globals ──────────────────────────────────────────────────────
stats_lock    = Lock()
scraped_count = 0
error_count   = 0
log_fh        = None

# ─── Logging ──────────────────────────────────────────────────────
def log(msg):
    ts   = time.strftime("%H:%M:%S")
    line = "[" + ts + "] " + str(msg)
    with stats_lock:
        print(line, flush=True)
        if log_fh:
            log_fh.write(line + "\n")
            log_fh.flush()

# ─── HTTP helper ──────────────────────────────────────────────────
def fetch(url, retries=3, timeout=30, as_json=False):
    for attempt in range(retries):
        try:
            time.sleep(REQUEST_DELAY)
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json() if as_json else r.text
            elif r.status_code == 429:
                wait = 10 * (attempt + 1)
                log("  Rate limited — sleeping " + str(wait) + "s")
                time.sleep(wait)
            elif r.status_code == 404:
                return None
            else:
                time.sleep(3)
        except requests.exceptions.Timeout:
            log("  Timeout attempt " + str(attempt+1) + " for " + url)
            time.sleep(2)
        except Exception as e:
            if attempt == retries - 1:
                log("  FETCH ERROR " + url + ": " + str(e))
            time.sleep(1)
    return None

# ─── Anime detail page ────────────────────────────────────────────
def extract_meta(detail_url, anime_id):
    html = fetch(detail_url)
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    meta = {"anime_id": anime_id, "detail_url": detail_url}

    # Title
    for sel in ["h2.film-name", ".film-name", "h1", ".dynamic-name"]:
        el = soup.select_one(sel)
        if el:
            meta["title"] = el.get_text(strip=True)
            break

    # Poster
    for sel in [".film-poster img", ".anime-poster img"]:
        el = soup.select_one(sel)
        if el:
            meta["poster_url"] = el.get("data-src") or el.get("src", "")
            break

    # Synopsis
    for sel in [".film-description .text", ".film-description", ".synopsis"]:
        el = soup.select_one(sel)
        if el:
            meta["synopsis"] = el.get_text(strip=True)
            break

    # Info items
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
            meta.setdefault("genres", [g.strip() for g in re.split(r"[,]+", genres_str) if g.strip()])

    return meta

# ─── Episode list ─────────────────────────────────────────────────
def get_episodes(anime_id):
    url  = API_BASE + "episode/list/" + str(anime_id)
    data = fetch(url, retries=3, timeout=20, as_json=True)
    if not data or not isinstance(data, dict) or "html" not in data:
        return []
    soup = BeautifulSoup(data["html"], "lxml")
    eps  = []
    for a in soup.select("a.ep-item"):
        href      = a.get("href", "")
        ep_num    = a.get("data-number", "")
        ep_id     = a.get("data-id", "")
        title_el  = a.select_one(".ep-name")
        title     = title_el.get_text(strip=True) if title_el else a.get("title", "")
        jname     = title_el.get("data-jname", "") if title_el else ""
        # Prefer ep id from URL ?ep= param
        m = re.search(r"[?&]ep=(\d+)", href)
        if m:
            ep_id = m.group(1)
        eps.append({
            "episode_id":     ep_id,
            "episode_number": int(ep_num) if str(ep_num).isdigit() else 0,
            "title":          title,
            "japanese_title": jname,
            "watch_url":      href if href.startswith("http") else BASE_URL + href,
            "streams":        [],   # filled by get_episode_streams()
        })
    return eps

# ─── Stream servers for one episode ───────────────────────────────
def get_episode_streams(ep_id):
    url  = API_BASE + "episode/servers?episodeId=" + str(ep_id)
    data = fetch(url, retries=3, timeout=20, as_json=True)
    if not data or not isinstance(data, dict) or not data.get("status"):
        return []
    soup    = BeautifulSoup(data.get("html", ""), "lxml")
    streams = []
    for s in soup.select(".server-item"):
        server_name = s.get("data-server-name", "")
        stype       = s.get("data-type", "")   # "sub" or "dub"
        shash       = s.get("data-hash", "")
        embed_url   = ""
        if shash:
            try:
                embed_url = base64.b64decode(shash).decode("utf-8")
            except Exception:
                embed_url = ""
        if embed_url:
            # Extract the streaming provider from the URL domain
            provider = re.search(r"https?://([^/]+)", embed_url)
            provider = provider.group(1) if provider else ""
            streams.append({
                "server_name": server_name,
                "type":        stype,          # sub or dub
                "embed_url":   embed_url,
                "provider":    provider,
            })
    return streams

# ─── Per-anime scrape task ────────────────────────────────────────
def scrape_one(anime_info):
    global scraped_count, error_count
    anime_id   = anime_info.get("anime_id")
    detail_url = anime_info.get("url")
    try:
        # 1. Metadata
        meta = extract_meta(detail_url, anime_id)
        if not meta:
            with stats_lock:
                error_count += 1
            return None
        meta["category"]     = anime_info.get("category", "")
        meta["listing_page"] = anime_info.get("listing_page", 0)
        meta.setdefault("title", anime_info.get("title", ""))

        # 2. Episode list
        episodes = get_episodes(anime_id)

        # 3. Stream links for each episode
        for ep in episodes:
            ep_id = ep.get("episode_id")
            if ep_id:
                ep["streams"] = get_episode_streams(ep_id)

        meta["episodes"]      = episodes
        meta["episode_count"] = len(episodes)

        with stats_lock:
            scraped_count += 1
            if scraped_count % 25 == 0:
                total_ep_streams = sum(
                    sum(len(ep.get("streams", [])) for ep in a.get("episodes", []))
                    for a in []  # avoid holding lock too long
                )
                log("  Progress: " + str(scraped_count) + " anime, " + str(error_count) + " errors")
        return meta

    except Exception as e:
        with stats_lock:
            error_count += 1
        log("  ERROR " + str(anime_id) + " (" + str(detail_url) + "): " + str(e))
        return None

# ─── Checkpoint helpers ───────────────────────────────────────────
def load_checkpoint():
    if not os.path.exists(CHECKPOINT):
        return [], set()
    try:
        with open(CHECKPOINT, "r", encoding="utf-8") as f:
            data = json.load(f)
        done_ids = {a["anime_id"] for a in data if "anime_id" in a}
        log("  Checkpoint loaded: " + str(len(data)) + " anime already scraped")
        return data, done_ids
    except Exception as e:
        log("  WARNING: Cannot load checkpoint: " + str(e))
        return [], set()

def save_checkpoint(results):
    tmp = CHECKPOINT + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False)
        os.replace(tmp, CHECKPOINT)
    except Exception as e:
        log("  WARNING: Checkpoint save failed: " + str(e))

# ─── Main scraping loop ───────────────────────────────────────────
def run_scrape(anime_list, done_ids):
    remaining = [a for a in anime_list if a.get("anime_id") not in done_ids]
    log("  Total in URL list:         " + str(len(anime_list)))
    log("  Already scraped (skipped): " + str(len(done_ids)))
    log("  Remaining to scrape:       " + str(len(remaining)))

    if not remaining:
        log("  Nothing left to scrape!")
        return []

    new_results  = []
    batch_size   = CHECKPOINT_EVERY
    total_batches = (len(remaining) + batch_size - 1) // batch_size

    for bi in range(total_batches):
        batch = remaining[bi * batch_size : (bi + 1) * batch_size]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(scrape_one, a): a for a in batch}
            for f in as_completed(futures, timeout=600):
                try:
                    r = f.result(timeout=300)
                    if r:
                        new_results.append(r)
                except Exception as e:
                    log("  Future error: " + str(e))
        log("  Batch " + str(bi+1) + "/" + str(total_batches) + " done — " + str(len(new_results)) + " new scraped")
        save_checkpoint(new_results)

    log("  Finished! New anime this run: " + str(len(new_results)) + ", errors: " + str(error_count))
    return new_results

# ─── Save output files ────────────────────────────────────────────
def save_data(all_anime):
    log("=" * 60)
    log("Saving output files...")

    # --- hianime_anime.json ---
    with open(FINAL_JSON, "w", encoding="utf-8") as f:
        json.dump(all_anime, f, ensure_ascii=False, indent=2)
    log("  hianime_anime.json  — " + str(round(os.path.getsize(FINAL_JSON)/1024/1024, 2)) + " MB")

    # --- hianime_anime.csv ---
    flat_fields = ["anime_id","title","japanese_name","synonyms","type","status",
                   "aired","premiered","mal_score","studios","producers","duration",
                   "genres","synopsis","poster_url","detail_url","category",
                   "listing_page","episode_count"]
    rows = []
    for a in all_anime:
        row = {k: a.get(k, "") for k in flat_fields}
        if isinstance(row.get("genres"), list):
            row["genres"] = ", ".join(row["genres"])
        rows.append(row)
    with open(FINAL_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=flat_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log("  hianime_anime.csv   — " + str(round(os.path.getsize(FINAL_CSV)/1024, 1)) + " KB")

    # --- hianime_episodes.csv ---
    ep_rows = []
    for a in all_anime:
        aid   = a.get("anime_id", "")
        title = a.get("title", "")
        for ep in a.get("episodes", []):
            if isinstance(ep, dict):
                r = {"anime_id": aid, "anime_title": title,
                     "episode_id": ep.get("episode_id",""),
                     "episode_number": ep.get("episode_number",""),
                     "title": ep.get("title",""),
                     "japanese_title": ep.get("japanese_title",""),
                     "watch_url": ep.get("watch_url",""),
                     "stream_count": len(ep.get("streams",[]))}
                ep_rows.append(r)
    if ep_rows:
        ep_fields = list(ep_rows[0].keys())
        with open(EPISODES_CSV, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=ep_fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(ep_rows)
        log("  hianime_episodes.csv — " + str(round(os.path.getsize(EPISODES_CSV)/1024, 1)) + " KB")

    # --- hianime_streams.csv ---
    st_rows = []
    for a in all_anime:
        aid       = a.get("anime_id", "")
        atitle    = a.get("title", "")
        for ep in a.get("episodes", []):
            if not isinstance(ep, dict):
                continue
            ep_id  = ep.get("episode_id","")
            ep_num = ep.get("episode_number","")
            ep_ttl = ep.get("title","")
            for s in ep.get("streams", []):
                st_rows.append({
                    "anime_id":      aid,
                    "anime_title":   atitle,
                    "episode_id":    ep_id,
                    "episode_number":ep_num,
                    "episode_title": ep_ttl,
                    "type":          s.get("type",""),        # sub / dub
                    "server_name":   s.get("server_name",""),
                    "provider":      s.get("provider",""),
                    "embed_url":     s.get("embed_url",""),
                })
    if st_rows:
        st_fields = list(st_rows[0].keys())
        with open(STREAMS_CSV, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=st_fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(st_rows)
        log("  hianime_streams.csv  — " + str(round(os.path.getsize(STREAMS_CSV)/1024/1024, 2)) + " MB  (" + str(len(st_rows)) + " stream links)")
    else:
        log("  No stream links found.")

    total_eps     = sum(a.get("episode_count",0) for a in all_anime)
    total_streams = len(st_rows)
    log("=" * 60)
    log("DONE — " + str(len(all_anime)) + " anime | " + str(total_eps) + " episodes | " + str(total_streams) + " stream links")
    log("=" * 60)

# ─── Entry point ──────────────────────────────────────────────────
def main():
    global log_fh
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log_fh = open(LOG_FILE, "a", encoding="utf-8")

    log("=" * 60)
    log("HiAnime.at FULL Scraper — Anime + Episodes + Streams")
    log("Started: " + time.strftime("%Y-%m-%d %H:%M:%S"))
    log("=" * 60)

    if not os.path.exists(URL_LIST):
        log("ERROR: " + URL_LIST + " not found!")
        return

    with open(URL_LIST, "r", encoding="utf-8") as f:
        anime_list = json.load(f)
    log("Loaded " + str(len(anime_list)) + " anime URLs")

    prev_results, done_ids = load_checkpoint()
    new_results  = run_scrape(anime_list, done_ids)
    all_results  = prev_results + new_results

    if all_results:
        save_data(all_results)
    else:
        log("No data to save.")

    if log_fh:
        log_fh.close()

if __name__ == "__main__":
    main()
