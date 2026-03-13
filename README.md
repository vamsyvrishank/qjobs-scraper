# QuantJobs — Live Quant Firm Job Scraper

A local web app that aggregates job listings from quant firms, persists them across runs, and lets you track your applications — all without any external database or auth.

---

## Quick Start

```bash
# 1. Install core dependencies
pip install flask flask-cors requests beautifulsoup4

# 2. (Optional but recommended) Install Playwright for JS-rendered sites
#    Required for full Jane Street and Two Sigma results
pip install playwright && playwright install chromium

# 3. Run the server
python app.py

# 4. Open in browser
open http://localhost:5000
```

On first run the server scrapes all sources and saves results to `jobs.json`. On every subsequent start it loads from `jobs.json` instantly (no wait) and then scrapes for new jobs in the background.

---

## Features

| Feature | Details |
|---|---|
| **Multi-source scraping** | Greenhouse API, Lever API, custom HTML scrapers, Playwright for JS-rendered pages |
| **Location filter** | Filter by USA / India / USA + India / All Regions |
| **Role filter** | QR, QD, SWE/QD, DE, ML, Trading, All |
| **Firm sidebar** | Click any firm to filter; shows live job count |
| **Full-text search** | Searches title, firm, location in real-time |
| **Persistent storage** | Jobs saved to `jobs.json` — survive server restarts, accumulate over time |
| **Smart deduplication** | URL-based + title-based dedup; multi-location same role shown as "New York, +2 more" |
| **Application tracking** | Mark/unmark applied per job; state saved to `applied.json` with metadata snapshot |
| **Archive** | Jobs not seen in 60 days auto-moved to `jobs_archive.json`; viewable in the UI |
| **Source error visibility** | Amber banner shows which scrapers failed on the last run |
| **Load from file** | "↑ FROM FILE" button loads cached data instantly without any network calls |
| **Last scraped timestamp** | Footer shows when data was last fetched vs loaded from disk |
| **Per-domain rate limiting** | 30s cooldown per domain prevents hammering sites on rapid re-scrapes |

---

## Adding Your Own Firms

All firm configuration lives in **`firms.json`** — no Python code changes needed for Greenhouse and Lever firms.

### Step 1 — Open `firms.json`

```json
{
  "greenhouse": [ ... ],
  "lever":      [ ... ],
  "custom":     [ ... ]
}
```

### Adding a Greenhouse firm

1. Find the firm's Greenhouse slug by visiting `https://boards.greenhouse.io/SLUG` — if the page loads, the slug is valid.
2. Add an entry to the `"greenhouse"` array:

```json
{
  "greenhouse": [
    {"slug": "citadel", "name": "Citadel"},
    {"slug": "yourfirm", "name": "Your Firm Name"}
  ]
}
```

**Examples of known slugs:**

| Firm | Slug |
|---|---|
| Citadel | `citadel` |
| Citadel Securities | `citadelsecurities` |
| DRW | `drw` |
| Hudson River Trading | `hrt` |
| Jump Trading | `jumptrading` |
| Akuna Capital | `akunacapital` |
| IMC Trading | `imc` |
| Susquehanna (SIG) | `sig` |
| PDT Partners | `pdtpartners` |
| Renaissance Technologies | `rentec` *(if they use Greenhouse)* |
| Virtu Financial | `virtu` |
| Tower Research | `tower-research-capital` |

> To verify: open `https://boards-api.greenhouse.io/v1/boards/SLUG/jobs` in your browser. If you get a JSON response with a `"jobs"` array, the slug works.

### Adding a Lever firm

1. Find the slug by visiting `https://jobs.lever.co/SLUG` — if the page loads, it's valid.
2. Add to the `"lever"` array:

```json
{
  "lever": [
    {"slug": "radixtrading", "name": "Radix Trading"},
    {"slug": "yourfirmslug", "name": "Your Firm Name"}
  ]
}
```

**Examples:**

| Firm | Slug |
|---|---|
| Radix Trading | `radixtrading` |
| Volant Trading | `volanttrading` |
| Two Roads | `tworoads` |
| D.E. Shaw | `deshaw` |
| Marshall Wace | `marshallwace` |

### Adding a custom scraper (HTML career pages)

For firms that don't use Greenhouse or Lever, you need to write a small scraper function. This takes about 10–20 lines of code.

**Step 1** — Add the entry to `firms.json`:

```json
{
  "custom": [
    {
      "name": "Your Firm",
      "url":  "https://careers.yourfirm.com/jobs",
      "type": "yourfirm"
    }
  ]
}
```

**Step 2** — Add a fetcher function to `scraper.py`:

```python
def fetch_yourfirm(source_errors, source_timings):
    firm = "Your Firm"
    jobs = []
    t0   = time.time()
    try:
        url = "https://careers.yourfirm.com/jobs"
        _rate_limit(url)                                      # respect rate limits
        r   = requests.get(url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            title = a.get_text(strip=True)
            if not title or not is_relevant(title):
                continue
            href     = a["href"]
            full_url = href if href.startswith("http") else f"https://careers.yourfirm.com{href}"
            # Add a URL pattern check to avoid false positives, e.g.:
            if "/jobs/" not in full_url:
                continue
            jobs.append({
                "id":              make_job_id(full_url),
                "title":           title,
                "firm":            firm,
                "location":        "New York, NY",           # hardcode or parse from page
                "location_region": "USA",
                "url":             full_url,
                "posted":          "",
                "tag":             tag_role(title),
                "source":          "scrape",
            })
    except Exception as e:
        source_errors.append({"firm": firm, "error": str(e)})
        print(f"{firm} error: {e}")
    source_timings[firm] = int((time.time() - t0) * 1000)
    return jobs
```

**Step 3** — Register it in `CUSTOM_DISPATCHERS` in `scraper.py`:

```python
CUSTOM_DISPATCHERS = {
    "twosigma":   fetch_twosigma,
    "janestreet": fetch_janestreet,
    "yourfirm":   fetch_yourfirm,   # <-- add this
    ...
}
```

That's it. Restart the server and your new firm will appear.

### JS-rendered pages (Playwright)

Some career portals require JavaScript to load job listings (Jane Street and Two Sigma are the main ones). Install Playwright:

```bash
pip install playwright && playwright install chromium
```

The scraper automatically detects whether Playwright is installed and uses it for JS-rendered sites. Without it, those sources return 0 jobs and a warning appears in the UI's amber errors banner.

For a new JS-rendered firm, follow the custom scraper steps above but swap `requests.get()` for `_playwright_get_html(url)`:

```python
html = _playwright_get_html(url)    # renders JS before parsing
soup = BeautifulSoup(html, "html.parser")
```

---

## Data Files

All files are created automatically in the `scrapper/` directory:

| File | Purpose | Created by |
|---|---|---|
| `firms.json` | Firm configuration — edit this to add firms | You |
| `jobs.json` | Active job listings (last 60 days) | Auto on first scrape |
| `applied.json` | Your application history with metadata snapshot | Auto when you mark applied |
| `jobs_archive.json` | Jobs not seen in 60+ days | Auto after 60 days |

### jobs.json schema

```json
{
  "meta": {
    "last_saved":   "2026-03-12T10:30:00Z",
    "last_scraped": "2026-03-12T10:30:00Z",
    "total": 247
  },
  "jobs": [
    {
      "id":              "a3f8bc2e14d9",
      "title":           "Quantitative Researcher",
      "firm":            "Citadel",
      "location":        "Chicago, IL, +2 more",
      "location_region": "USA",
      "url":             "https://boards.greenhouse.io/citadel/jobs/12345",
      "posted":          "2026-03-10",
      "tag":             "QR",
      "source":          "greenhouse",
      "first_seen":      "2026-03-10T08:00:00Z",
      "last_seen":       "2026-03-12T10:30:00Z"
    }
  ]
}
```

### applied.json schema

```json
{
  "applied": [
    {
      "job_id":     "a3f8bc2e14d9",
      "applied_at": "2026-03-12T14:22:00Z",
      "notes":      "",
      "title":      "Quantitative Researcher",
      "firm":       "Citadel",
      "url":        "https://..."
    }
  ]
}
```

Title, firm, and URL are snapshotted at apply time so your history stays intact even after jobs expire and get archived.

---

## UI Controls

| Button | What it does |
|---|---|
| **↻ SCRAPE** | Hits all configured sources for fresh data, merges into `jobs.json` |
| **↑ FROM FILE** | Loads `jobs.json` from disk instantly — no network calls |
| **HIDE APPLIED** | Hides jobs you've already marked as applied |
| **+ APPLIED** (on card) | Marks that job as applied; saves to `applied.json` |
| **UNDO** (on applied card) | Removes the application record |
| **SHOW ARCHIVE** (bottom) | Expands the archive panel showing expired jobs |

---

## API Endpoints

The Flask backend exposes these endpoints (useful for scripting or debugging):

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/jobs` | Returns cached jobs + metadata |
| `POST` | `/api/refresh` | Starts a background scrape |
| `POST` | `/api/reload` | Reloads `jobs.json` into cache without scraping |
| `GET` | `/api/status` | Returns scrape status, source errors, timings |
| `GET` | `/api/archive` | Returns archived jobs |
| `GET` | `/api/applied` | Returns all applied job records |
| `POST` | `/api/applied` | Marks a job as applied (`{job_id, title, firm, url}`) |
| `DELETE` | `/api/applied/<id>` | Removes an applied record |

---

## Role Tags

| Tag | Meaning | Examples |
|---|---|---|
| **QR** | Quantitative Researcher | "Quantitative Researcher", "QR Summer Associate" |
| **QD** | Quantitative Developer | "Quantitative Developer", "Quant Dev" |
| **SWE/QD** | Software Engineer | "Software Engineer", "Infrastructure Engineer" |
| **DE** | Data Engineer | "Data Engineer", "Data Infrastructure", "Platform Engineer" |
| **ML** | Machine Learning | "ML Engineer", "Machine Learning Researcher" |
| **Trading** | Trader roles | "Trader", "Trading Associate" |
| **Other** | Anything else that passed the relevance filter | "Risk Analyst", "Execution Strategist" |

---

## Size Management

`jobs.json` stays bounded automatically:

- After every scrape, jobs not seen in **60 days** are moved to `jobs_archive.json`
- Active jobs at any point are only those seen within the last 60 days
- In practice for ~200–300 active quant roles, `jobs.json` stays well under 1MB
- `jobs_archive.json` grows unboundedly but is never read at runtime — delete it anytime

To change the archive window, edit `ARCHIVE_DAYS = 60` in `app.py`.

---

## Troubleshooting

**"0 jobs from Jane Street / Two Sigma"**
Install Playwright: `pip install playwright && playwright install chromium`

**"HTTP 404" or "HTTP 403" error in the banner for a Greenhouse firm**
The slug is wrong or that firm no longer uses Greenhouse. Verify by visiting `https://boards.greenhouse.io/SLUG` in your browser.

**Jobs not updating after editing `firms.json`**
The scraper reloads `firms.json` on every scrape — no restart needed. Just click **↻ SCRAPE** in the UI.

**Server starts but shows 0 jobs**
`jobs.json` doesn't exist yet (first run). The server is running the initial scrape synchronously — wait ~60 seconds for it to complete.
