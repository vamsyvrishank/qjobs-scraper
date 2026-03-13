"""
scraper.py — Generic multi-source job scraper
=============================================
Supports 4 source types configured entirely via firms.json:

  greenhouse  → public Greenhouse boards API (no auth)
  lever       → public Lever postings API (no auth)
  html        → BeautifulSoup / Playwright page scrape (link_pattern match)
  json_api    → fetch a JSON endpoint and map fields via config

To add a new firm: edit firms.json only. No Python changes needed.
"""

import requests
from bs4 import BeautifulSoup
import json, re, os, time, hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin

# ── Playwright (optional) ─────────────────────────────────────────────────────
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("Note: Playwright not installed — js_rendered sites will be skipped.")
    print("      Fix: pip install playwright && playwright install chromium")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Keyword filters ───────────────────────────────────────────────────────────
QUANT_KEYWORDS = [
    "quant", "quantitative", "researcher", "trading", "systematic",
    "algorithmic", "alpha", "signal", "data engineer", "data infrastructure",
    "software engineer", "swe", "developer", "platform engineer",
    "machine learning", "ml engineer", "low latency", "hft",
    "financial engineer", "risk", "execution", "research",
]
EXCLUDE_KEYWORDS = [
    "sales", "marketing", "human resources", "legal counsel",
    "compliance officer", "recruiter", "office manager",
    "executive assistant", "accounting", "facilities",
    "graphic design", "copywriter", "receptionist",
]

def is_relevant(title: str) -> bool:
    t = title.lower()
    if any(ex in t for ex in EXCLUDE_KEYWORDS):
        return False
    return any(kw in t for kw in QUANT_KEYWORDS)

def tag_role(title: str) -> str:
    t = title.lower()
    if any(x in t for x in ["quantitative researcher", "quant researcher"]):
        return "QR"
    if any(x in t for x in ["quantitative developer", "quant developer"]):
        return "QD"
    if any(x in t for x in ["data engineer", "data infrastructure"]):
        return "DE"
    if any(x in t for x in ["machine learning", "ml engineer"]):
        return "ML"
    if any(x in t for x in ["software engineer", "swe", "developer", "engineer"]):
        return "SWE/QD"
    if any(x in t for x in ["trader", "trading"]):
        return "Trading"
    return "Other"

# ── Location classifier ───────────────────────────────────────────────────────
USA_SIGNALS = [
    "new york", "chicago", "san francisco", "houston", "austin", "boston",
    "seattle", "los angeles", "miami", "denver", "stamford", "greenwich",
    "philadelphia", "atlanta", "dallas", "minneapolis", "connecticut",
    "new jersey", "remote", "u.s.", "usa", "united states",
    " ny", " il", " ca", " tx", " ma", " ct", " nj", " fl", " wa",
    " pa", " ga", " co", " or", " nc", " oh", " mn", " va", " md",
]
INDIA_SIGNALS = [
    "india", "mumbai", "bangalore", "bengaluru",
    "hyderabad", "chennai", "pune", "delhi",
    "gurugram", "gurgaon", "noida",
]

def classify_location(loc: str) -> str:
    if not loc:
        return "USA"
    s = " " + loc.lower() + " "
    if any(sig in s for sig in INDIA_SIGNALS):
        return "India"
    if any(sig in s for sig in USA_SIGNALS):
        return "USA"
    return "Other"

# ── Stable job ID ─────────────────────────────────────────────────────────────
def make_job_id(url: str) -> str:
    normalized = url.split("?")[0].rstrip("/").lower()
    return hashlib.md5(normalized.encode()).hexdigest()[:12]

# ── Per-domain rate limiting ──────────────────────────────────────────────────
_domain_last_hit: dict = {}
DOMAIN_COOLDOWN = 20  # seconds between hits to same domain

def _rate_limit(url: str):
    domain = urlparse(url).netloc
    elapsed = time.time() - _domain_last_hit.get(domain, 0)
    if elapsed < DOMAIN_COOLDOWN:
        time.sleep(DOMAIN_COOLDOWN - elapsed)
    _domain_last_hit[domain] = time.time()

# ── Firms config ──────────────────────────────────────────────────────────────
FIRMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firms.json")

def load_firms() -> dict:
    with open(FIRMS_FILE, "r") as f:
        return json.load(f)

# ── Playwright / requests HTML fetcher ───────────────────────────────────────
def _fetch_html_playwright(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=30000)
        html = page.content()
        browser.close()
    return html

def _fetch_html(url: str, js_rendered: bool) -> str:
    if js_rendered:
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "js_rendered=true requires Playwright. "
                "Run: pip install playwright && playwright install chromium"
            )
        return _fetch_html_playwright(url)
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.text

# ── Nested dict path walker ───────────────────────────────────────────────────
def _resolve_path(obj, path: str):
    """Walk a dot-separated key path through nested dicts/lists."""
    if not path:
        return obj
    for key in path.split("."):
        if isinstance(obj, list):
            obj = [item.get(key) for item in obj if isinstance(item, dict)]
        elif isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return None
    return obj

def _get_field(obj: dict, field_spec: str) -> str:
    """
    field_spec supports:
      "title"           — simple key
      "position.name"   — dot path
      "title|name|text" — pipe-separated alternatives (first non-empty wins)
    """
    for spec in field_spec.split("|"):
        val = _resolve_path(obj, spec.strip())
        if val and isinstance(val, str) and val.strip():
            return val.strip()
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE TYPE 1 — Greenhouse public API
# ══════════════════════════════════════════════════════════════════════════════
def fetch_greenhouse(entry: dict, source_errors: list, source_timings: dict) -> list:
    """
    firms.json entry:
      { "slug": "pdtpartners", "name": "PDT Partners" }
    """
    firm = entry["name"]
    slug = entry["slug"]
    jobs = []
    t0 = time.time()
    try:
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            source_errors.append({"firm": firm, "error": f"HTTP {r.status_code}"})
            return jobs
        for job in r.json().get("jobs", []):
            title = job.get("title", "")
            if not is_relevant(title):
                continue
            offices  = job.get("offices", [])
            location = offices[0].get("name", "") if offices else ""
            job_url  = job.get("absolute_url", "")
            posted   = job.get("updated_at", "")[:10] if job.get("updated_at") else ""
            jobs.append({
                "id":              make_job_id(job_url),
                "title":           title,
                "firm":            firm,
                "location":        location,
                "location_region": classify_location(location),
                "url":             job_url,
                "posted":          posted,
                "tag":             tag_role(title),
                "source":          "greenhouse",
            })
    except Exception as e:
        source_errors.append({"firm": firm, "error": str(e)})
        print(f"  Greenhouse error {firm}: {e}")
    source_timings[firm] = int((time.time() - t0) * 1000)
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE TYPE 2 — Lever public API
# ══════════════════════════════════════════════════════════════════════════════
def fetch_lever(entry: dict, source_errors: list, source_timings: dict) -> list:
    """
    firms.json entry:
      { "slug": "radixtrading", "name": "Radix Trading" }
    """
    firm = entry["name"]
    slug = entry["slug"]
    jobs = []
    t0 = time.time()
    try:
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            source_errors.append({"firm": firm, "error": f"HTTP {r.status_code}"})
            return jobs
        for job in r.json():
            title = job.get("text", "")
            if not is_relevant(title):
                continue
            location   = job.get("categories", {}).get("location", "")
            created_ms = job.get("createdAt", 0)
            posted = (
                datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
                .strftime("%Y-%m-%d") if created_ms else ""
            )
            job_url = job.get("hostedUrl", "")
            jobs.append({
                "id":              make_job_id(job_url),
                "title":           title,
                "firm":            firm,
                "location":        location,
                "location_region": classify_location(location),
                "url":             job_url,
                "posted":          posted,
                "tag":             tag_role(title),
                "source":          "lever",
            })
    except Exception as e:
        source_errors.append({"firm": firm, "error": str(e)})
        print(f"  Lever error {firm}: {e}")
    source_timings[firm] = int((time.time() - t0) * 1000)
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE TYPE 3 — Generic HTML scrape
# ══════════════════════════════════════════════════════════════════════════════
def fetch_html(entry: dict, source_errors: list, source_timings: dict) -> list:
    """
    firms.json entry:
      {
        "name":         "Citadel",
        "url":          "https://www.citadel.com/careers/open-opportunities/",
        "js_rendered":  true,
        "link_pattern": "/careers/details/",
        "base_url":     "https://www.citadel.com",
        "location":     "New York, NY"
      }

    Fields:
      url          — the careers listing page to scrape
      js_rendered  — (optional, default false) use Playwright instead of requests
      link_pattern — substring that every job link href must contain
                     if omitted, ALL links with relevant anchor text are kept
      base_url     — (optional) prepended to relative hrefs
      location     — (optional) static location string for all jobs from this firm
    """
    firm        = entry["name"]
    url         = entry["url"]
    js_rendered = entry.get("js_rendered", False)
    pattern     = entry.get("link_pattern", "")
    base_url    = entry.get("base_url", "").rstrip("/")
    static_loc  = entry.get("location", "")
    jobs = []
    t0 = time.time()

    try:
        _rate_limit(url)
        html = _fetch_html(url, js_rendered)
        soup = BeautifulSoup(html, "html.parser")

        seen_urls = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith("#") or href.startswith("mailto:"):
                continue
            if pattern and pattern not in href:
                continue

            # Resolve relative URLs
            if href.startswith("http"):
                full_url = href
            elif base_url:
                full_url = urljoin(base_url + "/", href.lstrip("/"))
            else:
                continue

            title = re.sub(r"\s+", " ", a.get_text(separator=" ", strip=True))
            if not title or len(title) < 4 or len(title) > 150:
                continue
            if not is_relevant(title):
                continue
            if full_url in seen_urls:
                continue

            seen_urls.add(full_url)
            jobs.append({
                "id":              make_job_id(full_url),
                "title":           title,
                "firm":            firm,
                "location":        static_loc,
                "location_region": classify_location(static_loc),
                "url":             full_url,
                "posted":          "",
                "tag":             tag_role(title),
                "source":          "playwright" if js_rendered else "html",
            })

        if not jobs:
            hint = (
                "No jobs found — page may need js_rendered: true (install Playwright)."
                if not js_rendered
                else "No matching links found — check link_pattern in firms.json."
            )
            source_errors.append({"firm": firm, "error": hint})

    except Exception as e:
        source_errors.append({"firm": firm, "error": str(e)})
        print(f"  HTML error {firm}: {e}")

    source_timings[firm] = int((time.time() - t0) * 1000)
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE TYPE 4 — Generic JSON API
# ══════════════════════════════════════════════════════════════════════════════
def fetch_json_api(entry: dict, source_errors: list, source_timings: dict) -> list:
    """
    firms.json entry:
      {
        "name":           "Jane Street",
        "url":            "https://www.janestreet.com/open-roles.json",
        "jobs_path":      "jobs",
        "title_field":    "title",
        "url_field":      "url|link|applyUrl",
        "location_field": "location|office",
        "posted_field":   "created_at",
        "base_url":       "https://www.janestreet.com",
        "location":       "New York, NY"
      }

    Fields:
      url            — JSON endpoint URL
      jobs_path      — dot-path to the array inside the JSON, e.g. "data.postings"
                       omit if the root is already an array
      title_field    — dot-path or pipe-separated alts to the job title string
      url_field      — dot-path or pipe-separated alts to the apply/detail URL
      location_field — (optional) dot-path or alts to location string
      posted_field   — (optional) dot-path to a date string (truncated to YYYY-MM-DD)
      base_url       — (optional) prepended to relative url_field values
      location       — (optional) static fallback location if location_field is absent
    """
    firm         = entry["name"]
    url          = entry["url"]
    jobs_path    = entry.get("jobs_path", "")
    title_field  = entry.get("title_field", "title")
    url_field    = entry.get("url_field", "url")
    loc_field    = entry.get("location_field", "")
    posted_field = entry.get("posted_field", "")
    base_url     = entry.get("base_url", "").rstrip("/")
    static_loc   = entry.get("location", "")
    jobs = []
    t0 = time.time()

    try:
        _rate_limit(url)
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()

        items = _resolve_path(data, jobs_path) if jobs_path else data
        if not isinstance(items, list):
            raise ValueError(
                f"jobs_path '{jobs_path}' resolved to {type(items).__name__}, "
                f"expected list. Check firms.json."
            )

        for item in items:
            if not isinstance(item, dict):
                continue
            title = _get_field(item, title_field)
            if not title or not is_relevant(title):
                continue

            job_url = _get_field(item, url_field)
            if job_url and base_url and not job_url.startswith("http"):
                job_url = f"{base_url}/{job_url.lstrip('/')}"

            location = _get_field(item, loc_field) if loc_field else ""
            if not location:
                location = static_loc

            posted = _get_field(item, posted_field) if posted_field else ""
            if posted and len(posted) >= 10:
                posted = posted[:10]

            jobs.append({
                "id":              make_job_id(job_url or title + firm),
                "title":           title,
                "firm":            firm,
                "location":        location,
                "location_region": classify_location(location),
                "url":             job_url,
                "posted":          posted,
                "tag":             tag_role(title),
                "source":          "json_api",
            })

    except Exception as e:
        source_errors.append({"firm": firm, "error": str(e)})
        print(f"  JSON API error {firm}: {e}")

    source_timings[firm] = int((time.time() - t0) * 1000)
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
# DISPATCHER
# ══════════════════════════════════════════════════════════════════════════════
SOURCE_HANDLERS = {
    "greenhouse": fetch_greenhouse,
    "lever":      fetch_lever,
    "html":       fetch_html,
    "json_api":   fetch_json_api,
}


# ══════════════════════════════════════════════════════════════════════════════
# MASTER FETCH
# ══════════════════════════════════════════════════════════════════════════════
def fetch_all_jobs():
    all_jobs       = []
    source_errors  = []
    source_timings = {}

    firms = load_firms()

    for source_type, handler in SOURCE_HANDLERS.items():
        for entry in firms.get(source_type, []):
            firm_name = entry.get("name", "?")
            try:
                jobs = handler(entry, source_errors, source_timings)
                all_jobs.extend(jobs)
                print(f"  [{source_type}] {firm_name}: {len(jobs)} jobs")
            except Exception as e:
                source_errors.append({"firm": firm_name, "error": str(e)})
                print(f"  [{source_type}] {firm_name}: ERROR — {e}")

    # ── Dual-key deduplication ─────────────────────────────────────────────
    seen_url_keys   = set()
    seen_title_keys = {}
    deduped         = []

    for j in all_jobs:
        url_key    = j["url"].split("?")[0].rstrip("/").lower()
        norm_title = re.sub(r"[^a-z0-9]", "", j["title"].lower())
        title_key  = f"{j['firm'].lower()}::{norm_title}"

        if not url_key:
            continue
        if url_key in seen_url_keys:
            continue

        seen_url_keys.add(url_key)

        if title_key in seen_title_keys:
            existing  = deduped[seen_title_keys[title_key]]
            new_loc   = j.get("location", "")
            exist_loc = existing.get("location", "")
            if new_loc and new_loc not in exist_loc:
                m = re.search(r"\+(\d+) more", exist_loc)
                if m:
                    n = int(m.group(1)) + 1
                    existing["location"] = re.sub(r"\+\d+ more", f"+{n} more", exist_loc)
                else:
                    existing["location"] = f"{exist_loc} +1 more"
        else:
            seen_title_keys[title_key] = len(deduped)
            deduped.append(j)

    print(f"\nTotal unique jobs: {len(deduped)}")
    if source_errors:
        print(f"Source errors ({len(source_errors)}): {[e['firm'] for e in source_errors]}")

    return deduped, source_errors, source_timings


if __name__ == "__main__":
    jobs, errors, timings = fetch_all_jobs()
    print(json.dumps(jobs[:3], indent=2))
    if errors:
        print("\nErrors:", json.dumps(errors, indent=2))
