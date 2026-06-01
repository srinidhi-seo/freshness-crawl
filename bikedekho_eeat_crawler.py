#!/usr/bin/env python3
"""
BikeDekho EEAT Freshness Crawler
================================

A Screaming-Frog / OnCrawl-style crawler purpose-built for BikeDekho model pages.
For every URL it answers the two questions you asked for:

  1. WHEN was the page last updated?       -> multi-strategy freshness detection
  2. WHAT content changed since last run?   -> content fingerprint + section diff

...and then scores every page against Google's E-E-A-T framework
(Experience, Expertise, Authoritativeness, Trustworthiness) with a fully
transparent, auditable rubric (every point is explained in the output).

It writes:
  - data/data.json          -> the file the dashboard reads (latest snapshot)
  - data/history/<date>.json-> one snapshot per run (for day-over-day diffing)
  - data/data.csv           -> flat export for Sheets / Excel / BigQuery load

Run it once by hand, then put it on a daily schedule (GitHub Actions cron,
system cron, or a cloud scheduler). See README.md.

Usage:
    # Preferred: pull the model list (and lastmod dates) straight from the sitemap
    python bikedekho_eeat_crawler.py --sitemap https://www.bikedekho.com/BikeModel.xml
    python bikedekho_eeat_crawler.py --sitemap <url> --workers 8 --delay 0.8
    python bikedekho_eeat_crawler.py --sitemap <url> --limit 50   # test run

    # Or fall back to a static file, one URL per line
    python bikedekho_eeat_crawler.py --urls urls.txt

Dependencies:
    pip install requests beautifulsoup4 lxml python-dateutil
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib import robotparser

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

# --------------------------------------------------------------------------- #
#  Config
# --------------------------------------------------------------------------- #

USER_AGENT = (
    "BikeDekho-EEAT-Auditor/1.0 (+internal SEO monitoring; contact: seo@bikedekho.com)"
)
DATA_DIR = "data"
HISTORY_DIR = os.path.join(DATA_DIR, "history")
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# EEAT weights. The four pillars sum to 100. Tune these in one place.
WEIGHTS = {
    "experience": 20,      # real-owner signals: reviews, owner photos, Q&A
    "expertise": 25,       # editorial depth: byline, spec completeness, pros/cons
    "authoritativeness": 25,  # links, news, structured data, canonical correctness
    "trustworthiness": 30,    # https, freshness, price accuracy, schema, indexability
}

# Freshness buckets in days -> label used by the dashboard filters.
FRESHNESS_BUCKETS = [
    (7, "fresh"),          # updated within a week
    (30, "recent"),        # within a month
    (90, "ageing"),        # within a quarter
    (180, "stale"),        # within ~6 months
    (10**9, "very_stale"), # older / unknown
]


# --------------------------------------------------------------------------- #
#  URL classification — critical because ~half the list is non-canonical
# --------------------------------------------------------------------------- #

def classify_url(url: str) -> str:
    """Bucket a URL by template so EEAT is scored fairly per page type."""
    p = urlparse(url).path.lower()
    q = urlparse(url).query.lower()
    if "/images/" in p or p.endswith(("-front-view", "-rear-view")):
        return "image"
    if p.endswith("/colors") or p.endswith("/colours"):
        return "colours"
    if "bike-loan-emi-calculator" in p:
        return "emi_tool"
    if p.endswith("/mileage"):
        return "mileage"
    if p.endswith("/range"):
        return "range"
    if p.endswith("/specifications") or p.endswith("/variants"):
        return "specs"
    if p.endswith("/connectivity"):
        return "feature_subpage"
    if "amp=1" in q or p.endswith(".html") and "?" in url:
        return "amp_or_param"
    # /brand/model  (two clean path segments) = a primary model page
    segs = [s for s in p.split("/") if s]
    if len(segs) == 2:
        return "model_page"
    if len(segs) == 1:
        return "listing_or_brand"
    return "other"


# --------------------------------------------------------------------------- #
#  Fetch
# --------------------------------------------------------------------------- #

def fetch(url: str, session: requests.Session, timeout: int = 25):
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
        return r, None
    except requests.RequestException as e:
        return None, str(e)


# --------------------------------------------------------------------------- #
#  Last-updated detection (multiple strategies, best wins)
# --------------------------------------------------------------------------- #

def _parse_date(value: str):
    try:
        dt = dateparser.parse(value, fuzzy=True)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, OverflowError, TypeError):
        return None


ONPAGE_DATE_RE = re.compile(
    r"(?:last\s+updated|updated\s+on|published\s+on|reviewed\s+on)\s*:?\s*"
    r"([0-9]{1,2}\s+\w+\s+[0-9]{4}|\w+\s+[0-9]{1,2},?\s+[0-9]{4})",
    re.IGNORECASE,
)


def detect_last_updated(resp, soup, jsonld_blocks, sitemap_lastmod=None):
    """Return (iso_date_or_None, source_label). Tries the most reliable first.

    The sitemap <lastmod> is treated as the most authoritative signal when present,
    because it is the site's own declaration of when the page last changed and it
    costs no extra request."""
    # 0) Sitemap <lastmod> — the site's own freshness declaration, most reliable
    if sitemap_lastmod:
        dt = _parse_date(sitemap_lastmod)
        if dt:
            return dt.date().isoformat(), "sitemap:lastmod"

    # 1) schema.org dateModified / datePublished inside JSON-LD
    for block in jsonld_blocks:
        for key in ("dateModified", "datePublished"):
            val = _deep_get(block, key)
            if val:
                dt = _parse_date(val if isinstance(val, str) else str(val))
                if dt:
                    return dt.date().isoformat(), f"jsonld:{key}"

    # 2) <meta> article:modified_time / og:updated_time / lastmod
    for attr, val in (("property", "article:modified_time"),
                      ("property", "og:updated_time"),
                      ("name", "lastmod"),
                      ("itemprop", "dateModified")):
        tag = soup.find("meta", attrs={attr: val})
        if tag and tag.get("content"):
            dt = _parse_date(tag["content"])
            if dt:
                return dt.date().isoformat(), f"meta:{val}"

    # 3) Visible on-page "Last updated on ..." text
    text = soup.get_text(" ", strip=True)
    m = ONPAGE_DATE_RE.search(text)
    if m:
        dt = _parse_date(m.group(1))
        if dt:
            return dt.date().isoformat(), "onpage_text"

    # 4) HTTP Last-Modified response header (weak but better than nothing)
    if resp is not None:
        lm = resp.headers.get("Last-Modified")
        if lm:
            dt = _parse_date(lm)
            if dt:
                return dt.date().isoformat(), "http_last_modified"

    return None, "none"


def _deep_get(obj, key):
    """Find first value of `key` anywhere in a nested dict/list (JSON-LD graphs)."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _deep_get(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _deep_get(item, key)
            if found is not None:
                return found
    return None


def freshness_bucket(last_updated_iso):
    if not last_updated_iso:
        return None, "very_stale"
    try:
        d = datetime.fromisoformat(last_updated_iso).replace(tzinfo=timezone.utc)
    except ValueError:
        return None, "very_stale"
    days = (datetime.now(timezone.utc) - d).days
    for limit, label in FRESHNESS_BUCKETS:
        if days <= limit:
            return days, label
    return days, "very_stale"


# --------------------------------------------------------------------------- #
#  Signal extraction (the raw inputs for EEAT scoring + content diffing)
# --------------------------------------------------------------------------- #

def extract_signals(url, resp, soup, jsonld_blocks):
    text = soup.get_text(" ", strip=True)
    words = len(text.split())

    schema_types = set()
    for block in jsonld_blocks:
        t = _deep_get(block, "@type")
        if isinstance(t, str):
            schema_types.add(t)
        elif isinstance(t, list):
            schema_types.update(str(x) for x in t)

    canonical_tag = soup.find("link", rel=lambda v: v and "canonical" in v)
    canonical = canonical_tag["href"].strip() if canonical_tag and canonical_tag.get("href") else None
    canonical_self = bool(canonical and canonical.rstrip("/") == url.rstrip("/"))

    robots = soup.find("meta", attrs={"name": re.compile("robots", re.I)})
    robots_content = (robots.get("content") or "").lower() if robots else ""
    noindex = "noindex" in robots_content

    # Rating / review-count (Experience signal)
    rating = None
    review_count = None
    m = re.search(r"\b([0-5]\.[0-9])\b\s*[\d,\.]*\s*(?:k\s*)?reviews?", text, re.I)
    if m:
        rating = float(m.group(1))
    m = re.search(r"([\d,\.]+\s*k?)\s*reviews?", text, re.I)
    if m:
        rc = m.group(1).lower().replace(",", "").strip()
        review_count = int(float(rc[:-1]) * 1000) if rc.endswith("k") else int(float(rc)) if rc.replace(".", "").isdigit() else None

    headings = [h.get_text(" ", strip=True) for h in soup.find_all(["h2", "h3"])]

    signals = {
        "http_status": resp.status_code if resp is not None else 0,
        "final_url": resp.url if resp is not None else url,
        "redirected": bool(resp is not None and resp.url.rstrip("/") != url.rstrip("/")),
        "https": (resp.url.startswith("https") if resp is not None else url.startswith("https")),
        "word_count": words,
        "canonical": canonical,
        "canonical_self": canonical_self,
        "noindex": noindex,
        "schema_types": sorted(schema_types),
        "has_product_schema": any("Product" in t for t in schema_types),
        "has_faq_schema": any("FAQ" in t for t in schema_types),
        "has_breadcrumb_schema": any("Breadcrumb" in t for t in schema_types),
        "has_aggregate_rating": bool(_deep_get(jsonld_blocks, "aggregateRating")),
        "rating": rating,
        "review_count": review_count,
        "has_reviews_section": bool(re.search(r"user review|owner.{0,10}review|write review", text, re.I)),
        "has_pros_cons": bool(re.search(r"things we like|pros|cons|things we (?:don.t|do not) like", text, re.I)),
        "has_faq_section": bool(re.search(r"frequently asked|faq", text, re.I)),
        "has_news_section": bool(re.search(r"\bnews\b|latest stories", text, re.I)),
        "has_comparison": bool(re.search(r"compare|comparison with similar", text, re.I)),
        "has_expert_overview": bool(re.search(r"expert review|our verdict|road test|first ride", text, re.I)),
        "has_price_table": bool(re.search(r"ex-?showroom|on road price", text, re.I)),
        "has_specs": bool(re.search(r"engine|mileage|kerb weight|displacement|specifications", text, re.I)),
        "internal_links": len([a for a in soup.find_all("a", href=True) if "bikedekho.com" in a["href"] or a["href"].startswith("/")]),
        "external_links": len([a for a in soup.find_all("a", href=True) if a["href"].startswith("http") and "bikedekho.com" not in a["href"]]),
        "image_count": len(soup.find_all("img")),
        "meta_description_len": len((soup.find("meta", attrs={"name": "description"}) or {}).get("content", "")) if soup.find("meta", attrs={"name": "description"}) else 0,
        "title": (soup.title.get_text(strip=True) if soup.title else ""),
        "headings": headings,
    }
    return signals


# --------------------------------------------------------------------------- #
#  Content fingerprint + diff ("what content was added")
# --------------------------------------------------------------------------- #

def content_fingerprint(soup):
    """Hash the main editorial text + the inventory of section headings."""
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    main = soup.get_text(" ", strip=True)
    sig = hashlib.sha256(main.encode("utf-8", "ignore")).hexdigest()
    sections = sorted({h.get_text(" ", strip=True).lower()
                       for h in soup.find_all(["h2", "h3"])})
    return sig, sections


def diff_content(prev, cur):
    """Compare today's signals against the previous snapshot for one URL."""
    if not prev:
        return {"is_new": True, "added_sections": cur["sections"],
                "removed_sections": [], "word_delta": cur["word_count"],
                "content_changed": True}
    added = [s for s in cur["sections"] if s not in set(prev.get("sections", []))]
    removed = [s for s in prev.get("sections", []) if s not in set(cur["sections"])]
    return {
        "is_new": False,
        "added_sections": added,
        "removed_sections": removed,
        "word_delta": cur["word_count"] - prev.get("word_count", 0),
        "content_changed": cur["fingerprint"] != prev.get("fingerprint"),
    }


# --------------------------------------------------------------------------- #
#  EEAT scoring — transparent rubric, every point explained
# --------------------------------------------------------------------------- #

def score_eeat(s, freshness_label, page_type):
    """Return dict with the four pillar scores (0..weight), total 0..100,
    and a human-readable reason list per pillar."""
    reasons = {"experience": [], "expertise": [], "authoritativeness": [], "trustworthiness": []}

    # ---- EXPERIENCE (real first-hand owner signals) ----
    exp = 0
    if s["has_reviews_section"]:
        exp += 6; reasons["experience"].append("Has a user/owner reviews section (+6)")
    if s["review_count"]:
        pts = min(8, s["review_count"] // 50)  # 1 pt per 50 reviews, cap 8
        exp += pts; reasons["experience"].append(f"{s['review_count']} owner reviews (+{pts})")
    if s["rating"]:
        exp += 3; reasons["experience"].append(f"Aggregate owner rating {s['rating']} shown (+3)")
    if s["has_comparison"]:
        exp += 3; reasons["experience"].append("Owner-comparison content present (+3)")
    exp = min(WEIGHTS["experience"], exp)

    # ---- EXPERTISE (editorial depth) ----
    expr = 0
    if s["has_expert_overview"]:
        expr += 7; reasons["expertise"].append("Expert verdict / road-test content (+7)")
    if s["word_count"] >= 800:
        expr += 6; reasons["expertise"].append(f"Substantial editorial text ({s['word_count']} words) (+6)")
    elif s["word_count"] >= 350:
        expr += 3; reasons["expertise"].append(f"Moderate editorial text ({s['word_count']} words) (+3)")
    else:
        reasons["expertise"].append(f"Thin content ({s['word_count']} words) (+0)")
    if s["has_specs"]:
        expr += 5; reasons["expertise"].append("Detailed specifications present (+5)")
    if s["has_pros_cons"]:
        expr += 4; reasons["expertise"].append("Pros & cons / things-we-like (+4)")
    if s["has_price_table"]:
        expr += 3; reasons["expertise"].append("Variant price table (+3)")
    expr = min(WEIGHTS["expertise"], expr)

    # ---- AUTHORITATIVENESS ----
    auth = 0
    if s["has_news_section"]:
        auth += 4; reasons["authoritativeness"].append("Linked news / latest stories (+4)")
    if s["internal_links"] >= 50:
        auth += 6; reasons["authoritativeness"].append(f"Strong internal linking ({s['internal_links']}) (+6)")
    elif s["internal_links"] >= 15:
        auth += 3; reasons["authoritativeness"].append(f"Some internal linking ({s['internal_links']}) (+3)")
    if s["has_breadcrumb_schema"]:
        auth += 4; reasons["authoritativeness"].append("BreadcrumbList structured data (+4)")
    if s["has_faq_section"] or s["has_faq_schema"]:
        auth += 4; reasons["authoritativeness"].append("FAQ content/markup (+4)")
    if s["canonical_self"]:
        auth += 7; reasons["authoritativeness"].append("Self-referencing canonical = indexable primary entity (+7)")
    elif s["canonical"]:
        reasons["authoritativeness"].append("Canonical points to a DIFFERENT URL — authority consolidated elsewhere (+0)")
    auth = min(WEIGHTS["authoritativeness"], auth)

    # ---- TRUSTWORTHINESS ----
    trust = 0
    if s["https"]:
        trust += 3; reasons["trustworthiness"].append("Served over HTTPS (+3)")
    # Freshness is the heaviest trust lever
    fresh_pts = {"fresh": 12, "recent": 9, "ageing": 5, "stale": 2, "very_stale": 0}.get(freshness_label, 0)
    trust += fresh_pts
    reasons["trustworthiness"].append(f"Freshness = '{freshness_label}' (+{fresh_pts})")
    if s["has_product_schema"]:
        trust += 4; reasons["trustworthiness"].append("Product structured data (+4)")
    if s["has_aggregate_rating"]:
        trust += 3; reasons["trustworthiness"].append("aggregateRating in schema (+3)")
    if s["has_price_table"]:
        trust += 3; reasons["trustworthiness"].append("Transparent pricing (+3)")
    if s["meta_description_len"] >= 70:
        trust += 2; reasons["trustworthiness"].append("Healthy meta description (+2)")
    if s["noindex"]:
        trust -= 5; reasons["trustworthiness"].append("Page is noindex (-5)")
    if s["http_status"] >= 400 or s["http_status"] == 0:
        trust -= 10; reasons["trustworthiness"].append(f"Bad HTTP status {s['http_status']} (-10)")
    trust = max(0, min(WEIGHTS["trustworthiness"], trust))

    total = exp + expr + auth + trust

    # Non-canonical / tool pages are penalised because they are not the
    # indexable entity Google ranks — flagged, not silently low.
    flags = []
    if page_type in ("image", "colours", "emi_tool", "amp_or_param", "feature_subpage"):
        flags.append(f"Secondary template ('{page_type}') — usually canonicalised to parent")
    if s["canonical"] and not s["canonical_self"]:
        flags.append("Canonicalised to another URL")
    if s["word_count"] < 200:
        flags.append("Thin content (<200 words)")

    return {
        "experience": exp,
        "expertise": expr,
        "authoritativeness": auth,
        "trustworthiness": trust,
        "total": total,
        "grade": _grade(total),
        "reasons": reasons,
        "flags": flags,
    }


def _grade(total):
    if total >= 80: return "A"
    if total >= 65: return "B"
    if total >= 50: return "C"
    if total >= 35: return "D"
    return "F"


# --------------------------------------------------------------------------- #
#  Per-URL pipeline
# --------------------------------------------------------------------------- #

def process_url(url, session, prev_snapshot, sitemap_lastmod=None):
    resp, err = fetch(url, session)
    page_type = classify_url(url)
    record = {"url": url, "page_type": page_type, "crawled_at": TODAY}

    if err or resp is None:
        # Even on a fetch failure, the sitemap may still tell us the page's age.
        lu, lu_src = (None, "none")
        if sitemap_lastmod:
            dt = _parse_date(sitemap_lastmod)
            if dt:
                lu, lu_src = dt.date().isoformat(), "sitemap:lastmod"
        days_old, fresh_label = freshness_bucket(lu)
        record.update({
            "status": "error", "error": err, "http_status": 0,
            "last_updated": lu, "last_updated_source": lu_src,
            "days_since_update": days_old, "freshness_label": fresh_label,
            "eeat": None, "diff": {"is_new": prev_snapshot is None, "content_changed": False},
        })
        return record

    soup = BeautifulSoup(resp.text, "lxml")

    jsonld_blocks = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            jsonld_blocks.append(json.loads(tag.string or "{}"))
        except (json.JSONDecodeError, TypeError):
            continue

    last_updated, lu_source = detect_last_updated(resp, soup, jsonld_blocks, sitemap_lastmod)
    days_old, fresh_label = freshness_bucket(last_updated)
    signals = extract_signals(url, resp, soup, jsonld_blocks)
    fingerprint, sections = content_fingerprint(BeautifulSoup(resp.text, "lxml"))

    cur_content = {"fingerprint": fingerprint, "sections": sections, "word_count": signals["word_count"]}
    prev_content = (prev_snapshot or {}).get("_content") if prev_snapshot else None
    diff = diff_content(prev_content, cur_content)

    eeat = score_eeat(signals, fresh_label, page_type)

    record.update({
        "status": "ok",
        "http_status": signals["http_status"],
        "title": signals["title"],
        "last_updated": last_updated,
        "last_updated_source": lu_source,
        "days_since_update": days_old,
        "freshness_label": fresh_label,
        "word_count": signals["word_count"],
        "canonical": signals["canonical"],
        "canonical_self": signals["canonical_self"],
        "noindex": signals["noindex"],
        "rating": signals["rating"],
        "review_count": signals["review_count"],
        "schema_types": signals["schema_types"],
        "internal_links": signals["internal_links"],
        "eeat": eeat,
        "diff": diff,
        "_content": cur_content,   # kept for next run's diff; stripped from dashboard file
    })
    return record


# --------------------------------------------------------------------------- #
#  Orchestration
# --------------------------------------------------------------------------- #

def fetch_sitemap(sitemap_url, session, include_hindi=False):
    """Fetch a sitemap and return {clean_url: lastmod_iso_or_None}.

    BikeDekho's BikeModel.xml lists every model twice — once as /hi/<...> (Hindi)
    and once as the clean English /<brand>/<model>. We drop the /hi/ duplicates so
    each model is scored exactly once, and use <lastmod> as the freshness source."""
    try:
        r = session.get(sitemap_url, timeout=40)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"ERROR: could not fetch sitemap {sitemap_url}: {e}", file=sys.stderr)
        return {}

    soup = BeautifulSoup(r.content, "xml")
    out = {}
    for url_tag in soup.find_all("url"):
        loc = url_tag.find("loc")
        if not loc or not loc.text:
            continue
        loc_url = loc.text.strip()
        if "/hi/" in loc_url and not include_hindi:   # skip Hindi locale duplicates
            continue
        lastmod_tag = url_tag.find("lastmod")
        out[loc_url] = lastmod_tag.text.strip() if lastmod_tag and lastmod_tag.text else None
    return out


def load_prev_snapshot():
    path = os.path.join(DATA_DIR, "data_full.json")
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        return {row["url"]: row for row in data.get("pages", [])}
    return {}


def robots_allowed(urls):
    rp = robotparser.RobotFileParser()
    try:
        rp.set_url("https://www.bikedekho.com/robots.txt")
        rp.read()
        return {u: rp.can_fetch(USER_AGENT, u) for u in urls}
    except Exception:
        return {u: True for u in urls}


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--sitemap", help="Sitemap URL (preferred). e.g. https://www.bikedekho.com/BikeModel.xml")
    src.add_argument("--urls", help="Static text file, one URL per line (fallback)")
    ap.add_argument("--include-hindi", action="store_true", help="Also crawl /hi/ locale URLs (default: skip)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--delay", type=float, default=0.8, help="Seconds between requests per worker")
    ap.add_argument("--limit", type=int, default=0, help="Crawl only first N (testing)")
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"})

    # url -> sitemap lastmod (None when sourced from a static file)
    lastmod_map = {}
    if args.sitemap:
        lastmod_map = fetch_sitemap(args.sitemap, session, include_hindi=args.include_hindi)
        urls = list(lastmod_map.keys())
        print(f"Sitemap parsed: {len(urls)} model URLs (Hindi duplicates skipped).")
        if not urls:
            print("ERROR: sitemap returned no URLs — aborting.", file=sys.stderr)
            sys.exit(1)
    else:
        with open(args.urls) as f:
            urls = [ln.strip() for ln in f if ln.strip() and ln.startswith("http")]

    if args.limit:
        urls = urls[:args.limit]

    os.makedirs(HISTORY_DIR, exist_ok=True)
    prev = load_prev_snapshot()
    allowed = robots_allowed(urls)

    results = []
    def worker(u):
        if not allowed.get(u, True):
            return {"url": u, "status": "blocked_by_robots", "eeat": None,
                    "page_type": classify_url(u), "last_updated": None,
                    "last_updated_source": "none", "freshness_label": "very_stale",
                    "diff": {"is_new": False, "content_changed": False}}
        time.sleep(args.delay)
        return process_url(u, session, prev.get(u), sitemap_lastmod=lastmod_map.get(u))

    print(f"Crawling {len(urls)} URLs with {args.workers} workers ...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, rec in enumerate(ex.map(worker, urls), 1):
            results.append(rec)
            if i % 25 == 0:
                print(f"  {i}/{len(urls)} done")

    # Full file (keeps _content for next run's diff)
    full = {"generated_at": datetime.now(timezone.utc).isoformat(), "pages": results}
    with open(os.path.join(DATA_DIR, "data_full.json"), "w") as f:
        json.dump(full, f)

    # Dashboard file (strip the heavy _content blob)
    dash_pages = []
    for r in results:
        r2 = {k: v for k, v in r.items() if k != "_content"}
        dash_pages.append(r2)
    summary = build_summary(dash_pages)
    dash = {"generated_at": full["generated_at"], "summary": summary, "pages": dash_pages}
    with open(os.path.join(DATA_DIR, "data.json"), "w") as f:
        json.dump(dash, f, indent=None)
    with open(os.path.join(HISTORY_DIR, f"{TODAY}.json"), "w") as f:
        json.dump(dash, f)

    write_csv(dash_pages)
    print("Done. Wrote data/data.json, data/data.csv and history snapshot.")
    print(json.dumps(summary, indent=2))


def build_summary(pages):
    scored = [p for p in pages if p.get("eeat")]
    no_score = [p for p in pages if not p.get("eeat")]
    avg = round(sum(p["eeat"]["total"] for p in scored) / len(scored), 1) if scored else 0
    by_fresh = {}
    for p in pages:
        by_fresh[p.get("freshness_label", "very_stale")] = by_fresh.get(p.get("freshness_label", "very_stale"), 0) + 1
    by_grade = {}
    for p in scored:
        g = p["eeat"]["grade"]
        by_grade[g] = by_grade.get(g, 0) + 1
    changed = [p for p in pages if p.get("diff", {}).get("content_changed")]
    return {
        "total_urls": len(pages),
        "scored": len(scored),
        "no_eeat_score": len(no_score),
        "avg_eeat": avg,
        "content_changed_today": len(changed),
        "by_freshness": by_fresh,
        "by_grade": by_grade,
        "stale_or_worse": sum(by_fresh.get(k, 0) for k in ("stale", "very_stale")),
    }


def write_csv(pages):
    cols = ["url", "page_type", "http_status", "last_updated", "freshness_label",
            "days_since_update", "word_count", "canonical_self", "rating",
            "review_count", "eeat_total", "grade", "content_changed"]
    with open(os.path.join(DATA_DIR, "data.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for p in pages:
            e = p.get("eeat") or {}
            w.writerow([
                p.get("url"), p.get("page_type"), p.get("http_status"),
                p.get("last_updated"), p.get("freshness_label"),
                p.get("days_since_update"), p.get("word_count"),
                p.get("canonical_self"), p.get("rating"), p.get("review_count"),
                e.get("total"), e.get("grade"),
                p.get("diff", {}).get("content_changed"),
            ])


if __name__ == "__main__":
    main()
