"""Train high-level Vegeta Neural Anime Character Vision models.

Builds two complementary compact artifacts in ``api/`` (and mirrors to
``anime_app/web/api/`` and ``anime_app/server/``):

1. ``vegeta_char_brain.npz``: Sparse TF-IDF semantic visual trait matrix
   across up to 15,000+ top AniList anime characters, trained by running
   ``SmilingWolf/wd-vit-tagger-v3`` ONNX locally across both full official
   portraits and upper-head crops, enriched with AniList gender, favourites,
   role, and visual description cues.
2. ``vegeta_char_visuals.npz`` + ``vegeta_char_visuals_meta.json.gz``:
   Expanded multi-character 312-dim visual portrait feature database.
"""

import csv
import gzip
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import cv2
import numpy as np
import onnxruntime as ort

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_PATH = os.path.join(ROOT, "api", "vegeta_mal_knowledge.json.gz")
OUT_BRAIN = os.path.join(ROOT, "api", "vegeta_char_brain.npz")
OUT_NPZ = os.path.join(ROOT, "api", "vegeta_char_visuals.npz")
OUT_META = os.path.join(ROOT, "api", "vegeta_char_visuals_meta.json.gz")

MIRRORS = [
    os.path.join(ROOT, "anime_app", "web", "api"),
    os.path.join(ROOT, "anime_app", "server"),
]

SCRATCH_DIR = os.environ.get(
    "VEGETA_SCRATCH_DIR",
    r"C:\Users\Munib Raza\.gemini\antigravity\brain\a91d790b-8804-4c78-b699-10705ba5d729\scratch",
)
INT8_MODEL_PATH = os.path.join(SCRATCH_DIR, "wd_vit_v3_int8.onnx")
MODEL_PATH = INT8_MODEL_PATH if os.path.exists(INT8_MODEL_PATH) else os.path.join(SCRATCH_DIR, "wd_vit_v3.onnx")
TAGS_CSV_PATH = os.path.join(SCRATCH_DIR, "selected_tags.csv")
CHAR_CACHE_PATH = os.path.join(SCRATCH_DIR, "anilist_characters_cache.json.gz")
IMG_CACHE_DIR = os.path.join(SCRATCH_DIR, "char_portraits")
NN_CKPT_PATH = os.path.join(SCRATCH_DIR, "nn_traits_ckpt.npz")

ANILIST_URL = "https://graphql.anilist.co"
QUERY = """
query ($ids: [Int]) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      idMal
      title { romaji english }
      characters(sort: [ROLE, FAVOURITES_DESC], perPage: 10) {
        edges {
          role
          node {
            id
            name { full }
            gender
            favourites
            description(asHtml: false)
            image { medium large }
          }
        }
      }
    }
  }
}
"""

STOP_APPEARANCE_TAGS = {
    "solo", "looking at viewer", "upper body", "portrait", "bust", "simple background",
    "white background", "black background", "grey background", "gradient background",
    "blurry", "blurry background", "depth of field", "sketch", "monochrome", "greyscale",
    "comic", "chibi", "english text", "japanese text", "text", "text focus", "watermark",
    "signature", "speech bubble", "fake screenshot", "letterboxed", "pillarboxed",
    "cropped", "close-up", "from behind", "from side", "profile", "cowboy shot",
    "full body", "standing", "sitting", "outdoors", "indoors", "day", "night",
    "smile", "open mouth", "closed mouth", "blush", "blush stickers", "tongue",
    "tongue out", ":p", ":q", ":d", ":o", ">_<", "^_^", "o_o", "=_=", "tears", "crying",
    "wink", "grin", "smirk", "looking back", "looking away", "looking up", "looking down",
    "1other", "multiple boys", "multiple girls", "2boys", "2girls", "3boys", "4boys",
    "rating:safe", "general", "sensitive", "questionable", "explicit",
    "nipples", "pussy", "penis", "nude", "completely nude", "topless", "bottomless",
    "uncensored", "censored", "sex", "hetero", "yuri", "yaoi", "cum",
}

HAIR_COLOR_TAGS = (
    "pink hair", "black hair", "brown hair", "blonde hair", "blue hair",
    "red hair", "green hair", "white hair", "silver hair", "grey hair",
    "purple hair", "orange hair", "aqua hair",
)

HAIR_BITS = {tag: (1 << idx) for idx, tag in enumerate(HAIR_COLOR_TAGS)}

DESC_TRAIT_PATTERNS = [
    ("scarf", r"\bscarf\b"),
    ("white scarf", r"\bwhite scarf\b"),
    ("red scarf", r"\bred scarf\b"),
    ("pink hair", r"\bpink(?:ish)?(?: spiky)? hair\b|\bsalmon[- ]colored hair\b"),
    ("white hair", r"\bwhite(?: spiky)? hair\b"),
    ("silver hair", r"\bsilver hair\b"),
    ("blonde hair", r"\bblonde? hair\b|\bgolden hair\b"),
    ("black hair", r"\bblack(?: spiky)? hair\b"),
    ("green hair", r"\bgreen hair\b"),
    ("blue hair", r"\bblue hair\b"),
    ("red hair", r"\bred hair\b|\bcrimson hair\b"),
    ("spiked hair", r"\bspiky hair\b|\bspiked hair\b"),
    ("glasses", r"\bglasses\b|\bspectacles\b|\beyeglasses\b"),
    ("blindfold", r"\bblindfold\b"),
    ("eyepatch", r"\beyepatch\b"),
    ("straw hat", r"\bstraw hat\b"),
    ("bandages", r"\bbandages?\b"),
    ("scar", r"\bscar\b"),
    ("fire", r"\bfire\b|\bflames?\b"),
    ("dragon", r"\bdragon\b"),
    ("katana", r"\bkatana\b"),
    ("sword", r"\bsword\b"),
    ("horns", r"\bhorns?\b"),
    ("armor", r"\barmor\b"),
    ("headphones", r"\bheadphones?\b"),
]


def decode_bgr_composited(raw: bytes) -> np.ndarray | None:
    """Decode JPEG/PNG (8-bit or 16-bit) onto white RGB canvas (exact SmilingWolf alpha composite)."""
    try:
        arr = np.frombuffer(raw, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
        if img is None or img.size == 0:
            return None
        if img.dtype == np.uint16:
            img = (img >> 8).astype(np.uint8)
        elif img.dtype != np.uint8:
            img = np.clip(img, 0, 255).astype(np.uint8)
        if img.ndim == 2:
            return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        if img.shape[2] == 4:
            alpha = img[:, :, 3:4].astype(np.float32) / 255.0
            bgr = img[:, :, :3].astype(np.float32)
            comp = bgr * alpha + 255.0 * (1.0 - alpha)
            return np.clip(comp, 0, 255).astype(np.uint8)
        return img[:, :, :3]
    except Exception:
        return None


def prepare_wd_input(img_bgr: np.ndarray, size: int = 448) -> np.ndarray:
    """Pad to white square and resize with bicubic interpolation matching SmilingWolf app.py."""
    h, w = img_bgr.shape[:2]
    m = max(h, w)
    canvas = np.full((m, m, 3), 255, dtype=np.uint8)
    y0 = (m - h) // 2
    x0 = (m - w) // 2
    canvas[y0:y0 + h, x0:x0 + w] = img_bgr
    if m != size:
        canvas = cv2.resize(canvas, (size, size), interpolation=cv2.INTER_CUBIC)
    return canvas.astype(np.float32)


def extract_visual_descriptor(img_bgr: np.ndarray) -> np.ndarray | None:
    """Extract 312-dim L2-normalized portrait visual descriptor compatible with api/index.py."""
    if img_bgr is None or img_bgr.size == 0:
        return None
    h, w = img_bgr.shape[:2]
    if h < 20 or w < 20:
        return None

    resized = cv2.resize(img_bgr, (128, 128), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV)
    hist_parts = []
    h_glob = cv2.calcHist([hsv], [0, 1], None, [8, 3], [0, 180, 0, 256]).flatten()
    h_glob = h_glob / (np.linalg.norm(h_glob) + 1e-6)
    hist_parts.append(h_glob * 1.4)
    for y0 in (0, 64):
        for x0 in (0, 64):
            patch = hsv[y0:y0 + 64, x0:x0 + 64]
            hp = cv2.calcHist([patch], [0, 1], None, [8, 3], [0, 180, 0, 256]).flatten()
            hp = hp / (np.linalg.norm(hp) + 1e-6)
            hist_parts.append(hp)

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
            cell_hist = np.bincount(
                bins[y0:y1, x0:x1].ravel(),
                weights=mag[y0:y1, x0:x1].ravel(),
                minlength=8,
            ).astype(np.float32)
            grad_parts.append(cell_hist)
    grad_vec = np.concatenate(grad_parts)
    grad_vec = grad_vec / (np.linalg.norm(grad_vec) + 1e-6)

    small_gray = cv2.resize(gray, (8, 8), interpolation=cv2.INTER_AREA).flatten()
    small_gray = small_gray - small_gray.mean()
    small_gray = small_gray / (np.linalg.norm(small_gray) + 1e-6)

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
                "User-Agent": "KuroAnime-VegetaVisionTrainer/2.0",
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
            time.sleep(2)
        except Exception:
            time.sleep(2)
    return []


def collect_characters(max_anime: int) -> list[dict]:
    if os.path.exists(CHAR_CACHE_PATH):
        try:
            with gzip.open(CHAR_CACHE_PATH, "rb") as f:
                cached = json.loads(f.read().decode("utf-8"))
            if isinstance(cached, dict) and cached.get("max_anime", 0) >= max_anime and cached.get("chars"):
                print(f"Loaded {len(cached['chars'])} cached AniList characters.", flush=True)
                return cached["chars"]
        except Exception:
            pass

    with gzip.open(KB_PATH, "rb") as f:
        kb = json.loads(f.read().decode("utf-8"))

    anilist_ids = [int(k) for k in list(kb.keys())[:max_anime]]
    print(f"Fetching character metadata & large portraits for top {len(anilist_ids)} anime...", flush=True)

    candidates = []
    seen = set()
    batch_size = 50
    for i in range(0, len(anilist_ids), batch_size):
        batch = anilist_ids[i:i + batch_size]
        media_list = fetch_anilist_batch(batch)
        for m in media_list:
            al_id = m.get("id")
            mal_id = m.get("idMal")
            t_obj = m.get("title") or {}
            title = t_obj.get("english") or t_obj.get("romaji") or ""
            for edge in (m.get("characters") or {}).get("edges") or []:
                node = edge.get("node") or {}
                cid = node.get("id")
                cname = (node.get("name") or {}).get("full") or ""
                img_obj = node.get("image") or {}
                img_url = img_obj.get("large") or img_obj.get("medium") or ""
                if not cid or not cname or not img_url or "default.jpg" in img_url:
                    continue
                if cid in seen:
                    continue
                seen.add(cid)
                candidates.append({
                    "cid": int(cid),
                    "n": cname,
                    "r": (edge.get("role") or "S")[:1],
                    "g": (node.get("gender") or "")[:1],
                    "fav": int(node.get("favourites") or 0),
                    "al": int(al_id) if al_id else None,
                    "mal": int(mal_id) if mal_id else None,
                    "t": title,
                    "ds": (node.get("description") or "")[:600],
                    "img": img_url,
                })
        if (i // batch_size + 1) % 10 == 0 or (i + batch_size) >= len(anilist_ids):
            print(f"  Batch {i // batch_size + 1}/{(len(anilist_ids) + batch_size - 1) // batch_size}: {len(candidates)} chars", flush=True)
        time.sleep(0.72)

    os.makedirs(SCRATCH_DIR, exist_ok=True)
    with gzip.open(CHAR_CACHE_PATH, "wb") as f:
        f.write(json.dumps({"max_anime": max_anime, "chars": candidates}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return candidates


def download_portrait(item: dict) -> tuple[dict, bytes] | None:
    cid = item["cid"]
    cache_file = os.path.join(IMG_CACHE_DIR, f"{cid}.bin")
    if os.path.exists(cache_file) and os.path.getsize(cache_file) > 200:
        try:
            return item, open(cache_file, "rb").read()
        except Exception:
            pass
    url = item.get("img")
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        if len(raw) < 200:
            return None
        os.makedirs(IMG_CACHE_DIR, exist_ok=True)
        with open(cache_file, "wb") as f:
            f.write(raw)
        return item, raw
    except Exception:
        return None


def load_tag_vocabulary() -> tuple[list[str], list[int], dict[str, int]]:
    with open(TAGS_CSV_PATH, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    gen_col_indices = []
    vocab = []
    for idx, r in enumerate(rows):
        if r.get("category") != "0":
            continue
        name = r["name"].replace("_", " ").strip().lower()
        if name in STOP_APPEARANCE_TAGS:
            continue
        gen_col_indices.append(idx)
        vocab.append(name)
    vocab_map = {t: i for i, t in enumerate(vocab)}
    return vocab, gen_col_indices, vocab_map


def estimate_portrait_hair_tag(img_bgr: np.ndarray) -> list[tuple[str, float]]:
    """Fast heuristic upper-head hair color estimation for fallback tier characters."""
    h, w = img_bgr.shape[:2]
    crop = img_bgr[max(0, int(h * 0.05)):max(1, int(h * 0.38)), max(0, int(w * 0.22)):max(1, int(w * 0.78))]
    if crop.size == 0:
        return []
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    # Ignore near-white studio background pixels
    fg = hsv[(hsv[:, :, 2] < 242) | (hsv[:, :, 1] > 28)]
    if fg.shape[0] < 20:
        return []
    med_h, med_s, med_v = np.median(fg, axis=0)
    out = []
    if med_v < 58 and med_s < 85:
        out.append(("black hair", 0.55))
    elif med_s < 32 and med_v > 175:
        out.append(("white hair", 0.45))
        out.append(("silver hair", 0.35))
    elif med_s < 42 and 90 <= med_v <= 175:
        out.append(("grey hair", 0.40))
    elif med_s >= 35:
        if med_h <= 8 or med_h >= 170:
            if med_s < 115 and med_v > 145:
                out.append(("pink hair", 0.48))
            else:
                out.append(("red hair", 0.48))
        elif 9 <= med_h <= 21:
            out.append(("orange hair" if med_v > 120 else "brown hair", 0.46))
        elif 22 <= med_h <= 34:
            out.append(("blonde hair", 0.48))
        elif 35 <= med_h <= 84:
            out.append(("green hair", 0.48))
        elif 85 <= med_h <= 128:
            out.append(("blue hair", 0.48))
        elif 129 <= med_h <= 154:
            out.append(("purple hair", 0.48))
        elif 155 <= med_h <= 169:
            out.append(("pink hair", 0.52))
    return out


def main():
    max_anime = int(sys.argv[1]) if len(sys.argv) > 1 else 2600
    nn_limit = int(sys.argv[2]) if len(sys.argv) > 2 else 1100
    t_start = time.time()

    if not os.path.exists(MODEL_PATH) or not os.path.exists(TAGS_CSV_PATH):
        raise FileNotFoundError(f"Missing ONNX/CSV in {SCRATCH_DIR}: {MODEL_PATH}")

    vocab, gen_cols, vocab_map = load_tag_vocabulary()
    gen_cols_arr = np.asarray(gen_cols, dtype=np.int32)
    print(f"Curated semantic trait vocabulary: {len(vocab)} high-signal appearance tags", flush=True)

    candidates = collect_characters(max_anime)
    print(f"Downloading / loading portraits for {len(candidates)} characters...", flush=True)

    valid_items: list[dict] = []
    imgs_bgr: list[np.ndarray] = []
    desc_vecs: list[np.ndarray] = []

    with ThreadPoolExecutor(max_workers=24) as pool:
        futs = [pool.submit(download_portrait, c) for c in candidates]
        done = 0
        for fut in as_completed(futs):
            done += 1
            res = fut.result()
            if res is not None:
                try:
                    item, raw = res
                    bgr = decode_bgr_composited(raw)
                    if bgr is not None:
                        dv = extract_visual_descriptor(bgr)
                        if dv is not None:
                            valid_items.append(item)
                            imgs_bgr.append(bgr)
                            desc_vecs.append(dv)
                except Exception:
                    pass
            if done % 2000 == 0 or done == len(candidates):
                print(f"  Portraits ready: {len(valid_items)}/{done}", flush=True)

    # Sort deterministically by AniList favourites desc so popular heroes get full ViT extraction
    order = sorted(range(len(valid_items)), key=lambda i: (valid_items[i]["fav"], 1 if valid_items[i].get("r") == "M" else 0), reverse=True)
    valid_items = [valid_items[i] for i in order]
    imgs_bgr = [imgs_bgr[i] for i in order]
    desc_vecs = [desc_vecs[i] for i in order]
    n_chars = len(valid_items)
    n_nn = min(n_chars, max(0, nn_limit))
    print(
        f"Running local ONNX WD-ViT-v3 neural extraction ({os.path.basename(MODEL_PATH)}) on top {n_nn}/{n_chars} iconic characters...",
        flush=True,
    )

    # Load resumable checkpoint map keyed by character cid -> (indices, values)
    nn_ckpt: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    if os.path.exists(NN_CKPT_PATH):
        try:
            with np.load(NN_CKPT_PATH, allow_pickle=False) as ck:
                cids_ck = ck["cids"]
                ip_ck = ck["indptr"]
                ix_ck = ck["indices"]
                dt_ck = ck["data"]
                for idx_ck, cid_val in enumerate(cids_ck):
                    s0, s1 = int(ip_ck[idx_ck]), int(ip_ck[idx_ck + 1])
                    nn_ckpt[int(cid_val)] = (np.array(ix_ck[s0:s1]), np.array(dt_ck[s0:s1]))
            print(f"  Resumed {len(nn_ckpt)} cached neural vectors from checkpoint.", flush=True)
        except Exception:
            nn_ckpt = {}

    todo_nn_indices = [i for i in range(n_nn) if valid_items[i]["cid"] not in nn_ckpt]
    if todo_nn_indices:
        t_nn_start = time.time()
        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = 6
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess = ort.InferenceSession(MODEL_PATH, sess_options=sess_opts, providers=["CPUExecutionProvider"])
        in_name = sess.get_inputs()[0].name

        batch_size = 8
        for b0 in range(0, len(todo_nn_indices), batch_size):
            b_idxs = todo_nn_indices[b0:b0 + batch_size]
            batch_arr = np.stack([prepare_wd_input(imgs_bgr[k]) for k in b_idxs], axis=0)
            probs = sess.run(None, {in_name: batch_arr})[0][:, gen_cols_arr]
            for j, ci in enumerate(b_idxs):
                pvec = probs[j]
                keep = np.where(pvec >= 0.06)[0]
                if keep.size > 64:
                    keep = keep[np.argsort(pvec[keep])[::-1][:64]]
                nn_ckpt[valid_items[ci]["cid"]] = (keep.astype(np.uint16), pvec[keep].astype(np.float16))

            done_nn = min(len(todo_nn_indices), b0 + batch_size)
            if (b0 // batch_size + 1) % 12 == 0 or done_nn == len(todo_nn_indices):
                elapsed = time.time() - t_nn_start
                rate = done_nn / max(elapsed, 0.1)
                eta = (len(todo_nn_indices) - done_nn) / max(rate, 0.1)
                print(f"  Neural ViT progress: {done_nn}/{len(todo_nn_indices)} ({rate:.2f} img/s, ETA {eta/60:.1f}m)", flush=True)
                # Save incremental checkpoint safely
                ck_cids = np.asarray(list(nn_ckpt.keys()), dtype=np.int32)
                ck_ip = np.zeros(len(ck_cids) + 1, dtype=np.int32)
                ck_ixs = []
                ck_dts = []
                for idx_c, ck_id in enumerate(ck_cids):
                    ix, dt = nn_ckpt[int(ck_id)]
                    ck_ip[idx_c + 1] = ck_ip[idx_c] + len(ix)
                    ck_ixs.append(ix)
                    ck_dts.append(dt)
                tmp_ckpt = NN_CKPT_PATH + ".tmp.npz"
                with open(tmp_ckpt, "wb") as _cf:
                    np.savez_compressed(
                        _cf,
                        cids=ck_cids,
                        indptr=ck_ip,
                        indices=np.concatenate(ck_ixs) if ck_ixs else np.zeros(0, dtype=np.uint16),
                        data=np.concatenate(ck_dts) if ck_dts else np.zeros(0, dtype=np.float16),
                    )
                os.replace(tmp_ckpt, NN_CKPT_PATH)

    # Assemble complete multi-modal appearance vectors across all n_chars characters
    top_k = 56
    char_top_idx: list[np.ndarray] = []
    char_top_val: list[np.ndarray] = []
    df = np.zeros(len(vocab), dtype=np.float32)
    gender_arr = np.zeros(n_chars, dtype=np.int8)
    hair_mask_arr = np.zeros(n_chars, dtype=np.uint32)
    fav_arr = np.zeros(n_chars, dtype=np.int32)
    role_arr = np.zeros(n_chars, dtype=np.uint8)

    idx_boy = vocab_map.get("1boy", -1)
    idx_mfocus = vocab_map.get("male focus", -1)
    idx_girl = vocab_map.get("1girl", -1)
    idx_ffocus = vocab_map.get("female focus", -1)

    for ci in range(n_chars):
        item = valid_items[ci]
        vec = np.zeros(len(vocab), dtype=np.float32)

        if item["cid"] in nn_ckpt:
            ix, dt = nn_ckpt[item["cid"]]
            vec[ix] = dt.astype(np.float32)
        else:
            for htag, hprob in estimate_portrait_hair_tag(imgs_bgr[ci]):
                hi = vocab_map.get(htag, -1)
                if hi >= 0:
                    vec[hi] = max(vec[hi], hprob)

        g = item.get("g") or ""
        if g == "M":
            gender_arr[ci] = 1
            if idx_boy >= 0:
                vec[idx_boy] = max(vec[idx_boy], 0.90)
            if idx_mfocus >= 0:
                vec[idx_mfocus] = max(vec[idx_mfocus], 0.85)
        elif g == "F":
            gender_arr[ci] = 2
            if idx_girl >= 0:
                vec[idx_girl] = max(vec[idx_girl], 0.90)
            if idx_ffocus >= 0:
                vec[idx_ffocus] = max(vec[idx_ffocus], 0.85)

        ds_low = (item.get("ds") or "").lower()
        if ds_low:
            for tag_name, pat in DESC_TRAIT_PATTERNS:
                ti = vocab_map.get(tag_name, -1)
                if ti >= 0 and re.search(pat, ds_low):
                    vec[ti] = max(vec[ti], 0.62)

        hmask = 0
        for htag, hbit in HAIR_BITS.items():
            hi = vocab_map.get(htag, -1)
            if hi >= 0 and vec[hi] >= 0.22:
                hmask |= hbit
        hair_mask_arr[ci] = hmask
        fav_arr[ci] = int(item.get("fav") or 0)
        role_arr[ci] = 1 if item.get("r") == "M" else 0

        keep = np.where(vec >= 0.08)[0]
        if keep.size > top_k:
            sub = keep[np.argsort(vec[keep])[::-1][:top_k]]
        else:
            sub = keep
        df[sub] += 1.0
        char_top_idx.append(sub.astype(np.uint16))
        char_top_val.append(vec[sub].astype(np.float16))

    # Compute smooth corpus IDF and L2-normalize each character's TF-IDF vector
    idf = np.log(1.0 + n_chars / (df + 3.0)).astype(np.float32)
    indptr = np.zeros(n_chars + 1, dtype=np.int32)
    indices_list = []
    data_list = []
    for ci in range(n_chars):
        idx = char_top_idx[ci]
        w = char_top_val[ci].astype(np.float32) * idf[idx]
        norm = np.linalg.norm(w) + 1e-6
        w = (w / norm).astype(np.float16)
        indptr[ci + 1] = indptr[ci] + len(idx)
        indices_list.append(idx)
        data_list.append(w)

    all_indices = np.concatenate(indices_list).astype(np.uint16)
    all_data = np.concatenate(data_list).astype(np.float16)

    np.savez_compressed(
        OUT_BRAIN,
        indptr=indptr,
        indices=all_indices,
        data=all_data,
        idf=idf,
        vocab=np.asarray(vocab),
        gender=gender_arr,
        hair_mask=hair_mask_arr,
        favourites=fav_arr,
        role=role_arr,
    )

    matrix = np.vstack(desc_vecs).astype(np.float16)
    np.savez_compressed(OUT_NPZ, features=matrix)

    meta_out = [
        {"n": it["n"], "r": it["r"], "al": it["al"], "mal": it["mal"], "t": it["t"], "g": it["g"], "fav": it["fav"]}
        for it in valid_items
    ]
    with gzip.open(OUT_META, "wb") as f:
        f.write(json.dumps(meta_out, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    for mirror_dir in MIRRORS:
        if os.path.isdir(mirror_dir):
            for p in (OUT_BRAIN, OUT_NPZ, OUT_META):
                shutil.copy2(p, os.path.join(mirror_dir, os.path.basename(p)))

    print(
        f"Training complete in {time.time() - t_start:.1f}s! "
        f"Characters={n_chars:,} | brain={os.path.getsize(OUT_BRAIN)/1e6:.2f} MB | "
        f"visuals={os.path.getsize(OUT_NPZ)/1e6:.2f} MB",
        flush=True,
    )


if __name__ == "__main__":
    main()
