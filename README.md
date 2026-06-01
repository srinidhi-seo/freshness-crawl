# BikeDekho EEAT &amp; Freshness Monitor

Crawls every BikeDekho model URL, detects **when each page was last updated** and
**what content changed**, scores each page against Google's **E-E-A-T** framework,
and presents it all in a daily-refreshing dashboard with drill-down and filters.

## What you get

| File | What it is |
|---|---|
| `bikedekho_eeat_crawler.py` | The engine. Crawls, dates, diffs, and scores every URL. Outputs `data/data.json`, `data/data.csv`, and a dated history snapshot. |
| `dashboard.html` | The viewer. Reads `data.json`, shows KPIs, a sortable/filterable table, and a click-through EEAT breakdown per URL. Works standalone (embedded seed) or live (next to a real `data.json`). |
| `daily-crawl.yml` | The scheduler. A GitHub Actions workflow that runs the crawler every day, commits fresh data, and publishes the dashboard. This is what makes it "update every day." |
| `urls.txt` | Your URL list, one per line. |

## How the daily update actually works (read this)

A dashboard file by itself cannot crawl `bikedekho.com` from a browser (cross-origin
requests are blocked) and nothing in a static file runs on a schedule. So the design
splits into **engine + viewer**:

1. The **crawler** runs on a schedule on infrastructure you control and writes `data.json`.
2. The **dashboard** simply reads whatever `data.json` sits next to it.

Pick one place to run the crawler daily:

- **GitHub Actions (recommended, free):** put these files in a repo, drop
  `daily-crawl.yml` into `.github/workflows/`, and it runs every morning, commits the
  new data, and serves the dashboard via GitHub Pages. Zero servers to manage.
- **A cron box / VM:** `30 1 * * * cd /path && python bikedekho_eeat_crawler.py --urls urls.txt`
- **A cloud scheduler** (Cloud Scheduler + Cloud Run, Lambda + EventBridge, etc.).

## Quick start (local)

```bash
pip install requests beautifulsoup4 lxml python-dateutil

# test on the first 30 URLs
python bikedekho_eeat_crawler.py --urls urls.txt --limit 30

# full run
python bikedekho_eeat_crawler.py --urls urls.txt --workers 6 --delay 0.8

# view it
python -m http.server 8000   # then open http://localhost:8000/dashboard.html
```

Open `dashboard.html` directly (double-click) to see the seed preview without crawling.

## How last-updated is detected

The crawler tries the most reliable signal first and falls back:
1. `dateModified` / `datePublished` in the page's JSON-LD structured data
2. `<meta article:modified_time>` / `og:updated_time` / `lastmod`
3. Visible "Last updated on …" text on the page
4. HTTP `Last-Modified` response header

The winning source is recorded per URL (`last_updated_source`) so you can trust or
discount it. Age is bucketed into `fresh` (≤7d) · `recent` (≤30d) · `ageing` (≤90d) ·
`stale` (≤180d) · `very_stale`.

## How content-change is detected

Each run fingerprints the main editorial text (SHA-256) and records the inventory of
section headings + word count. The next run diffs against the stored snapshot and
reports **added sections, removed sections, and word delta** — that is your "what
content was added" answer, per URL, per day.

## The EEAT rubric (100 pts, fully transparent)

Every point is explained in the drill-down. Tune the weights at the top of the script.

- **Experience /20** — user/owner reviews section, review count, aggregate rating, owner comparisons.
- **Expertise /25** — expert verdict / road-test content, editorial word count, spec completeness, pros &amp; cons, variant pricing.
- **Authoritativeness /25** — internal-link depth, linked news, BreadcrumbList + FAQ schema, and **self-referencing canonical** (i.e. this URL is the indexable primary entity).
- **Trustworthiness /30** — HTTPS, **freshness** (heaviest single lever), Product + aggregateRating schema, transparent pricing, healthy meta description, minus penalties for `noindex` or bad HTTP status.

Pages are also **flagged** (not silently buried) when they are a secondary template
(`/images/…`, `/colors`, `bike-loan-emi-calculator`, `?amp=1`) or canonicalise to a
different URL — because a large share of your list does exactly that and should not be
judged as standalone pages.

## Notes &amp; etiquette

- The crawler respects `robots.txt`, sets a descriptive User-Agent, and throttles with
  `--delay`. Since you own BikeDekho, you can raise `--workers` / lower `--delay`, ideally
  crawling off-peak.
- For a list this size, the cleanest freshness feed is often BikeDekho's own XML sitemaps
  (`<lastmod>`); the crawler can be pointed at those to cut load. Ask if you want that mode.
- `data/data_full.json` keeps the content fingerprints needed for next-day diffing —
  don't delete it between runs.
