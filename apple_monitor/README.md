# Apple Procurement Monitor

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)
![GCP](https://img.shields.io/badge/GCP-e2--micro-orange?logo=google-cloud&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

Automated daily monitoring of **muasamcong.mpi.gov.vn** — Vietnam's national public procurement portal — for Apple and IT-related government tenders. Each morning, a GCP VM crawls the portal, deduplicates results, runs AI analysis on every new bid, refreshes status for previously closed bids, and delivers an HTML digest with an Excel attachment to configured recipients.

---

## Features

- **Keyword-driven search** across 20+ Apple/IT terms (MacBook, iPad, iPhone, laptop, thiết bị CNTT, etc.)
- **Deduplication via Google Sheets** — skips any bid already recorded; no SQL server required
- **AI bid analysis** — Gemini Flash scores each new bid: product type, estimated value, deadline urgency, priority
- **Closed-bid refresh** — re-fetches status, winner, and award price for all previously-tracked bids with stale status, no date cap
- **Structured email digest** — HTML email with color-coded priority table and Excel attachment (two tabs: Active Bids sorted by value, Full Database)
- **Automated token refresh** — Playwright headless browser captures fresh session cookies without manual intervention
- **Resilient to government server quirks** — custom SSL adapter, randomized delays, graceful 401 handling
- **Fully resumable backfills** — Google Sheet as checkpoint; safe to kill and restart mid-run

---

## Architecture

```text
muasamcong.mpi.gov.vn
        │
        │  HTTPS POST (Elasticsearch API)
        │  Legacy SSL adapter (weak DH params)
        ▼
┌───────────────────┐
│   Crawler         │  paginated, keyword loop
│   apple_monitor.py│  randomized delays
└────────┬──────────┘
         │  raw bid list
         ▼
┌───────────────────┐
│   Deduplicator    │  check notifyId
│                   │──────────────────► Google Sheets (database)
│                   │  skip seen bids         (gspread)
└────────┬──────────┘
         │  new bids only
         ▼
┌───────────────────┐
│   AI Analyzer     │  Gemini 1.5 Flash
│                   │  product type, value,
│                   │  urgency, priority score
└────────┬──────────┘
         │  enriched bids
         ▼
┌───────────────────┐
│  Closed-Bid       │  re-fetch all stale bids
│  Refresher        │  update winner / award price
└────────┬──────────┘
         │  updated records
         ▼
┌───────────────────┐
│  Email Builder    │  HTML digest
│                   │  + Excel (openpyxl)
│                   │──► Gmail SMTP
└───────────────────┘

Runs daily at 06:00 ICT (23:00 UTC) via systemd on GCP e2-micro VM
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
├── backfill_urls.py              # One-time: fix 6800 portal URLs
├── backfill_status.py            # One-time: refresh all bid statuses
├── backfill_winner.py            # One-time: backfill winner/award data
├── reanalyze.py                  # Re-run AI analysis on existing bids
├── send_catchup.py               # Manual email trigger
├── run_monitor.py                # Entry point used by systemd
├── setup.sh                      # VM bootstrap script
├── requirements.txt
└── google_service_account.json   # GCP credentials (gitignored)
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

# Full run
python apple_monitor.py
```

---

## Deployment on GCP (e2-micro VM)

The `setup.sh` script handles VM bootstrap. After SSH-ing into the VM:

```bash
bash setup.sh                        # installs Python, deps, creates systemd service
sudo systemctl enable apple-monitor
sudo systemctl start apple-monitor
```

The systemd unit fires at **23:00 UTC (06:00 ICT)** daily. Logs are written to `monitor.log` and captured by `journald`.

To sync local changes to the VM:

```powershell
.\sync_to_vm.ps1   # rsync wrapper over SSH
```

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
