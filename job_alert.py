import json
import os
import time
from urllib.parse import quote_plus
import requests
from bs4 import BeautifulSoup

# ================= SEARCH CONFIGURATION =================
# List of all target job titles/keywords to monitor
SEARCH_QUERIES = [
    "Accounts Receivable",
    "AR Executive",
    "Billing Specialist",
    "Finance Associate",
    "Financial Analyst",
    "Order to Cash",
    "Credit and Collections",
    "Account Executive",
]

LOCATION = "Bengaluru, Karnataka, India"  # You can also change to "India" or "Remote"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
HISTORY_FILE = "seen_jobs.json"
# ========================================================


def send_telegram_alert(title, company, location, link, keyword):
  message = (
      f"🚨 *New Job Alert ({keyword})!*\n\n"
      f"📌 *Role:* {title}\n"
      f"🏢 *Company:* {company}\n"
      f"📍 *Location:* {location}\n\n"
      f"🔗 [Apply Here]({link})"
  )
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  payload = {
      "chat_id": TELEGRAM_CHAT_ID,
      "text": message,
      "parse_mode": "Markdown",
  }
  try:
    requests.post(url, json=payload, timeout=10)
  except Exception as e:
    print(f"Failed to send Telegram alert: {e}")


def load_seen_jobs():
  if os.path.exists(HISTORY_FILE):
    with open(HISTORY_FILE, "r") as f:
      return set(json.load(f))
  return set()


def save_seen_jobs(seen_ids):
  with open(HISTORY_FILE, "w") as f:
    json.dump(list(seen_ids), f)


def check_all_roles():
  seen_jobs = load_seen_jobs()
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/124.0.0.0 Safari/537.36"
      )
  }

  for query in SEARCH_QUERIES:
    print(f"Scanning for: {query}")
    url = (
        f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
        f"keywords={quote_plus(query)}&location={quote_plus(LOCATION)}&f_TPR=r3600&sortBy=DD"
    )

    try:
      response = requests.get(url, headers=headers, timeout=15)
      if response.status_code != 200:
        continue

      soup = BeautifulSoup(response.text, "html.parser")
      job_cards = soup.find_all("li")

      for card in job_cards:
        link_elem = card.find("a", class_="base-card__full-link")
        title_elem = card.find("h3", class_="base-search-card__title")
        company_elem = card.find("h4", class_="base-search-card__subtitle")
        location_elem = card.find("span", class_="job-search-card__location")

        if not link_elem:
          continue

        job_url = link_elem.get("href", "").split("?")[0]
        job_id = (
            job_url.split("-")[-1] if "-" in job_url else job_url.split("/")[-1]
        )

        if job_id not in seen_jobs:
          title = title_elem.text.strip() if title_elem else query
          company = company_elem.text.strip() if company_elem else "Company"
          location = location_elem.text.strip() if location_elem else LOCATION

          print(f"--> Found new job: {title} at {company}")
          send_telegram_alert(title, company, location, job_url, query)
          seen_jobs.add(job_id)

    except Exception as e:
      print(f"Error checking {query}: {e}")

    # Small pause between queries to avoid rate limits
    time.sleep(2)

  save_seen_jobs(seen_jobs)


if __name__ == "__main__":
  check_all_roles()
