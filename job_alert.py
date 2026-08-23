#!/usr/bin/env python3
"""
LinkedIn job alert -> Telegram
  - Multiple search queries (coverage), deduped via seen_jobs.json
  - f_TPR lookback window OVERLAPS the cron interval so queue delays never drop jobs
  - Word-boundary regex include/exclude filtering (no substring false hits)
  - Phrase-based excludes so "Sales Tax Analyst" survives a "sales executive" block
  - seen_jobs.json pruning so the cache doesn't grow forever
  - Retry/backoff + polite pacing to avoid LinkedIn 429s
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- config ---

LOCATION = "Bengaluru, Karnataka, India"

# Each query is a separate LinkedIn search. Keep them short & specific.
SEARCH_QUERIES = [
    "accounts receivable",
    "order to cash",
    "billing analyst",
    "credit control",
    "collections analyst",
    "cash application",
    "revenue accountant",
    "finance analyst",
    "invoice to cash",
    "indirect tax",          # remove if you don't want tax roles
]

# Lookback window. With a 10-minute cron, r1800 (30 min) gives a 3x overlap.
TIME_WINDOW = "r86400"        # seconds; r900=15min, r1800=30min, r3600=1h
PAGES_PER_QUERY = 2          # 25 results per page
MAX_ALERTS_PER_RUN = 25      # safety valve if the cache is ever cleared

SEEN_FILE = "seen_jobs.json"
SEEN_MAX_AGE_DAYS = 45

# --- Title filtering --------------------------------------------------------
# A title passes if it matches >=1 INCLUDE pattern and 0 EXCLUDE patterns.
# All patterns are case-insensitive regex with word boundaries.

INCLUDE_PATTERNS = [
    r"\breceivables?\b",
    r"\ba\.?r\.?\b",                      # AR / A.R. as a whole word only
    r"\bbilling\b",
    r"\bo2c\b|\botc\b|\border[- ]to[- ]cash\b|\binvoice[- ]to[- ]cash\b",
    r"\bcollections?\b",
    r"\bcredit\s+control(ler)?\b",
    r"\binvoic(e|ing)\b",
    r"\bcash\s+application\b",
    r"\brevenue\s+(accountant|analyst|assurance|operations)\b",
    r"\bfinanc(e|ial)\s+(analyst|executive|operations)\b",
    r"\baccounts?\s+(executive|analyst|officer)\b",
    r"\b(indirect|sales|us)\s+tax\b",     # tax roles; delete line to disable
    r"\brecord\s+to\s+report\b|\br2r\b",  # adjacent; delete if unwanted
]

# Phrase-based excludes. Deliberately NOT bare words like "sales" or "credit",
# which would kill "Sales Tax Analyst" or "Credit Controller".
EXCLUDE_PATTERNS = [
    r"\bsales\s+(executive|manager|officer|representative|associate|development)\b",
    r"\bbusiness\s+development\b",
    r"\bsoftware\b|\bdeveloper\b|\bengineer(ing)?\b|\bsre\b|\bdevops\b",
    r"\bitsm\b|\bservicenow\b|\bsupport\s+engineer\b",
    r"\breal\s+estate\b|\bbanquet\b|\bhospitality\b|\bchef\b|\bhotel\b",
    r"\bar\s+caller\b|\bvoice\s+process\b|\bmedical\s+billing\b|\bbpo\b",
    r"\btele\s*(caller|sales|marketing)\b",
    r"\bintern(ship)?\b",                 # remove if internships are OK
    r"\brecruit(er|ment)\b|\bhr\b",
    r"\baccounts?\s+payable\b|\bp2p\b|\bprocure[- ]to[- ]pay\b",  # AP side
]

INCLUDE_RE = [re.compile(p, re.I) for p in INCLUDE_PATTERNS]
EXCLUDE_RE = [re.compile(p, re.I) for p in EXCLUDE_PATTERNS]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"


# ---------------------------------------------------------------- helpers ---

def title_passes(title: str) -> bool:
    t = " ".join(title.split())
    if any(rx.search(t) for rx in EXCLUDE_RE):
        return False
    return any(rx.search(t) for rx in INCLUDE_RE)


def load_seen() -> dict:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            data = json.load(f)
        # migrate old list-format caches
        if isinstance(data, list):
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            data = {jid: today for jid in data}
        return data
    return {}


def save_seen(seen: dict) -> None:
    cutoff = datetime.now(timezone.utc).timestamp() - SEEN_MAX_AGE_DAYS * 86400
    pruned = {}
    for jid, ds in seen.items():
        try:
            ts = datetime.strptime(ds, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            ts = 0
        if ts >= cutoff:
            pruned[jid] = ds
    with open(SEEN_FILE, "w") as f:
        json.dump(pruned, f, indent=0, sort_keys=True)


def fetch_page(query: str, start: int):
    params = {
        "keywords": query,
        "location": LOCATION,
        "f_TPR": TIME_WINDOW,
        "start": start,
        "sortBy": "DD",  # date descending
    }
    for attempt in range(3):
        try:
            r = requests.get(SEARCH_URL, params=params, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                return r.text
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            if r.status_code == 400:   # start beyond available results
                return ""
        except requests.RequestException:
            time.sleep(5 * (attempt + 1))
    return ""


def parse_jobs(html_text: str):
    soup = BeautifulSoup(html_text, "html.parser")
    jobs = []
    for card in soup.select("div.base-card, li"):
        urn = card.get("data-entity-urn") or ""
        a = card.select_one("a.base-card__full-link") or card.select_one("a[href*='/jobs/view/']")
        title_el = card.select_one("h3.base-search-card__title")
        company_el = card.select_one("h4.base-search-card__subtitle")
        loc_el = card.select_one("span.job-search-card__location")
        time_el = card.select_one("time")
        if not (a and title_el):
            continue
        link = a.get("href", "").split("?")[0]
        job_id = urn.split(":")[-1] if urn else (
            re.search(r"/jobs/view/[^/]*?(\d+)", link).group(1)
            if re.search(r"/jobs/view/[^/]*?(\d+)", link) else link
        )
        jobs.append({
            "id": job_id,
            "title": title_el.get_text(strip=True),
            "company": company_el.get_text(strip=True) if company_el else "?",
            "location": loc_el.get_text(strip=True) if loc_el else "",
            "posted": time_el.get_text(strip=True) if time_el else "",
            "link": link,
        })
    return jobs


def send_telegram(text: str) -> bool:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(3):
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code == 200:
            return True
        if r.status_code == 429:
            retry = r.json().get("parameters", {}).get("retry_after", 5)
            time.sleep(retry + 1)
            continue
        time.sleep(3)
    return False


# ------------------------------------------------------------------- main ---

def main():
    seen = load_seen()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    collected, filtered_out = {}, 0

    for query in SEARCH_QUERIES:
        for page in range(PAGES_PER_QUERY):
            html_text = fetch_page(query, page * 25)
            if not html_text:
                break
            page_jobs = parse_jobs(html_text)
            if not page_jobs:
                break
            for job in page_jobs:
                if job["id"] in seen or job["id"] in collected:
                    continue
                if not title_passes(job["title"]):
                    filtered_out += 1
                    seen[job["id"]] = today   # don't re-evaluate noise next run
                    continue
                collected[job["id"]] = job
            time.sleep(2)  # be polite between requests

    new_jobs = list(collected.values())[:MAX_ALERTS_PER_RUN]
    print(f"queries={len(SEARCH_QUERIES)} new={len(new_jobs)} filtered={filtered_out}")

    import html as html_mod
    for job in new_jobs:
        msg = (
            f"<b>{html_mod.escape(job['title'])}</b>\n"
            f"{html_mod.escape(job['company'])} — {html_mod.escape(job['location'])}\n"
            f"Posted: {html_mod.escape(job['posted'])}\n"
            f"<a href=\"{job['link']}\">Apply on LinkedIn</a>"
        )
        if send_telegram(msg):
            seen[job["id"]] = today
        time.sleep(1.2)  # Telegram rate limit headroom

    save_seen(seen)


if __name__ == "__main__":
    sys.exit(main())
