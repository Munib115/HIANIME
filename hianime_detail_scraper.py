"""
HiAnime.at Detail Scraper - Resumes from existing anime_url_list.json
Scrapes detail pages + episodes for all 10,253 anime.
Supports resume: saves checkpoint every 100 anime and skips already-done IDs.
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests
from bs4 import BeautifulSoup
import re, json, csv, os, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

BASE_URL     = "https://hianime.at"
OUTPUT_DIR   = r"C:\Users\Munib Raza\OneDrive\Desktop\hianime_output"
URL_LIST     = os.path.join(OUTPUT_DIR, "anime_url_list.json")
CHECKPOINT   = os.path.join(OUTPUT_DIR, "hianime_checkpoint.json")
FINAL_JSON   = os.path.join(OUTPUT_DIR, "hianime_anime.json")
FINAL_CSV    = os.path.join(OUTPUT_DIR, "hianime_anime.csv")
EPISODES_CSV = os.path.join(OUTPUT_DIR, "hianime_episodes.csv")
LOG_FILE     = os.path.join(OUTPUT_DIR, "detail_scrape.log")

MAX_WORKERS       = 6
CHECKPOINT_EVERY  = 100
REQUEST_DELAY     = 0.2

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

stats_lock    = Lock()
scraped_count = 0
error_count   = 0
log_fh        = None

def log(msg):
    ts   = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with stats_lock:
        print(line, flush=True)
        if log_fh:
            log_fh.write(line + "\n")
            log_fh.flush()

def fetch(url, retries=3, timeout=30):
    for attempt in range(retries):
        try:
            time.sleep(REQUEST_DELAY)
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.text
            elif r.status_code == 429:
                wait = 5 * (attempt + 1)
                log(f"  Rate limited, sleeping {wait}s")
                time.sleep(wait)
            elif r.status_code == 404:
                return None
            else:
                time.sleep(2)
        except requests.exceptions.Timeout:
            log(f"  Timeout attempt {attempt+1} for {url}")
            time.sleep(2)
        except Exception as e:
            if attempt == retries - 1:
                log(f"  ERROR {url}: {e}")
            time.sleep(1)
    return None

def extract_meta(detail_url, anime_id):
    html = fetch(detail_url)
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    meta = {"anime_id": anime_id, "detail_url": detail_url}

    for sel in ["h2.film-name", ".film-name", "h2", ".dynamic-name"]:
        el = soup.select_one(sel)
        if el:
            meta["title"] = el.get_text(strip=True)
            break

    poster = soup.select_one(".film-poster img, .anime-poster img")
    if poster:
        meta["poster_url"] = poster.get("data-src") or poster.get("src", "")

    for sel in [".film-description .text", ".film-description", ".synopsis"]:
        desc = soup.select_one(sel)
        if desc:
            meta["synopsis"] = desc.get_text(strip=True)
            break

    for item in soup.select(".item, .item-title, .item-list"):
        txt = item.get_text(" ", strip=True)
        prefixes = {
            "Japanese:": "japanese_name", "Synonyms:": "synonyms",
            "Aired:": "aired", "Status:": "status", "MAL Score:": "mal_score",
            "Studios:": "studios", "Duration:": "duration", "Type:": "type",
            "Producers:": "producers",
        }
        for prefix, key in prefixes.items():
            if txt.startswith(prefix):
                meta.setdefault(key, txt.replace(prefix, "", 1).strip())
        if txt.startswith("Genres:"):
            genres_str = txt.replace("Genres:", "").strip()
            meta.setdefault("genres", [g.strip() for g in re.split(r"[,\s]+", genres_str) if g.strip()])
    return meta

def get_episodes(anime_id):
    url  = f"{BASE_URL}/api/theme/episode/list/{anime_id}"
    html = fetch(url, retries=3, timeout=20)
    if not html:
        return []
    try:
        data = json.loads(html)
        if isinstance(data, dict) and "html" in data:
            soup = BeautifulSoup(data["html"], "lxml")
            eps  = []
            for a in soup.select("a.ep-item"):
                href   = a.get("href", "")
                ep_num = a.get("data-number", "")
                ep_id  = a.get("data-id", "")
                txt    = a.get_text(strip=True)
                title  = txt[len(str(ep_num)):].strip() if ep_num and txt.startswith(str(ep_num)) else txt
                m = re.search(r"[?&]ep=(\d+)", href)
                if m:
                    ep_id = m.group(1)
                eps.append({
                    "episode_id":     ep_id,
                    "episode_number": int(ep_num) if str(ep_num).isdigit() else 0,
                    "title":          title,
                    "url":            href if href.startswith("http") else BASE_URL + href,
                })
            return eps
        elif isinstance(data, list):
            return data
    except Exception:
        pass
    return []

def scrape_one(anime_info):
    global scraped_count, error_count
    anime_id   = anime_info.get("anime_id")
    detail_url = anime_info.get("url")
    try:
        meta = extract_meta(detail_url, anime_id)
        if not meta:
            with stats_lock:
                error_count += 1
            return None
        meta["category"]     = anime_info.get("category", "")
        meta["listing_page"] = anime_info.get("listing_page", 0)
        meta.setdefault("title", anime_info.get("title", ""))
        eps                  = get_episodes(anime_id)
        meta["episodes"]     = eps
        meta["episode_count"]= len(eps)
        with stats_lock:
            scraped_count += 1
            if scraped_count % 100 == 0:
                log(f"  Progress: {scraped_count} scraped, {error_count} errors")
        return meta
    except Exception as e:
        with stats_lock:
            error_count += 1
        log(f"  ERROR {anime_id} ({detail_url}): {e}")
        return None

def load_checkpoint():
    if not os.path.exists(CHECKPOINT):
        return [], set()
    try:
        with open(CHECKPOINT, "r", encoding="utf-8") as f:
            data = json.load(f)
        done_ids = {a["anime_id"] for a in data if "anime_id" in a}
        log(f"  Checkpoint loaded: {len(data)} anime already scraped")
        return data, done_ids
    except Exception as e:
        log(f"  WARNING: Cannot load checkpoint: {e}")
        return [], set()

def save_checkpoint(results):
    tmp = CHECKPOINT + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False)
        os.replace(tmp, CHECKPOINT)
    except Exception as e:
        log(f"  WARNING: Checkpoint save failed: {e}")

def run_scrape(anime_list, done_ids):
    remaining = [a for a in anime_list if a.get("anime_id") not in done_ids]
    log(f"  Total in URL list:         {len(anime_list)}")
    log(f"  Already scraped (skipped): {len(done_ids)}")
    log(f"  Remaining to scrape:       {len(remaining)}")
    if not remaining:
        log("  Nothing left to scrape!")
        return []

    new_results  = []
    batch_size   = CHECKPOINT_EVERY
    total_batches= (len(remaining) + batch_size - 1) // batch_size

    for bi in range(total_batches):
        batch = remaining[bi * batch_size : (bi + 1) * batch_size]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(scrape_one, a): a for a in batch}
            for f in as_completed(futures, timeout=300):
                try:
                    r = f.result(timeout=120)
                    if r:
                        new_results.append(r)
                except Exception as e:
                    log(f"  Future error: {e}")
        log(f"  Batch {bi+1}/{total_batches} — {len(new_results)} scraped so far")
        save_checkpoint(new_results)

    log(f"\n  Finished! New anime this run: {len(new_results)}, errors: {error_count}")
    return new_results

def save_data(all_anime):
    log("=" * 60)
    log("Saving output files...")

    with open(FINAL_JSON, "w", encoding="utf-8") as f:
        json.dump(all_anime, f, ensure_ascii=False, indent=2)
    log(f"  hianime_anime.json — {os.path.getsize(FINAL_JSON)/1024/1024:.2f} MB")

    flat_fields = ["anime_id","title","japanese_name","synonyms","type","status",
                   "aired","mal_score","studios","producers","duration","genres",
                   "rating","synopsis","poster_url","detail_url","category",
                   "listing_page","episode_count"]
    rows = []
    for a in all_anime:
        row = {k: a.get(k, "") for k in flat_fields}
        if isinstance(row["genres"], list):
            row["genres"] = ", ".join(row["genres"])
        rows.append(row)
    with open(FINAL_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=flat_fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    log(f"  hianime_anime.csv  — {os.path.getsize(FINAL_CSV)/1024:.0f} KB")

    ep_rows = []
    for a in all_anime:
        aid, title = a.get("anime_id",""), a.get("title","")
        for ep in a.get("episodes", []):
            if isinstance(ep, dict):
                r = {"anime_id": aid, "anime_title": title}; r.update(ep)
                ep_rows.append(r)
    if ep_rows:
        ep_fields = list(ep_rows[0].keys())
        with open(EPISODES_CSV, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=ep_fields, extrasaction="ignore")
            w.writeheader(); w.writerows(ep_rows)
        log(f"  hianime_episodes.csv — {os.path.getsize(EPISODES_CSV)/1024:.0f} KB")

    total_eps = sum(a.get("episode_count",0) for a in all_anime)
    log("=" * 60)
    log(f"DONE — {len(all_anime)} anime, {total_eps} episodes total")
    log("=" * 60)

def main():
    global log_fh
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log_fh = open(LOG_FILE, "a", encoding="utf-8")
    log("=" * 60)
    log("HiAnime.at Detail Scraper (Resume Mode)")
    log(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 60)

    if not os.path.exists(URL_LIST):
        log(f"ERROR: {URL_LIST} not found!")
        return

    with open(URL_LIST, "r", encoding="utf-8") as f:
        anime_list = json.load(f)
    log(f"Loaded {len(anime_list)} anime URLs")

    prev_results, done_ids = load_checkpoint()
    new_results = run_scrape(anime_list, done_ids)
    all_results = prev_results + new_results

    if all_results:
        save_data(all_results)
    else:
        log("No data scraped.")

    if log_fh:
        log_fh.close()

if __name__ == "__main__":
    main()
