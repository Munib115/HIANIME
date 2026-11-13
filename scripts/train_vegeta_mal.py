"""
Train Vegeta Bot with MyAnimeList / AniList metadata for ALL anime across both
Server 1 (hianime.db) and Server 2 (munib_server.db).

Fetches in batches of 50 via AniList GraphQL (which includes idMal, tags,
synonyms, studios, descriptions, and top characters + aliases) and saves to
api/vegeta_mal_knowledge.json.gz (and mirrors).
"""
import gzip
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIANIME_DB = os.path.join(ROOT, "hianime.db")
MUNIB_DB = os.path.join(ROOT, "munib_server.db")
OUT_PATHS = [
    os.path.join(ROOT, "api", "vegeta_mal_knowledge.json.gz"),
    os.path.join(ROOT, "anime_app", "web", "api", "vegeta_mal_knowledge.json.gz"),
    os.path.join(ROOT, "anime_app", "server", "vegeta_mal_knowledge.json.gz"),
]

GRAPHQL_QUERY = """
query ($ids: [Int], $page: Int) {
  Page(page: $page, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      idMal
      title {
        romaji
        english
        native
      }
      synonyms
      description(asHtml: false)
      studios(isMain: true) {
        nodes {
          name
        }
      }
      tags {
        name
        rank
        isMediaSpoiler
      }
      characters(sort: [ROLE, FAVOURITES_DESC], perPage: 8) {
        edges {
          role
          node {
            name {
              full
              native
              alternative
            }
          }
        }
      }
    }
  }
}
"""


def clean_desc(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\(Source:.*?\)", "", text, flags=re.I | re.S)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:900]


def collect_all_anilist_ids() -> list[int]:
    ids_with_pop = {}

    if os.path.exists(HIANIME_DB):
        conn = sqlite3.connect(HIANIME_DB)
        for al_id, pop in conn.execute(
            "SELECT anilist_id, COALESCE(anilist_popularity, 0) FROM anime WHERE anilist_id IS NOT NULL AND anilist_id > 0"
        ):
            ids_with_pop[int(al_id)] = max(ids_with_pop.get(int(al_id), 0), int(pop or 0))
        conn.close()

    if os.path.exists(MUNIB_DB):
        conn = sqlite3.connect(MUNIB_DB)
        for al_id, pop in conn.execute(
            "SELECT anilist_id, COALESCE(anilist_popularity, 0) FROM anime WHERE anilist_id IS NOT NULL AND anilist_id > 0"
        ):
            ids_with_pop[int(al_id)] = max(ids_with_pop.get(int(al_id), 0), int(pop or 0))
        conn.close()

    # Sort highest popularity first so top anime are always enriched first
    sorted_ids = [k for k, _ in sorted(ids_with_pop.items(), key=lambda x: -x[1])]
    return sorted_ids


def fetch_batch(batch_ids: list[int]) -> list[dict]:
    payload = json.dumps({
        "query": GRAPHQL_QUERY,
        "variables": {"ids": batch_ids, "page": 1},
    }).encode("utf-8")

    for attempt in range(5):
        req = urllib.request.Request(
            "https://graphql.anilist.co",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "KuroAnime-VegetaTrainer/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("data", {}).get("Page", {}).get("media", []) or []
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After") or 15)
                print(f"  [Rate limit 429] Sleeping {retry_after}s...", flush=True)
                time.sleep(retry_after + 1)
            else:
                print(f"  [HTTP {e.code}] attempt {attempt + 1}/5", flush=True)
                time.sleep(2 * (attempt + 1))
        except Exception as e:
            print(f"  [Error] {e} attempt {attempt + 1}/5", flush=True)
            time.sleep(2 * (attempt + 1))
    return []


def save_knowledge(knowledge: dict):
    os.makedirs(os.path.dirname(OUT_PATHS[0]), exist_ok=True)
    raw = json.dumps(knowledge, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    for p in OUT_PATHS:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with gzip.open(p, "wb", compresslevel=6) as f:
            f.write(raw)


def main():
    max_limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    knowledge = {}
    if os.path.exists(OUT_PATHS[0]):
        try:
            with gzip.open(OUT_PATHS[0], "rb") as f:
                knowledge = json.loads(f.read().decode("utf-8"))
            print(f"Loaded existing knowledge cache: {len(knowledge)} entries", flush=True)
        except Exception:
            knowledge = {}

    all_ids = collect_all_anilist_ids()
    if max_limit > 0:
        all_ids = all_ids[:max_limit]

    todo = [aid for aid in all_ids if str(aid) not in knowledge]
    print(f"Total unique AniList IDs across both servers: {len(all_ids)} | Remaining to fetch: {len(todo)}", flush=True)

    batch_size = 50
    total_batches = (len(todo) + batch_size - 1) // batch_size

    for i in range(0, len(todo), batch_size):
        batch = todo[i : i + batch_size]
        batch_num = (i // batch_size) + 1
        media_list = fetch_batch(batch)
        for m in media_list:
            aid = str(m["id"])
            title_obj = m.get("title") or {}
            studios = [s["name"] for s in (m.get("studios") or {}).get("nodes", []) if s.get("name")]
            tags = [
                t["name"]
                for t in (m.get("tags") or [])
                if t.get("name") and not t.get("isMediaSpoiler") and (t.get("rank") or 0) >= 50
            ][:12]
            chars = []
            for edge in (m.get("characters") or {}).get("edges", []):
                node = edge.get("node") or {}
                name_obj = node.get("name") or {}
                full = (name_obj.get("full") or "").strip()
                if not full:
                    continue
                alts = [a.strip() for a in (name_obj.get("alternative") or []) if a and a.strip()][:3]
                chars.append({
                    "n": full,
                    "r": "M" if edge.get("role") == "MAIN" else "S",
                    "a": alts,
                })

            syns = [s.strip() for s in (m.get("synonyms") or []) if s and s.strip()][:6]
            entry = {
                "mal": m.get("idMal"),
                "en": title_obj.get("english") or "",
                "ro": title_obj.get("romaji") or "",
                "syn": syns,
                "st": studios[:3],
                "tg": tags,
                "ch": chars,
                "ds": clean_desc(m.get("description") or ""),
            }
            knowledge[aid] = entry

        # Mark IDs that weren't returned so we don't re-fetch dead IDs
        returned = {str(m["id"]) for m in media_list}
        for aid in batch:
            if str(aid) not in returned and str(aid) not in knowledge:
                knowledge[str(aid)] = {"mal": None, "ch": [], "tg": [], "syn": [], "st": [], "ds": ""}

        if batch_num % 10 == 0 or batch_num == total_batches:
            save_knowledge(knowledge)
            print(f"Batch {batch_num}/{total_batches} saved ({len(knowledge)} total entries)", flush=True)

        # Gentle pacing to stay well under AniList rate limit (~45 req/min)
        time.sleep(1.3)

    save_knowledge(knowledge)
    print(f"Done! Saved {len(knowledge)} enriched MAL/AniList entries to {OUT_PATHS[0]}", flush=True)


if __name__ == "__main__":
    main()
