# Apple Procurement Monitor

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)
![GCP](https://img.shields.io/badge/GCP-e2--micro-orange?logo=google-cloud&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

Automated daily monitoring of **muasamcong.mpi.gov.vn** — Vietnam's national public procurement portal — for Apple and IT-related government tenders. Each morning a GCP VM crawls the portal at 3 AM VN, then sends an HTML digest with Excel attachment at 6 AM VN. A GitHub Actions workflow runs at 3:30 AM VN as a hot-standby backup in case the VM's IP is blocked.

---

## Features

- **Keyword-driven search** across 20+ Apple/IT terms (MacBook, iPad, iPhone, laptop, thiết bị CNTT, etc.)
- **Deduplication via Google Sheets** — skips any bid already recorded; no SQL server required
- **AI bid analysis** — Gemini Flash scores each new bid: product type, estimated value, deadline urgency, priority
- **Closed-bid refresh** — re-fetches status, winner, and award price for the 200 most-recently-closed stale bids per run (backlog of ~3,500 clears in ~17 days)
- **Structured email digest** — HTML email with color-coded priority table and Excel attachment (two tabs: Active Bids sorted by value, Full Database)
- **Automated token refresh** — Playwright headless browser captures fresh session cookies without manual intervention
- **Resilient to government server quirks** — custom SSL adapter, randomized delays, graceful 401 handling
- **Fully resumable backfills** — Google Sheet as checkpoint; safe to kill and restart mid-run

---

## Architecture

```text
                        03:00 VN (20:00 UTC)            03:30 VN (20:30 UTC)
                   ┌─── GCP VM run_crawl.py ───┐   ┌── GitHub Actions backup ──┐
                   │   (primary)               │   │   (hot-standby if VM IP   │
                   │                           │   │    is blocked by portal)   │
                   └────────────┬──────────────┘   └──────────────┬────────────┘
                                │                                 │
                                ▼  both paths converge here       ▼
                muasamcong.mpi.gov.vn  (HTTPS POST, Legacy SSL)
                                │
                    token_refresh.py   headless Chromium intercepts
                    (Playwright)       session token before each crawl
                                │
                                ▼
                ┌───────────────────────────┐
                │  Crawler                  │  paginated keyword loop
                │  apple_monitor.py crawl   │  randomized delays
                └─────────────┬─────────────┘
                              │  raw bid list
                              ▼
                ┌───────────────────────────┐
                │  Deduplicator             │  check notifyId ──► Google Sheets
                │                           │  skip seen bids      (database)
                └─────────────┬─────────────┘
                              │  new bids only
                              ▼
                ┌───────────────────────────┐
                │  AI Analyzer              │  Gemini Flash
                │                           │  product, value, urgency, priority
                └─────────────┬─────────────┘
                              │  enriched bids written to Sheet
                              ▼
                ┌───────────────────────────┐
                │  Closed-Bid Refresher     │  200 most-recent stale bids/run
                │                           │  re-fetch winner / award price
                └─────────────┬─────────────┘

        06:00 VN (23:00 UTC) — GCP VM run_monitor.py
                              │  reads today's Sheet records
                              ▼
                ┌───────────────────────────┐
                │  Email Builder            │  HTML digest + Excel (openpyxl)
                │                           │──► Gmail SMTP → recipients
                └───────────────────────────┘
```

---

## Tech Highlights

### 1. Legacy SSL Compatibility

The government portal's Elasticsearch endpoint uses weak Diffie-Hellman parameters that Python's default SSL rejects. A custom `LegacySSLAdapter` patches `SSLContext` with `SECLEVEL=0` and mounts it only for the target host, keeping the rest of the session at default security.

```python
class LegacySSLAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)
```

### 2. Dual-Token Session Architecture

The portal exposes two separate authentication layers:

- **Search API** (`/services/smart/search`) — accepts an anonymous public token; refreshed automatically
- **Goods detail endpoint** — requires an authenticated `JSESSIONID`; stable across days but must be manually rotated when the goods endpoint goes stale

`token_refresh.py` uses Playwright to open the portal in a headless Chromium browser, intercepts the first `smart/search` network request, extracts the fresh token and cookies, and patches `apple_monitor_config.py` in-place — no manual browser interaction needed for the search token.

### 3. Google Sheets as a Zero-Infrastructure Database

Rather than running a SQL server, the pipeline uses a single Google Sheet as both the persistent store and idempotency checkpoint. On each run, the full `notifyId` index is loaded once into a Python set, making deduplication O(1) per bid. The same sheet drives the closed-bid refresh queue and the Excel export.

### 4. Rate-Limiting and Graceful Degradation

Requests include randomized delays (`DELAY_MIN`–`DELAY_MAX` seconds) between pages and keywords. A `401` from the search endpoint aborts the crawl early with a clear log message and still proceeds to the email step with whatever data was already collected — recipients always receive a report even on partial runs.

### 5. Resumable Backfills

One-time backfill scripts (`backfill_urls.py`, `backfill_status.py`, `backfill_winner.py`) use the Google Sheet as a row-level checkpoint. Each row is marked as processed before moving to the next, so a crash at row 4,000 of 6,800 resumes from row 4,001 — no re-processing of already-updated records.

---

## Repository Layout

```text
apple_monitor/
├── apple_monitor.py              # Main pipeline (~1000 lines)
├── apple_monitor_config.example.py  # Config template (commit this)
├── apple_monitor_config.py       # Real secrets (gitignored)
├── token_refresh.py              # Playwright session capture
├── run_crawl.py                  # 3 AM VN entry point: token refresh + crawl + alert on failure
├── run_monitor.py                # 6 AM VN entry point: read Sheet, send email
├── backfill_urls.py              # One-time: fix 6800 portal URLs
├── backfill_status.py            # One-time: refresh all bid statuses
├── backfill_winner.py            # One-time: backfill winner/award data
├── reanalyze.py                  # Re-run AI analysis on existing bids
├── send_catchup.py               # Manual email trigger
├── setup.sh                      # VM bootstrap script
├── requirements.txt
└── google_service_account.json   # GCP credentials (gitignored)

.github/
└── workflows/
    └── backup-crawl.yml          # GitHub Actions: backup crawl at 3:30 AM VN
```

---

## Setup

### Prerequisites

- Python 3.10+
- A Google account (for Sheets + Gmail)
- A Google Cloud project (free tier sufficient)
- A Gemini API key (free tier at aistudio.google.com)
- Optional: GCP VM for unattended daily runs

### 1. Install Dependencies

```bash
git clone <repo-url>
cd apple_monitor
pip install -r requirements.txt
playwright install chromium   # only needed for token_refresh.py
```

### 2. Configure

```bash
cp apple_monitor_config.example.py apple_monitor_config.py
# Open apple_monitor_config.py and fill in all values (see sections below)
```

### 3. Google Sheets + Service Account

1. Create a new Google Sheet. Copy the **Sheet ID** from its URL:
   `docs.google.com/spreadsheets/d/<SHEET_ID>/edit`

2. In [console.cloud.google.com](https://console.cloud.google.com):
   - Enable **Google Sheets API** and **Google Drive API**
   - Create a **Service Account** under IAM & Admin
   - Generate a JSON key and save it as `google_service_account.json` in this folder

3. Share the Google Sheet with the service account email (`xxx@xxx.iam.gserviceaccount.com`) as **Editor**

4. Set in config:

   ```python
   GOOGLE_SHEET_ID         = "1BxiMVs0XRA..."
   GOOGLE_CREDENTIALS_FILE = "google_service_account.json"
   ```

### 4. Gmail App Password

1. Enable 2-Step Verification on your Google account
2. Go to **myaccount.google.com → Security → App Passwords**
3. Create an app password named "Apple Monitor"
4. Set in config:

   ```python
   GMAIL_SENDER       = "you@gmail.com"
   GMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"
   EMAIL_RECIPIENTS   = ["recipient@example.com"]
   ```

### 5. Gemini API Key

Get a free key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey):

```python
GEMINI_API_KEY = "AIza..."
```

### 6. muasamcong Session Token

**Automatic (recommended):**

```bash
python token_refresh.py
# Headless Chromium opens the portal, captures the token, and patches config automatically
```

**Manual fallback:**

1. Open Chrome → `muasamcong.mpi.gov.vn/web/guest/contractor-selection`
2. Press F12 → Network tab → filter Fetch/XHR
3. Perform any search
4. Find the POST to `/services/smart/search?token=...`
5. Right-click → Copy as cURL
6. Extract `token` and cookies (`JSESSIONID`, `NSC_WT_...`, `LFR_SESSION_...`) into config

> The search token expires with the browser session (~1-2 hours of inactivity). The pipeline prints `[401] Token expired` when a refresh is needed. `JSESSIONID` is stable for days and should only be manually replaced when the goods detail endpoint stops returning data.

---

## Running

```bash
# Test email only (no token/sheet needed)
python apple_monitor.py test

# Crawl only (no email) — used by 3 AM cron and GitHub Actions
python apple_monitor.py crawl

# Send email from today's already-crawled Sheet data (no crawl)
python apple_monitor.py send-only

# Full run (crawl + email in one step — for manual use)
python apple_monitor.py
```

---

## Deployment on GCP (e2-micro VM)

The `setup.sh` script handles VM bootstrap. After SSH-ing into the VM:

```bash
bash setup.sh   # installs Python 3.11, Playwright, deps
```

**Crontab (VM):**

```cron
# 03:00 VN (20:00 UTC) — crawl + token refresh + alert on failure
0 20 * * * cd /home/dphm57/apple_monitor && python3 run_crawl.py >> /home/dphm57/apple_monitor/monitor.log 2>&1

# 06:00 VN (23:00 UTC) — send email from today's Sheet data
0 23 * * * cd /home/dphm57/apple_monitor && python3 run_monitor.py >> /home/dphm57/apple_monitor/monitor.log 2>&1
```

Logs: `/home/dphm57/apple_monitor/monitor.log`

To sync local changes to the VM:

```powershell
.\sync_to_vm.ps1   # rsync wrapper over SSH
```

### GitHub Actions Backup

A second crawl runs at **03:30 AM VN (20:30 UTC)** via GitHub Actions (`.github/workflows/backup-crawl.yml`). It uses a GitHub-hosted runner with a different IP range, so it succeeds even when the GCP VM's IP is blocked by the portal. It only crawls (no email) — the 6 AM VM job sends the email from whatever data is in the Sheet.

**Required GitHub Secrets** (Settings → Secrets and variables → Actions):

| Secret | Content |
| --- | --- |
| `APPLE_MONITOR_CONFIG` | Full content of `apple_monitor_config.py` |
| `GOOGLE_CREDENTIALS_JSON` | Full content of `google_service_account.json` |

If VM is down entirely (no crawl AND no email): run `python apple_monitor.py send-only` locally to send the email manually.

---

## Security Notes

- `apple_monitor_config.py` is gitignored — never commit it
- `google_service_account.json` is gitignored — store securely
- `apple_monitor_config.example.py` contains only placeholder values and is safe to commit
- The Gmail App Password grants send-only access; use a dedicated sender account

---

## Cost Estimate

| Service | Usage | Cost |
| --- | --- | --- |
| Gemini 1.5 Flash | ~100 bids/day | ~$0.002/day |
| Google Sheets API | read/write daily | Free |
| Gmail SMTP | 1 email/day | Free |
| GCP e2-micro VM | 24/7 | ~$6/month (or free tier) |

---

## License

MIT
