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
import sqlite3
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
HIANIME_DB = os.path.join(ROOT, "hianime.db")
MUNIB_DB = os.path.join(ROOT, "munib_server.db")
OUT_BRAIN = os.path.join(ROOT, "api", "vegeta_char_brain.npz")
OUT_NPZ = os.path.join(ROOT, "api", "vegeta_char_visuals.npz")
OUT_META = os.path.join(ROOT, "api", "vegeta_char_visuals_meta.json.gz")
OUT_POSTER_NPZ = os.path.join(ROOT, "api", "vegeta_poster_visuals.npz")
OUT_POSTER_META = os.path.join(ROOT, "api", "vegeta_poster_meta.json.gz")

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
DANBOORU_TAGS_PATH = os.path.join(SCRATCH_DIR, "danbooru_char_tags.jsonl.gz")
IMG_CACHE_DIR = os.path.join(SCRATCH_DIR, "char_portraits")
POSTER_CACHE_DIR = os.path.join(SCRATCH_DIR, "anime_posters")
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
    ("pink hair", r"\bpink(?:ish)?(?:\s+spiky|\s+long|\s+short)?\s+hair\b|\bsalmon[- ]colored hair\b"),
    ("white hair", r"\bwhite(?:\s+spiky|\s+long|\s+short)?\s+hair\b|\bsnow[- ]white hair\b"),
    ("silver hair", r"\bsilver(?:[- ]colored)?(?:\s+long|\s+short)?\s+hair\b"),
    ("grey hair", r"\bgr[ae]y(?:\s+long|\s+short)?\s+hair\b"),
    ("blonde hair", r"\bblond[ea]?(?:\s+spiky|\s+long|\s+short)?\s+hair\b|\bgolden hair\b"),
    ("black hair", r"\bblack(?:\s+spiky|\s+long|\s+short)?\s+hair\b|\bjet[- ]black hair\b|\braven hair\b"),
    ("brown hair", r"\bbrown(?:\s+long|\s+short)?\s+hair\b|\bbrunette\b|\bchestnut hair\b"),
    ("green hair", r"\bgreen(?:\s+long|\s+short)?\s+hair\b|\bemerald hair\b"),
    ("blue hair", r"\bblue(?:\s+long|\s+short)?\s+hair\b|\bcyan hair\b|\baqua hair\b"),
    ("red hair", r"\bred(?:\s+spiky|\s+long|\s+short)?\s+hair\b|\bcrimson hair\b|\bscarlet hair\b"),
    ("orange hair", r"\borange hair\b|\bginger hair\b"),
    ("purple hair", r"\bpurple hair\b|\bviolet hair\b|\blavender hair\b"),
    ("long hair", r"\blong hair\b|\bwaist-length hair\b"),
    ("short hair", r"\bshort hair\b"),
    ("spiked hair", r"\bspiky hair\b|\bspiked hair\b"),
    ("ponytail", r"\bponytail\b"),
    ("twintails", r"\btwintails?\b|\btwin[- ]tails?\b|\bpigtails?\b"),
    ("hair ribbon", r"\bribbon\b"),
    ("braid", r"\bbraid(?:s|ed)?\b"),
    ("ahoge", r"\bahoge\b|\bcowlick\b"),
    ("bald", r"\bbald\b|\bshaved head\b"),
    ("blue eyes", r"\bblue eyes\b"),
    ("red eyes", r"\bred eyes\b|\bcrimson eyes\b|\bscarlet eyes\b"),
    ("green eyes", r"\bgreen eyes\b|\bemerald eyes\b"),
    ("yellow eyes", r"\bgold(?:en)? eyes\b|\byellow eyes\b|\bamber eyes\b"),
    ("purple eyes", r"\bpurple eyes\b|\bviolet eyes\b"),
    ("brown eyes", r"\bbrown eyes\b"),
    ("black eyes", r"\bblack eyes\b|\bdark eyes\b"),
    ("pink eyes", r"\bpink eyes\b"),
    ("heterochromia", r"\bheterochromia\b|\bmismatched eyes\b|\btwo different colored eyes\b"),
    ("glasses", r"\bglasses\b|\bspectacles\b|\beyeglasses\b"),
    ("goggles", r"\bgoggles\b"),
    ("blindfold", r"\bblindfold\b"),
    ("eyepatch", r"\beyepatch\b|\beye patch\b"),
    ("mask", r"\bmask\b"),
    ("straw hat", r"\bstraw hat\b"),
    ("hat", r"\bhat\b|\bcap\b|\bberet\b"),
    ("headband", r"\bheadband\b"),
    ("bandana", r"\bbandana\b"),
    ("earrings", r"\bearrings?\b"),
    ("choker", r"\bchoker\b"),
    ("bandages", r"\bbandages?\b"),
    ("scar", r"\bscar(?:red|s)?\b"),
    ("tattoo", r"\btattoo(?:s|ed)?\b"),
    ("facial mark", r"\bwhiskers?\b|\bmarkings?\b|\bcurse mark\b"),
    ("beard", r"\bbeard\b|\bgoatee\b"),
    ("mustache", r"\bmustache\b|\bmoustache\b"),
    ("fangs", r"\bfangs?\b"),
    ("pointy ears", r"\belf\b|\belven\b|\bpointed ears\b|\bpointy ears\b"),
    ("animal ears", r"\bcat ears?\b|\bfox ears?\b|\bwolf ears?\b|\bdog ears?\b|\brabbit ears?\b|\bbunny ears?\b"),
    ("tail", r"\btail\b"),
    ("horns", r"\bhorns?\b"),
    ("wings", r"\bwings?\b"),
    ("halo", r"\bhalo\b"),
    ("kimono", r"\bkimono\b|\byukata\b|\bhakama\b"),
    ("school uniform", r"\bschool uniform\b|\bsailor uniform\b"),
    ("suit", r"\bsuit\b|\btuxedo\b"),
    ("necktie", r"\bnecktie\b|\btie\b"),
    ("jacket", r"\bjacket\b|\bhoodie\b|\bblazer\b"),
    ("cloak", r"\bcloak\b|\bmantle\b"),
    ("cape", r"\bcape\b"),
    ("coat", r"\bcoat\b|\btrench coat\b"),
    ("armor", r"\barmor\b|\barmour\b"),
    ("gloves", r"\bgloves\b"),
    ("headphones", r"\bheadphones?\b"),
    ("cigarette", r"\bcigarette\b|\bsmoking\b|\bcigar\b"),
    ("katana", r"\bkatana\b|\bsamurai\b"),
    ("sword", r"\bsword\b|\bblade\b"),
    ("scythe", r"\bscythe\b"),
    ("spear", r"\bspear\b|\blance\b"),
    ("gun", r"\bgun\b|\bpistol\b|\brifle\b"),
    ("guitar", r"\bguitar\b"),
    ("fire", r"\bfire\b|\bflames?\b"),
    ("dragon", r"\bdragon\b"),
    ("demon", r"\bdemon\b|\bdevil\b"),
    ("muscular", r"\bmuscular\b|\bbuff\b"),
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


def _norm_char_key(name: str, fold: bool = False) -> str:
    k = re.sub(r"[^a-z0-9\s]", " ", str(name or "").lower()).strip()
    k = re.sub(r"\s+", " ", k)
    if fold:
        k = k.replace("ou", "o").replace("uu", "u")
    return k


def load_danbooru_char_tags() -> dict[str, list[str]]:
    """Load 98k+ Danbooru character tag profiles indexed by exact, swapped, and macron-folded keys."""
    if not os.path.exists(DANBOORU_TAGS_PATH):
        return {}
    tmap: dict[str, list[str]] = {}
    with gzip.open(DANBOORU_TAGS_PATH, "rt", encoding="utf-8") as f:
        for ln in f:
            try:
                obj = json.loads(ln)
            except Exception:
                continue
            raw_char = str(obj.get("character") or "").replace("_", " ").strip()
            raw_desc = str(obj.get("description") or "").strip()
            if not raw_char or not raw_desc:
                continue
            tags = [t.strip().lower().replace("_", " ") for t in raw_desc.split(",") if t.strip()]
            if not tags:
                continue
            base_char = re.sub(r"\s*\([^()]*\)\s*$", "", raw_char).strip()
            for candidate in {raw_char, base_char}:
                for fold in (False, True):
                    k = _norm_char_key(candidate, fold=fold)
                    if k and k not in tmap:
                        tmap[k] = tags
                    parts = k.split()
                    if len(parts) == 2:
                        swapped = f"{parts[1]} {parts[0]}"
                        if swapped not in tmap:
                            tmap[swapped] = tags
    print(f"Loaded {len(tmap):,} Danbooru character appearance keys.", flush=True)
    return tmap


def _match_danbooru_tags(char_name: str, anime_title: str, db_tag_map: dict[str, list[str]]) -> list[str]:
    if not db_tag_map or not char_name:
        return []
    t_clean = _norm_char_key(anime_title or "")
    first_word = t_clean.split()[0] if t_clean else ""
    keys = []
    if first_word:
        keys.append(_norm_char_key(f"{char_name} {first_word}", fold=False))
        keys.append(_norm_char_key(f"{char_name} {first_word}", fold=True))
    keys.append(_norm_char_key(char_name, fold=False))
    keys.append(_norm_char_key(char_name, fold=True))
    for k in keys:
        if k in db_tag_map:
            return db_tag_map[k]
    return []


def collect_target_anilist_ids(max_anime: int) -> list[int]:
    """Collect top popularity + recent 2022-2026 AniList IDs across DBs + MAL knowledge."""
    al_scores: dict[int, int] = {}
    for db_path in (HIANIME_DB, MUNIB_DB):
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            for al_id, pop, yr in conn.execute(
                "SELECT anilist_id, COALESCE(anilist_popularity, 0), COALESCE(season_year, 0) FROM anime WHERE anilist_id > 0"
            ):
                aid = int(al_id)
                boost = int(pop or 0) + (60000 if int(yr or 0) >= 2023 else 0)
                if boost > al_scores.get(aid, -1):
                    al_scores[aid] = boost
            conn.close()
    if os.path.exists(KB_PATH):
        with gzip.open(KB_PATH, "rb") as f:
            kb = json.loads(f.read().decode("utf-8"))
        for idx, k in enumerate(kb.keys()):
            aid = int(k)
            if aid not in al_scores:
                al_scores[aid] = max(0, 50000 - idx * 5)
    ranked = [aid for aid, _ in sorted(al_scores.items(), key=lambda kv: -kv[1])]
    return ranked[:max_anime]


def collect_characters(max_anime: int) -> list[dict]:
    cached_chars: list[dict] = []
    seen_cid: set[int] = set()
    covered_al: set[int] = set()

    if os.path.exists(CHAR_CACHE_PATH):
        try:
            with gzip.open(CHAR_CACHE_PATH, "rb") as f:
                cached = json.loads(f.read().decode("utf-8"))
            if isinstance(cached, dict) and cached.get("chars"):
                cached_chars = cached["chars"]
                for c in cached_chars:
                    if c.get("cid"):
                        seen_cid.add(int(c["cid"]))
                    if c.get("al"):
                        covered_al.add(int(c["al"]))
                print(f"Loaded {len(cached_chars)} cached AniList characters across {len(covered_al)} anime.", flush=True)
        except Exception:
            cached_chars = []

    target_ids = collect_target_anilist_ids(max_anime)
    missing_ids = [aid for aid in target_ids if aid not in covered_al]
    if not missing_ids and cached_chars:
        return cached_chars

    print(f"Fetching missing AniList characters for {len(missing_ids)} anime (including 2023-2026 hits)...", flush=True)
    candidates = list(cached_chars)
    batch_size = 50
    for i in range(0, len(missing_ids), batch_size):
        batch = missing_ids[i:i + batch_size]
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
                if int(cid) in seen_cid:
                    continue
                seen_cid.add(int(cid))
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
        if (i // batch_size + 1) % 8 == 0 or (i + batch_size) >= len(missing_ids):
            print(f"  Batch {i // batch_size + 1}/{(len(missing_ids) + batch_size - 1) // batch_size}: {len(candidates)} total chars", flush=True)
        time.sleep(0.68)

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


def compute_dhash64(img_bgr: np.ndarray) -> int:
    """Compute a 64-bit horizontal difference hash invariant to brightness and resolution scaling."""
    gray = cv2.cvtColor(cv2.resize(img_bgr, (9, 8), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
    diff = gray[:, 1:] > gray[:, :-1]
    val = 0
    for bit in diff.ravel():
        val = (val << 1) | int(bool(bit))
    return int(val)


def train_poster_visuals(max_posters: int = 1200) -> None:
    """Build compact cover/key-art visual + dHash index across top popular & 2023-2026 anime."""
    rows_map: dict[int, tuple[int, str, str, int]] = {}
    for db_path in (HIANIME_DB, MUNIB_DB):
        if not os.path.exists(db_path):
            continue
        conn = sqlite3.connect(db_path)
        for aid, al_id, title, title_en, purl, plarge, pop, yr in conn.execute(
            """
            SELECT anime_id, COALESCE(anilist_id, 0), title, title_english, poster_url, poster_large,
                   COALESCE(anilist_popularity, 0), COALESCE(season_year, 0)
            FROM anime
            WHERE COALESCE(NULLIF(poster_large, ''), poster_url) IS NOT NULL
            ORDER BY COALESCE(anilist_popularity, 0) DESC, season_year DESC
            LIMIT 2500
            """
        ):
            img_url = (plarge or purl or "").strip()
            if not img_url.startswith("http"):
                continue
            key = int(al_id) if al_id and int(al_id) > 0 else -int(aid)
            score = int(pop or 0) + (60000 if int(yr or 0) >= 2023 else 0)
            if key not in rows_map or score > rows_map[key][3]:
                rows_map[key] = (int(al_id or 0), (title_en or title or "").strip(), img_url, score, int(aid))
        conn.close()

    ordered = sorted(rows_map.values(), key=lambda x: -x[3])[:max_posters]
    os.makedirs(POSTER_CACHE_DIR, exist_ok=True)
    print(f"Building Google-Lens cover/poster visual database for top {len(ordered)} anime...", flush=True)

    def _fetch_poster(entry):
        al_id, title, url, _, aid = entry
        cache_key = f"al_{al_id}" if al_id > 0 else f"id_{aid}"
        cpath = os.path.join(POSTER_CACHE_DIR, f"{cache_key}.bin")
        raw = None
        if os.path.exists(cpath) and os.path.getsize(cpath) > 200:
            try:
                raw = open(cpath, "rb").read()
            except Exception:
                raw = None
        if raw is None:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    raw = resp.read()
                if len(raw) > 200:
                    with open(cpath, "wb") as f:
                        f.write(raw)
            except Exception:
                return None
        if not raw:
            return None
        bgr = decode_bgr_composited(raw)
        if bgr is None:
            return None
        dv = extract_visual_descriptor(bgr)
        if dv is None:
            return None
        dh = compute_dhash64(bgr)
        return (al_id, aid, title, dv, dh)

    vecs = []
    dhashes = []
    pmeta = []
    with ThreadPoolExecutor(max_workers=24) as pool:
        for res in pool.map(_fetch_poster, ordered):
            if res is None:
                continue
            al_id, aid, title, dv, dh = res
            vecs.append(dv)
            dhashes.append(dh)
            pmeta.append({"al": al_id, "aid": aid, "t": title})

    if vecs:
        np.savez_compressed(
            OUT_POSTER_NPZ,
            features=np.vstack(vecs).astype(np.float16),
            dhashes=np.asarray(dhashes, dtype=np.uint64),
        )
        with gzip.open(OUT_POSTER_META, "wb") as f:
            f.write(json.dumps(pmeta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        for mirror_dir in MIRRORS:
            if os.path.isdir(mirror_dir):
                for p in (OUT_POSTER_NPZ, OUT_POSTER_META):
                    shutil.copy2(p, os.path.join(mirror_dir, os.path.basename(p)))
        print(f"Poster database ready: {len(pmeta)} covers ({os.path.getsize(OUT_POSTER_NPZ)/1e6:.2f} MB)", flush=True)


def main():
    max_anime = int(sys.argv[1]) if len(sys.argv) > 1 else 3600
    nn_limit = int(sys.argv[2]) if len(sys.argv) > 2 else 1350
    t_start = time.time()

    if not os.path.exists(MODEL_PATH) or not os.path.exists(TAGS_CSV_PATH):
        raise FileNotFoundError(f"Missing ONNX/CSV in {SCRATCH_DIR}: {MODEL_PATH}")

    vocab, gen_cols, vocab_map = load_tag_vocabulary()
    gen_cols_arr = np.asarray(gen_cols, dtype=np.int32)
    print(f"Curated semantic trait vocabulary: {len(vocab)} high-signal appearance tags", flush=True)

    db_tag_map = load_danbooru_char_tags()
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
    top_k = 64
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

    danbooru_hits = 0
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

        # Infuse verified Danbooru character appearance profiles (hair, eyes, outfit, accessories)
        db_tags = _match_danbooru_tags(item.get("n") or "", item.get("t") or "", db_tag_map)
        if db_tags:
            danbooru_hits += 1
            for dt_tag in db_tags:
                if dt_tag in STOP_APPEARANCE_TAGS:
                    continue
                ti = vocab_map.get(dt_tag, -1)
                if ti >= 0:
                    vec[ti] = max(vec[ti], 0.78)

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
                    vec[ti] = max(vec[ti], 0.68)

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

    print(f"Infused Danbooru multi-tag appearance traits into {danbooru_hits:,}/{n_chars:,} AniList characters!", flush=True)

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

    train_poster_visuals(1200)

    print(
        f"Training complete in {time.time() - t_start:.1f}s! "
        f"Characters={n_chars:,} | Danbooru={danbooru_hits:,} | NN_ViT={n_nn:,} | "
        f"brain={os.path.getsize(OUT_BRAIN)/1e6:.2f} MB | visuals={os.path.getsize(OUT_NPZ)/1e6:.2f} MB",
        flush=True,
    )


if __name__ == "__main__":
    main()
