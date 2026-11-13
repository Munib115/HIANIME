"""
Build a compact visual feature database of anime characters from AniList/MyAnimeList
using OpenCV + NumPy (no PyTorch required).

Fetches character portrait URLs via AniList GraphQL for the top popular anime
in vegeta_mal_knowledge.json.gz, downloads portraits concurrently, extracts
L2-normalized visual descriptors (color + spatial structure + gradient),
and saves them to api/vegeta_char_visuals.npz + api/vegeta_char_visuals_meta.json.gz
(and mirrors to anime_app/web/api/ and anime_app/server/).
"""

import gzip
import json
import os
import shutil
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_PATH = os.path.join(ROOT, "api", "vegeta_mal_knowledge.json.gz")
OUT_NPZ = os.path.join(ROOT, "api", "vegeta_char_visuals.npz")
OUT_META = os.path.join(ROOT, "api", "vegeta_char_visuals_meta.json.gz")

MIRRORS = [
    os.path.join(ROOT, "anime_app", "web", "api"),
    os.path.join(ROOT, "anime_app", "server"),
]

ANILIST_URL = "https://graphql.anilist.co"
QUERY = """
query ($ids: [Int]) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      idMal
      title { romaji english }
      characters(sort: [ROLE, FAVOURITES_DESC], perPage: 8) {
        edges {
          role
          node {
            id
            name { full }
            image { medium large }
          }
        }
      }
    }
  }
}
"""


def extract_visual_descriptor(img_bgr: np.ndarray) -> np.ndarray | None:
    """Extract a compact 256-dim L2-normalized visual descriptor (spatial HSV + HOG-like gradients + grayscale layout)."""
    if img_bgr is None or img_bgr.size == 0:
        return None
    h, w = img_bgr.shape[:2]
    if h < 20 or w < 20:
        return None

    # Standardize to 128x128
    resized = cv2.resize(img_bgr, (128, 128), interpolation=cv2.INTER_AREA)

    # 1. Global + 2x2 Spatial HSV Color Histogram (5 regions x 24 bins = 120 dims)
    hsv = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV)
    hist_parts = []
    # Global
    h_glob = cv2.calcHist([hsv], [0, 1], None, [8, 3], [0, 180, 0, 256]).flatten()
    h_glob = h_glob / (np.linalg.norm(h_glob) + 1e-6)
    hist_parts.append(h_glob * 1.4)
    # 2x2 quadrants
    for y0 in (0, 64):
        for x0 in (0, 64):
            patch = hsv[y0:y0 + 64, x0:x0 + 64]
            hp = cv2.calcHist([patch], [0, 1], None, [8, 3], [0, 180, 0, 256]).flatten()
            hp = hp / (np.linalg.norm(hp) + 1e-6)
            hist_parts.append(hp)

    # 2. 4x4 Spatial Gradient Orientation Histogram (16 cells x 8 bins = 128 dims)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.sqrt(gx * gx + gy * gy)
    ang = (np.arctan2(gy, gx) + np.pi) * (8.0 / (2.0 * np.pi))
    bins = np.clip(ang.astype(np.int32), 0, 7)

    grad_parts = []
    for cy in range(4):
        for cx in range(4):
            y0, y1 = cy * 32, (cy + 1) * 32
            x0, x1 = cx * 32, (cx + 1) * 32
            b_cell = bins[y0:y1, x0:x1].ravel()
            m_cell = mag[y0:y1, x0:x1].ravel()
            cell_hist = np.bincount(b_cell, weights=m_cell, minlength=8).astype(np.float32)
            grad_parts.append(cell_hist)
    grad_vec = np.concatenate(grad_parts)
    grad_vec = grad_vec / (np.linalg.norm(grad_vec) + 1e-6)

    # 3. 8x8 Low-freq grayscale layout (64 dims)
    small_gray = cv2.resize(gray, (8, 8), interpolation=cv2.INTER_AREA).flatten()
    small_gray = small_gray - small_gray.mean()
    small_gray = small_gray / (np.linalg.norm(small_gray) + 1e-6)

    # Combine with balanced weights: color (120) + structure (128) + layout (64) = 312 dims
    color_vec = np.concatenate(hist_parts).astype(np.float32)
    color_vec = color_vec / (np.linalg.norm(color_vec) + 1e-6)

    full = np.concatenate([color_vec * 1.1, grad_vec * 1.0, small_gray * 0.7]).astype(np.float32)
    full = full / (np.linalg.norm(full) + 1e-6)
    return full.astype(np.float16)


def fetch_anilist_batch(ids: list[int]) -> list[dict]:
    payload = json.dumps({"query": QUERY, "variables": {"ids": ids}}).encode("utf-8")
    for attempt in range(4):
        req = urllib.request.Request(
            ANILIST_URL,
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
                return (data.get("data") or {}).get("Page", {}).get("media") or []
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "15")) + 2
                print(f"  Rate limited (429), sleeping {wait}s...", flush=True)
                time.sleep(wait)
                continue
            print(f"  HTTP {e.code} on batch attempt {attempt + 1}", flush=True)
            time.sleep(2)
        except Exception as e:
            print(f"  Error on batch attempt {attempt + 1}: {e}", flush=True)
            time.sleep(2)
    return []


def download_and_extract(item: dict) -> tuple[dict, np.ndarray] | None:
    url = item.get("img")
    if not url or "default.jpg" in url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        arr = np.frombuffer(raw, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        vec = extract_visual_descriptor(img)
        if vec is None:
            return None
        return item, vec
    except Exception:
        return None


def main():
    max_anime = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
    print(f"Loading KB from {KB_PATH}...", flush=True)
    with gzip.open(KB_PATH, "rb") as f:
        kb = json.loads(f.read().decode("utf-8"))

    anilist_ids = [int(k) for k in list(kb.keys())[:max_anime]]
    print(f"Fetching character portrait URLs for top {len(anilist_ids)} anime from AniList...", flush=True)

    candidates = []
    seen_char_ids = set()
    batch_size = 50
    for i in range(0, len(anilist_ids), batch_size):
        batch = anilist_ids[i:i + batch_size]
        media_list = fetch_anilist_batch(batch)
        for m in media_list:
            al_id = m.get("id")
            mal_id = m.get("idMal")
            title_obj = m.get("title") or {}
            title = title_obj.get("english") or title_obj.get("romaji") or ""
            edges = (m.get("characters") or {}).get("edges") or []
            for edge in edges:
                node = edge.get("node") or {}
                cid = node.get("id")
                cname = (node.get("name") or {}).get("full") or ""
                img_obj = node.get("image") or {}
                img_url = img_obj.get("medium") or img_obj.get("large") or ""
                if not cid or not cname or not img_url or "default.jpg" in img_url:
                    continue
                if cid in seen_char_ids:
                    continue
                seen_char_ids.add(cid)
                candidates.append({
                    "cid": cid,
                    "name": cname,
                    "role": (edge.get("role") or "S")[:1],
                    "al": al_id,
                    "mal": mal_id,
                    "anime": title,
                    "img": img_url,
                })
        print(f"  Batch {i // batch_size + 1}/{(len(anilist_ids) + batch_size - 1) // batch_size}: {len(candidates)} unique characters queued", flush=True)
        time.sleep(0.75)  # stay comfortably under 90 req/min

    print(f"Downloading & extracting visual descriptors for {len(candidates)} character portraits...", flush=True)
    meta_list = []
    vecs = []
    done = 0
    with ThreadPoolExecutor(max_workers=24) as pool:
        futures = [pool.submit(download_and_extract, c) for c in candidates]
        for fut in as_completed(futures):
            done += 1
            res = fut.result()
            if res is not None:
                item, vec = res
                # Drop img URL from compact runtime metadata to save space
                meta_list.append({
                    "n": item["name"],
                    "r": item["role"],
                    "al": item["al"],
                    "mal": item["mal"],
                    "t": item["anime"],
                })
                vecs.append(vec)
            if done % 1000 == 0 or done == len(candidates):
                print(f"  Processed {done}/{len(candidates)} ({len(vecs)} valid)", flush=True)

    if not vecs:
        print("ERROR: No vectors extracted!", file=sys.stderr)
        sys.exit(1)

    matrix = np.vstack(vecs).astype(np.float16)
    np.savez_compressed(OUT_NPZ, features=matrix)
    with gzip.open(OUT_META, "wb") as f:
        f.write(json.dumps(meta_list, separators=(",", ":")).encode("utf-8"))

    npz_mb = os.path.getsize(OUT_NPZ) / (1024 * 1024)
    meta_kb = os.path.getsize(OUT_META) / 1024
    print(f"Saved {matrix.shape} to {OUT_NPZ} ({npz_mb:.2f} MB) + {OUT_META} ({meta_kb:.1f} KB)", flush=True)

    for mirror_dir in MIRRORS:
        if os.path.isdir(mirror_dir):
            shutil.copy2(OUT_NPZ, os.path.join(mirror_dir, os.path.basename(OUT_NPZ)))
            shutil.copy2(OUT_META, os.path.join(mirror_dir, os.path.basename(OUT_META)))
            print(f"  Mirrored to {mirror_dir}", flush=True)


if __name__ == "__main__":
    main()
