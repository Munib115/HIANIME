"""Train Vegeta AI with deep Anime & Character Lore from MyAnimeList & AniList.

Builds ``api/vegeta_char_lore.json.gz`` (and mirrors to ``anime_app/web/api/``
and ``anime_app/server/``) containing rich biographies, abilities, affiliations,
aliases, native names, Japanese seiyuu (voice actors), and popularity rankings
for the top ~4,500+ iconic anime characters in KuroAnime's dual-server catalog.
"""

import gzip
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request

from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_LORE = os.path.join(ROOT, "api", "vegeta_char_lore.json.gz")
MIRRORS = [
    os.path.join(ROOT, "anime_app", "web", "api"),
    os.path.join(ROOT, "anime_app", "server"),
]

SCRATCH_DIR = os.environ.get(
    "VEGETA_SCRATCH_DIR",
    r"C:\Users\Munib Raza\.gemini\antigravity\brain\a91d790b-8804-4c78-b699-10705ba5d729\scratch",
)
CHAR_CACHE_PATH = os.path.join(SCRATCH_DIR, "anilist_characters_cache.json.gz")
ANILIST_LORE_CACHE = os.path.join(SCRATCH_DIR, "anilist_char_lore_cache.json.gz")
MAL_CHAR_CACHE = os.path.join(SCRATCH_DIR, "mal_top_chars_cache.json.gz")
KB_PATH = os.path.join(ROOT, "api", "vegeta_mal_knowledge.json.gz")

ANILIST_URL = "https://graphql.anilist.co"
CHAR_BATCH_QUERY = """
query ($ids: [Int]) {
  Page(page: 1, perPage: 50) {
    characters(id_in: $ids) {
      id
      name { full native alternative }
      gender
      age
      bloodType
      favourites
      description(asHtml: false)
      media(sort: POPULARITY_DESC, perPage: 3, type: ANIME) {
        edges {
          characterRole
          voiceActors(language: JAPANESE, sort: FAVOURITES_DESC) {
            name { full native }
          }
          node {
            id
            idMal
            title { english romaji }
          }
        }
      }
    }
  }
}
"""

ANIME_FALLBACK_QUERY = """
query ($ids: [Int]) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      idMal
      title { english romaji }
      characters(sort: [ROLE, FAVOURITES_DESC], perPage: 8) {
        edges {
          role
          voiceActors(language: JAPANESE, sort: FAVOURITES_DESC) {
            name { full native }
          }
          node {
            id
            name { full native alternative }
            gender
            age
            bloodType
            favourites
            description(asHtml: false)
          }
        }
      }
    }
  }
}
"""


def clean_bio(text: str, max_len: int = 620) -> str:
    if not text:
        return ""
    # Strip AniList spoiler tags ~!...!~ safely (keep content concise, remove markup)
    text = re.sub(r"~!(.+?)!~", "", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\(Source:.*?\)", "", text, flags=re.I | re.S)
    text = re.sub(r"^(?:Details\s+Clubs\s+Pictures\s*)+", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 3].rsplit(" ", 1)[0] + "..."
    return text


def post_anilist(query: str, variables: dict) -> dict | None:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    for attempt in range(5):
        req = urllib.request.Request(
            ANILIST_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "KuroAnime-VegetaLoreTrainer/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "15")) + 2
                print(f"  [AniList 429] Sleeping {wait}s...", flush=True)
                time.sleep(wait)
                continue
            time.sleep(2 * (attempt + 1))
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


def collect_target_char_ids(max_chars: int = 4500) -> list[int]:
    if os.path.exists(CHAR_CACHE_PATH):
        try:
            with gzip.open(CHAR_CACHE_PATH, "rb") as f:
                data = json.loads(f.read().decode("utf-8"))
            chars = data.get("chars") or []
            chars.sort(key=lambda x: (int(x.get("fav") or 0), 1 if x.get("r") == "M" else 0), reverse=True)
            cids = [int(c["cid"]) for c in chars[:max_chars] if c.get("cid")]
            if cids:
                print(f"Selected top {len(cids)} characters from {len(chars)} catalog characters.", flush=True)
                return cids
        except Exception as err:
            print(f"Char cache fallback: {err}", flush=True)

    # Fallback: discover from top anime in vegeta_mal_knowledge.json.gz
    with gzip.open(KB_PATH, "rb") as f:
        kb = json.loads(f.read().decode("utf-8"))
    al_ids = [int(k) for k in list(kb.keys())[:900]]
    seen = set()
    cids = []
    for i in range(0, len(al_ids), 50):
        res = post_anilist(ANIME_FALLBACK_QUERY, {"ids": al_ids[i:i + 50]})
        for m in ((res or {}).get("data") or {}).get("Page", {}).get("media") or []:
            for e in (m.get("characters") or {}).get("edges") or []:
                nd = e.get("node") or {}
                cid = nd.get("id")
                if cid and cid not in seen:
                    seen.add(cid)
                    cids.append((int(nd.get("favourites") or 0), int(cid)))
        time.sleep(0.75)
    cids.sort(reverse=True)
    return [c for _, c in cids[:max_chars]]


def fetch_anilist_character_details(cids: list[int]) -> dict[str, dict]:
    lore_by_cid: dict[str, dict] = {}
    if os.path.exists(ANILIST_LORE_CACHE):
        try:
            with gzip.open(ANILIST_LORE_CACHE, "rb") as f:
                lore_by_cid = json.loads(f.read().decode("utf-8"))
            print(f"Loaded {len(lore_by_cid)} cached AniList character bios.", flush=True)
        except Exception:
            lore_by_cid = {}

    todo = [cid for cid in cids if str(cid) not in lore_by_cid]
    print(f"Fetching rich AniList character lore + seiyuu for {len(todo)}/{len(cids)} characters...", flush=True)
    batch_size = 50
    total_batches = (len(todo) + batch_size - 1) // batch_size

    for i in range(0, len(todo), batch_size):
        batch = todo[i:i + batch_size]
        res = post_anilist(CHAR_BATCH_QUERY, {"ids": batch})
        chars = (((res or {}).get("data") or {}).get("Page") or {}).get("characters") or []
        for ch in chars:
            cid = ch.get("id")
            if not cid:
                continue
            name_obj = ch.get("name") or {}
            full_name = (name_obj.get("full") or "").strip()
            if not full_name:
                continue
            alts = [a.strip() for a in (name_obj.get("alternative") or []) if a and len(a.strip()) >= 2][:4]
            native = (name_obj.get("native") or "").strip()

            al_id = None
            mal_id = None
            anime_title = ""
            role = "S"
            va_name = ""
            edges = (ch.get("media") or {}).get("edges") or []
            if edges:
                ed0 = edges[0]
                node0 = ed0.get("node") or {}
                al_id = node0.get("id")
                mal_id = node0.get("idMal")
                t_obj = node0.get("title") or {}
                anime_title = (t_obj.get("english") or t_obj.get("romaji") or "").strip()
                role = "M" if ed0.get("characterRole") == "MAIN" else "S"
                vas = ed0.get("voiceActors") or []
                if vas:
                    va_name = ((vas[0].get("name") or {}).get("full") or "").strip()

            bio = clean_bio(ch.get("description") or "")
            lore_by_cid[str(cid)] = {
                "cid": int(cid),
                "n": full_name,
                "nat": native,
                "a": alts,
                "r": role,
                "g": (ch.get("gender") or "").strip(),
                "age": (ch.get("age") or "").strip()[:24],
                "bt": (ch.get("bloodType") or "").strip()[:8],
                "fav": int(ch.get("favourites") or 0),
                "al": int(al_id) if al_id else None,
                "mal": int(mal_id) if mal_id else None,
                "t": anime_title,
                "va": va_name,
                "bio": bio,
            }

        bnum = (i // batch_size) + 1
        if bnum % 10 == 0 or bnum == total_batches:
            os.makedirs(SCRATCH_DIR, exist_ok=True)
            with gzip.open(ANILIST_LORE_CACHE, "wb") as f:
                f.write(json.dumps(lore_by_cid, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            print(f"  AniList batch {bnum}/{total_batches} ({len(lore_by_cid)} stored)", flush=True)
        time.sleep(0.75)

    return lore_by_cid


def scrape_mal_top_characters(limit_chars: int = 250) -> dict[str, dict]:
    """Fetch rich MyAnimeList biographies & abilities for top MAL characters."""
    mal_cache: dict[str, dict] = {}
    if os.path.exists(MAL_CHAR_CACHE):
        try:
            with gzip.open(MAL_CHAR_CACHE, "rb") as f:
                mal_cache = json.loads(f.read().decode("utf-8"))
            print(f"Loaded {len(mal_cache)} cached MyAnimeList character profiles.", flush=True)
        except Exception:
            mal_cache = {}

    char_links: list[tuple[str, str]] = []
    for offset in range(0, limit_chars, 50):
        url = f"https://myanimelist.net/character.php?limit={offset}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            soup = BeautifulSoup(html, "lxml")
            for row in soup.select("tr.ranking-list"):
                a = row.select_one("a.fs14") or row.select_one("a[href*='/character/']")
                if not a or not a.get("href"):
                    continue
                href = a["href"].split("?")[0]
                m_id = re.search(r"/character/(\d+)", href)
                if m_id:
                    char_links.append((m_id.group(1), href))
        except Exception as err:
            print(f"  MAL ranking page {offset} skip: {err}", flush=True)
            break
        time.sleep(0.9)

    todo = [(mcid, href) for mcid, href in char_links[:limit_chars] if mcid not in mal_cache]
    print(f"Enriching {len(todo)} MyAnimeList character pages (out of {len(char_links[:limit_chars])})...", flush=True)
    for idx, (mcid, href) in enumerate(todo, 1):
        try:
            req = urllib.request.Request(href, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            soup = BeautifulSoup(html, "lxml")
            h2 = soup.select_one("h2.normal_header")
            mal_name = ""
            if h2:
                mal_name = re.sub(r"\s*\([^)]*\)\s*$", "", h2.get_text(" ", strip=True)).strip()
            td = soup.select_one("#content td[style*='padding-left']") or soup.select_one("#content table td:nth-of-type(2)")
            bio_raw = ""
            if td:
                # Strip breadcrumbs/headers cleanly
                for rm in td.select(".breadcrumb, .normal_header, .js-sns-icon-container, script, style"):
                    rm.decompose()
                bio_raw = " ".join(td.get_text(" ", strip=True).split())
                # Drop trailing "No voice actors..." or voice actor table tail
                bio_raw = re.split(r"\bVoice Actors\b|\bAnimeography\b", bio_raw)[0].strip()
            bio_clean = clean_bio(bio_raw, max_len=650)
            mal_cache[mcid] = {"mal_cid": int(mcid), "n": mal_name, "bio": bio_clean}
        except Exception as err:
            print(f"  MAL char {mcid} skip: {err}", flush=True)
            time.sleep(2.5)
            continue

        if idx % 25 == 0 or idx == len(todo):
            with gzip.open(MAL_CHAR_CACHE, "wb") as f:
                f.write(json.dumps(mal_cache, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            print(f"  MAL characters enriched: {idx}/{len(todo)}", flush=True)
        time.sleep(0.55)

    return mal_cache


def normalize_name_key(name: str) -> str:
    toks = re.findall(r"[a-z0-9]+", (name or "").lower())
    return " ".join(sorted(t for t in toks if len(t) >= 2) or toks)


def main():
    max_chars = int(sys.argv[1]) if len(sys.argv) > 1 else 4500
    mal_limit = int(sys.argv[2]) if len(sys.argv) > 2 else 250

    cids = collect_target_char_ids(max_chars=max_chars)
    al_lore = fetch_anilist_character_details(cids)
    mal_lore = scrape_mal_top_characters(limit_chars=mal_limit) if mal_limit > 0 else {}

    mal_by_norm: dict[str, dict] = {}
    for m in mal_lore.values():
        k = normalize_name_key(m.get("n") or "")
        if k and m.get("bio"):
            mal_by_norm[k] = m

    merged_list = list(al_lore.values())
    merged_list.sort(key=lambda x: int(x.get("fav") or 0), reverse=True)
    enriched_mal_count = 0
    for rec in merged_list:
        nk = normalize_name_key(rec.get("n") or "")
        mal_hit = mal_by_norm.get(nk)
        if mal_hit:
            rec["mal_cid"] = mal_hit["mal_cid"]
            mb = mal_hit["bio"]
            if len(mb) > len(rec.get("bio") or ""):
                rec["bio"] = mb
            enriched_mal_count += 1

    os.makedirs(os.path.dirname(OUT_LORE), exist_ok=True)
    raw = json.dumps(merged_list, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.open(OUT_LORE, "wb", compresslevel=6) as f:
        f.write(raw)
    for mdir in MIRRORS:
        if os.path.isdir(mdir):
            shutil.copy2(OUT_LORE, os.path.join(mdir, os.path.basename(OUT_LORE)))

    kb_size = os.path.getsize(OUT_LORE) / 1024.0
    print(
        f"Saved {len(merged_list)} characters ({enriched_mal_count} MAL-enriched) -> {OUT_LORE} ({kb_size:.1f} KB)",
        flush=True,
    )


if __name__ == "__main__":
    main()
