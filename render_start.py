"""
KuroAnime Render Startup & Automatic Dual-Scraper Orchestrator
==============================================================
1. Initializes hianime.db (Server 1) and munib_server.db (Server 2) on persistent
   disk (if ANIME_DB / MUNIB_DB are configured, e.g. /data/hianime.db) or in the
   project root by copying/decompressing the bundled .db / .db.gz files.
2. Starts a background daemon thread that automatically runs:
   - scrape_new_anime_and_episodes_fast.py (Server 1 — ZokoAnime)
   - scrape_munib_new_episodes.py          (Server 2 — Munib Server)
   on startup (after a short warm-up) and every SCRAPE_INTERVAL_MINUTES (default 120m).
3. Starts the FastAPI + Static Web UI server via Uvicorn on 0.0.0.0:$PORT.
"""
import gzip
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = os.environ.get("SCRAPER_STATUS_FILE") or os.path.join(BASE_DIR, "scraper_status.json")
SCRAPE_INTERVAL_MINUTES = int(os.environ.get("SCRAPE_INTERVAL_MINUTES", "120"))
INITIAL_DELAY_SECONDS = int(os.environ.get("SCRAPE_INITIAL_DELAY_SECONDS", "45"))


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[RenderOrchestrator {ts}] {msg}", flush=True)


def write_status(**kwargs):
    data = {
        "auto_scraper_enabled": True,
        "interval_minutes": SCRAPE_INTERVAL_MINUTES,
        "running": False,
        "last_run_at": None,
        "next_run_at": None,
        "runs_completed": 0,
        "last_summary": None,
    }
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                data.update(json.load(f))
        except Exception:
            pass
    data.update(kwargs)
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"Warning writing status file: {e}")


def ensure_database_initialized(env_var: str, default_filename: str, gz_rel_path: str) -> str:
    """
    Ensure the target SQLite database exists and is non-empty.
    If ANIME_DB or MUNIB_DB points to a persistent disk (e.g. /data/hianime.db)
    that doesn't exist yet, seed it from the repo's .db or .db.gz file.
    """
    target_path = os.environ.get(env_var) or os.path.join(BASE_DIR, default_filename)
    target_path = os.path.abspath(target_path)
    os.environ[env_var] = target_path

    if os.path.exists(target_path) and os.path.getsize(target_path) > 100_000:
        log(f"{env_var} ready at {target_path} ({os.path.getsize(target_path) / 1048576:.2f} MB)")
        return target_path

    try:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
    except Exception:
        # Fallback to local workspace directory if /data is not mounted
        target_path = os.path.join(BASE_DIR, default_filename)
        os.environ[env_var] = target_path
        if os.path.exists(target_path) and os.path.getsize(target_path) > 100_000:
            log(f"{env_var} fallback ready at {target_path} ({os.path.getsize(target_path) / 1048576:.2f} MB)")
            return target_path

    repo_db = os.path.join(BASE_DIR, default_filename)
    repo_gz = os.path.join(BASE_DIR, gz_rel_path)

    if os.path.exists(repo_db) and os.path.abspath(repo_db) != target_path and os.path.getsize(repo_db) > 100_000:
        log(f"Seeding {target_path} from {repo_db}...")
        shutil.copy2(repo_db, target_path)
    elif os.path.exists(repo_gz):
        log(f"Decompressing {repo_gz} -> {target_path}...")
        with gzip.open(repo_gz, "rb") as f_in, open(target_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    else:
        log(f"Warning: No seed database found for {default_filename}; scraper will initialize it.")

    return target_path


def run_single_scraper(script_name: str, label: str) -> str:
    script_path = os.path.join(BASE_DIR, script_name)
    if not os.path.exists(script_path):
        msg = f"{label}: script {script_name} not found"
        log(msg)
        return msg

    log(f"Starting {label} ({script_name})...")
    start_t = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-u", script_path],
            cwd=BASE_DIR,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=900,  # 15-minute safety timeout per pass
        )
        elapsed = time.time() - start_t
        tail_lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()][-6:]
        summary = " | ".join(tail_lines) if tail_lines else f"exit={proc.returncode}"
        log(f"Finished {label} in {elapsed:.1f}s (exit={proc.returncode}): {summary}")
        if proc.returncode != 0 and proc.stderr:
            log(f"{label} stderr: {proc.stderr[-400:]}")
        return f"{label} ({elapsed:.0f}s): {summary}"
    except subprocess.TimeoutExpired:
        msg = f"{label}: timed out after 900s"
        log(msg)
        return msg
    except Exception as e:
        msg = f"{label}: error {e}"
        log(msg)
        return msg


def auto_scraper_loop():
    log(
        f"Auto-Scraper Daemon started. Initial run in {INITIAL_DELAY_SECONDS}s, "
        f"then every {SCRAPE_INTERVAL_MINUTES} minutes."
    )
    next_dt = datetime.now(timezone.utc) + timedelta(seconds=INITIAL_DELAY_SECONDS)
    write_status(running=False, next_run_at=next_dt.isoformat())

    time.sleep(INITIAL_DELAY_SECONDS)
    runs_completed = 0

    while True:
        now_iso = datetime.now(timezone.utc).isoformat()
        write_status(running=True, last_run_at=now_iso)

        s1_summary = run_single_scraper("scrape_new_anime_and_episodes_fast.py", "Server 1 (ZokoAnime)")
        s2_summary = run_single_scraper("scrape_munib_new_episodes.py", "Server 2 (Munib Server)")

        runs_completed += 1
        next_dt = datetime.now(timezone.utc) + timedelta(minutes=SCRAPE_INTERVAL_MINUTES)
        write_status(
            running=False,
            last_run_at=datetime.now(timezone.utc).isoformat(),
            next_run_at=next_dt.isoformat(),
            runs_completed=runs_completed,
            last_summary={"server1": s1_summary, "server2": s2_summary},
        )
        log(f"Cycle #{runs_completed} complete. Sleeping {SCRAPE_INTERVAL_MINUTES} minutes until next scrape...")
        time.sleep(SCRAPE_INTERVAL_MINUTES * 60)


def main():
    log("Booting KuroAnime Render Service...")
    ensure_database_initialized("ANIME_DB", "hianime.db", os.path.join("api", "hianime.db.gz"))
    ensure_database_initialized("MUNIB_DB", "munib_server.db", os.path.join("api", "munib_server.db.gz"))

    if os.environ.get("DISABLE_AUTO_SCRAPER", "").lower() not in ("1", "true", "yes"):
        scraper_thread = threading.Thread(target=auto_scraper_loop, name="AutoScraperDaemon", daemon=True)
        scraper_thread.start()
    else:
        log("Auto-Scraper disabled via DISABLE_AUTO_SCRAPER environment variable.")

    port = int(os.environ.get("PORT", "8000"))
    log(f"Starting FastAPI Uvicorn server on 0.0.0.0:{port}...")
    import uvicorn
    uvicorn.run("api.index:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()

