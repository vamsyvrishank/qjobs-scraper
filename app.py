from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import json, time, threading, os
from datetime import datetime, timedelta, timezone
from scraper import fetch_all_jobs

app = Flask(__name__, static_folder=".")
CORS(app)

# ── File paths ────────────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
JOBS_FILE    = os.path.join(BASE_DIR, "jobs.json")
APPLIED_FILE = os.path.join(BASE_DIR, "applied.json")
ARCHIVE_FILE = os.path.join(BASE_DIR, "jobs_archive.json")
ARCHIVE_DAYS = 60

# ── In-memory cache ───────────────────────────────────────────────────────────
_cache = {
    "jobs":          [],
    "last_fetched":  0,
    "last_scraped":  0,      # timestamp of last live scrape (0 if loaded from file)
    "status":        "idle",
    "error":         None,
    "data_source":   "none", # "file" | "scrape" | "none"
    "source_errors": [],     # [{firm, error}] from last scrape run
    "source_timings": {},    # {firm: ms}
}
CACHE_TTL = 600  # 10 minutes

# Lock for applied.json writes
_applied_lock = threading.Lock()

# ── File I/O helpers ──────────────────────────────────────────────────────────

def _load_jobs_file():
    if not os.path.exists(JOBS_FILE):
        return {"meta": {}, "jobs": []}
    try:
        with open(JOBS_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: could not load jobs.json: {e}")
        return {"meta": {}, "jobs": []}


def _save_jobs_file(jobs_list, last_scraped_iso):
    payload = {
        "meta": {
            "last_saved":   datetime.now(timezone.utc).isoformat() + "Z",
            "last_scraped": last_scraped_iso,
            "total":        len(jobs_list),
        },
        "jobs": jobs_list,
    }
    tmp = JOBS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, JOBS_FILE)   # atomic write


def _load_applied_file():
    if not os.path.exists(APPLIED_FILE):
        return {}
    try:
        with open(APPLIED_FILE, "r") as f:
            data = json.load(f)
        return {entry["job_id"]: entry for entry in data.get("applied", [])}
    except Exception as e:
        print(f"Warning: could not load applied.json: {e}")
        return {}


def _save_applied_file(applied_dict):
    tmp = APPLIED_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"applied": list(applied_dict.values())}, f, indent=2)
    os.replace(tmp, APPLIED_FILE)

# ── Merge & archive ───────────────────────────────────────────────────────────

def _merge_jobs(existing_jobs, new_jobs):
    """Merge freshly-scraped jobs into the existing persisted list."""
    now_iso = datetime.now(timezone.utc).isoformat() + "Z"
    index = {j["id"]: j for j in existing_jobs if j.get("id")}

    for job in new_jobs:
        jid = job.get("id")
        if not jid:
            continue
        if jid in index:
            existing = index[jid]
            existing["last_seen"]       = now_iso
            existing["location"]        = job["location"]
            existing["location_region"] = job.get("location_region", existing.get("location_region", "USA"))
            existing["posted"]          = job["posted"] or existing.get("posted", "")
            existing["title"]           = job["title"]
        else:
            job["first_seen"] = now_iso
            job["last_seen"]  = now_iso
            index[jid] = job

    return list(index.values())


def _archive_old_jobs(jobs_list):
    """Split into (active, stale) based on last_seen age."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=ARCHIVE_DAYS)
    active, stale = [], []
    for j in jobs_list:
        try:
            ls = j.get("last_seen", "")
            if ls:
                dt = datetime.fromisoformat(ls.rstrip("Z"))
                if dt < cutoff:
                    stale.append(j)
                    continue
        except (ValueError, TypeError):
            pass
        active.append(j)
    return active, stale


def _append_to_archive(stale_jobs):
    if not stale_jobs:
        return
    existing = []
    if os.path.exists(ARCHIVE_FILE):
        try:
            with open(ARCHIVE_FILE, "r") as f:
                existing = json.load(f).get("jobs", [])
        except Exception:
            pass
    archive_ids = {j["id"] for j in existing if j.get("id")}
    new_entries = [j for j in stale_jobs if j.get("id") not in archive_ids]
    with open(ARCHIVE_FILE, "w") as f:
        json.dump({"jobs": existing + new_entries}, f, indent=2)

# ── Refresh logic ─────────────────────────────────────────────────────────────

def _refresh():
    try:
        _cache["status"] = "fetching"
        _cache["error"]  = None

        # 1. Fetch from network
        new_jobs, source_errors, source_timings = fetch_all_jobs()

        # 2. Load existing persisted jobs
        stored        = _load_jobs_file()
        existing_jobs = stored.get("jobs", [])

        # 3. Merge new into existing
        merged = _merge_jobs(existing_jobs, new_jobs)

        # 4. Archive jobs not seen in 60+ days
        active, stale = _archive_old_jobs(merged)
        _append_to_archive(stale)

        # 5. Persist active jobs
        now_iso = datetime.now(timezone.utc).isoformat() + "Z"
        _save_jobs_file(active, last_scraped_iso=now_iso)

        # 6. Update cache
        _cache["jobs"]           = active
        _cache["last_fetched"]   = time.time()
        _cache["last_scraped"]   = time.time()
        _cache["data_source"]    = "scrape"
        _cache["source_errors"]  = source_errors
        _cache["source_timings"] = source_timings
        _cache["status"]         = "ready"
        print(f"Scrape complete: {len(new_jobs)} fetched, {len(active)} active, {len(stale)} archived, {len(source_errors)} source errors")

    except Exception as e:
        _cache["status"] = "error"
        _cache["error"]  = str(e)
        print(f"Fetch error: {e}")

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/jobs")
def get_jobs():
    now = time.time()
    if now - _cache["last_fetched"] > CACHE_TTL and _cache["status"] != "fetching":
        t = threading.Thread(target=_refresh, daemon=True)
        t.start()
        if not _cache["jobs"]:
            t.join(timeout=45)
    return jsonify({
        "jobs":          _cache["jobs"],
        "last_fetched":  _cache["last_fetched"],
        "last_scraped":  _cache.get("last_scraped", 0),
        "data_source":   _cache.get("data_source", "none"),
        "status":        _cache["status"],
        "count":         len(_cache["jobs"]),
        "error":         _cache["error"],
        "source_errors": _cache.get("source_errors", []),
    })


@app.route("/api/refresh", methods=["POST"])
def force_refresh():
    _cache["last_fetched"] = 0
    t = threading.Thread(target=_refresh, daemon=True)
    t.start()
    return jsonify({"message": "Scrape started"})


@app.route("/api/reload", methods=["POST"])
def reload_from_file():
    """Load jobs from jobs.json without hitting any external APIs."""
    stored = _load_jobs_file()
    jobs   = stored.get("jobs", [])
    _cache["jobs"]           = jobs
    _cache["last_fetched"]   = time.time()
    _cache["last_scraped"]   = 0
    _cache["data_source"]    = "file"
    _cache["source_errors"]  = []
    _cache["status"]         = "ready"
    return jsonify({"message": f"Loaded {len(jobs)} jobs from file", "count": len(jobs)})


@app.route("/api/status")
def status():
    return jsonify({
        "status":         _cache["status"],
        "count":          len(_cache["jobs"]),
        "last_fetched":   _cache["last_fetched"],
        "last_scraped":   _cache.get("last_scraped", 0),
        "data_source":    _cache.get("data_source", "none"),
        "error":          _cache["error"],
        "source_errors":  _cache.get("source_errors", []),
        "source_timings": _cache.get("source_timings", {}),
    })


@app.route("/api/archive")
def get_archive():
    if not os.path.exists(ARCHIVE_FILE):
        return jsonify({"jobs": [], "count": 0})
    try:
        with open(ARCHIVE_FILE, "r") as f:
            data = json.load(f)
        jobs = data.get("jobs", [])
        return jsonify({"jobs": jobs, "count": len(jobs)})
    except Exception as e:
        return jsonify({"jobs": [], "count": 0, "error": str(e)})


@app.route("/api/applied", methods=["GET"])
def get_applied():
    applied = _load_applied_file()
    return jsonify({"applied": list(applied.values())})


@app.route("/api/applied", methods=["POST"])
def mark_applied():
    body   = request.get_json(force=True) or {}
    job_id = body.get("job_id", "").strip()
    if not job_id:
        return jsonify({"error": "job_id required"}), 400
    with _applied_lock:
        applied = _load_applied_file()
        if job_id not in applied:
            applied[job_id] = {
                "job_id":     job_id,
                "applied_at": datetime.now(timezone.utc).isoformat() + "Z",
                "notes":      body.get("notes", ""),
                "title":      body.get("title", ""),
                "firm":       body.get("firm", ""),
                "url":        body.get("url", ""),
            }
            _save_applied_file(applied)
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/applied/<job_id>", methods=["DELETE"])
def unmark_applied(job_id):
    with _applied_lock:
        applied = _load_applied_file()
        if job_id in applied:
            del applied[job_id]
            _save_applied_file(applied)
    return jsonify({"ok": True})


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


# ── Startup ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Starting QuantJobs server on http://localhost:5000")

    stored = _load_jobs_file()
    if stored["jobs"]:
        _cache["jobs"]         = stored["jobs"]
        _cache["last_fetched"] = time.time()
        _cache["data_source"]  = "file"
        _cache["status"]       = "ready"
        print(f"Loaded {len(stored['jobs'])} jobs from jobs.json — background scrape starting…")
        # Background scrape to pick up new postings
        t = threading.Thread(target=_refresh, daemon=True)
        t.start()
    else:
        print("No jobs.json found. Running initial scrape (this may take a minute)…")
        _refresh()

    app.run(debug=False, port=5000, threaded=True)
