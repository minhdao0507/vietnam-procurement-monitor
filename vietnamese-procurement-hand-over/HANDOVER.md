# Vietnam Government Procurement Monitor — Handover Document

**Owner:** Apple Vietnam team  
**Built:** May 2026  
**Status:** Live — GCP VM crawls at 03:00 VN, sends email at 06:00 VN; GitHub Actions hot-standby backup crawls at 03:30 VN

> ⚠️ **Commit checklist — KHÔNG đưa lên git:**
> - VM external IP
> - Tên user / home path cụ thể trên VM
> - Email recipients (Apple employees)
> - Bất kỳ credential, token, password, API key nào
> - `apple_monitor_config.py`, `google_service_account.json` (đã có trong `.gitignore`)

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [File Structure](#file-structure)
4. [Setup Guide](#setup-guide)
5. [Configuration](#configuration)
6. [How Each Component Works](#how-each-component-works)
7. [Scheduled Tasks](#scheduled-tasks)
8. [Known Limitations](#known-limitations)
9. [Troubleshooting](#troubleshooting)
10. [Development Roadmap](#development-roadmap)

---

## Overview

An automated pipeline that monitors Vietnam's national government e-procurement portal ([muasamcong.mpi.gov.vn](https://muasamcong.mpi.gov.vn)) for public tenders involving Apple products and IT equipment.

**End-to-end flow:**

```
03:00 VN — GCP VM (primary)          03:30 VN — GitHub Actions (backup)
        │                                      │
        └──────────────┬────────────────────────┘
                       │  both run same crawl path
                       ▼
muasamcong.mpi.gov.vn (Vietnam national procurement portal)
        │
        ▼
[token_refresh.py]  ←  headless Chromium auto-refreshes session token
        │
        ▼
[apple_monitor.py crawl]  ←  fetch all pages per keyword, deduplicate
        │
        ▼  only new bids (not seen before)
[Google Sheets]     ←  permanent database (11,500+ records and growing)
        │
        ▼  active bids only (deadline not passed)
[Gemini Flash]      ←  AI analysis per bid
        │
        ▼  also refreshes 200 most-recent stale bids per run
[Google Sheets]     ←  updated winner / award price for closed bids

06:00 VN — GCP VM run_monitor.py
        │  reads today's Sheet records (send-only, no crawl)
        ▼
[Gmail SMTP]        ←  email to recipients
        + Excel attachment: Tab 1 = All Active, Tab 2 = Full Archive
```

**Schedule:**

- 03:00 VN (`0 20 * * *` UTC) — VM crawl via `run_crawl.py`
- 03:30 VN (`30 20 * * *` UTC) — GitHub Actions backup crawl
- 06:00 VN (`0 23 * * *` UTC) — VM email via `run_monitor.py`

**Recipients:** _(configured in `apple_monitor_config.py` — not committed)_

---

## Architecture

| Component | Technology | Purpose |
|---|---|---|
| **Crawler** | Python + `requests` with LegacySSLAdapter | Fetches bids from portal REST API |
| **Token refresh** | Playwright + headless Chrome | Auto-refreshes the session token before each run |
| **Deduplication** | Google Sheets API (`gspread`) | Prevents duplicate alerts across runs |
| **AI Analysis** | Gemini 2.5 Flash Lite (paid tier) | Summarizes each new bid: product, value, deadline, priority |
| **Email delivery** | Gmail SMTP + `openpyxl` | Sends HTML alert email + Excel attachment |
| **Scheduler** | GCP VM cron + GitHub Actions | VM: 20:00 UTC (crawl) + 23:00 UTC (email); GH Actions: 20:30 UTC (backup crawl) |
| **Secret Manager** | GCP Secret Manager | Stores `GMAIL_APP_PASSWORD`, `GEMINI_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON` — no plaintext credentials on disk |

---

## File Structure

```
apple_monitor/
├── apple_monitor.py              # Main pipeline (crawl → dedup → analyze → sheet)
├── apple_monitor_config.py       # All credentials and settings (keep secret)
├── token_refresh.py              # Auto-refresh muasamcong session token via Chrome
├── run_crawl.py                  # 03:00 VN entry point: token refresh + crawl; alert email on failure
├── run_monitor.py                # 06:00 VN entry point: read Sheet, send email (no crawl)
├── send_catchup.py               # One-time catch-up email (e.g. for new recipients)
├── requirements.txt              # Python dependencies
├── google_service_account.json   # Google Cloud service account (keep secret)
└── monitor.log                   # Runtime log

.github/
└── workflows/
    └── backup-crawl.yml          # GitHub Actions backup crawl at 03:30 VN
```

---

## Setup Guide

### 1. Prerequisites

- Python 3.10+ installed
- Google Chrome installed (used by Playwright for token refresh)
- Windows machine (for Task Scheduler)

### 2. Install dependencies

```bash
cd apple_monitor
pip install -r requirements.txt
playwright install chrome
```

### 3. Configure credentials

Edit `apple_monitor_config.py` and fill in:

- `GMAIL_SENDER` / `GMAIL_APP_PASSWORD` — Gmail account + App Password
- `EMAIL_RECIPIENTS` — list of recipient emails
- `GOOGLE_SHEET_ID` — from the Google Sheet URL
- `GOOGLE_CREDENTIALS_FILE` — path to service account JSON
- `GEMINI_API_KEY` — from [Google AI Studio](https://aistudio.google.com/apikey)
- `API_TOKEN` / `API_COOKIES` — from muasamcong (see Token section below)

### 4. First run

```bash
python run_monitor.py
```

### 5. Set up Task Scheduler

Run this PowerShell script as administrator (or see the Scheduled Tasks section):

```powershell
$taskName = "AppleProcurementMonitor"
$batPath  = "C:\path\to\apple_monitor\run_monitor.bat"
$workDir  = "C:\path\to\apple_monitor"

$triggers = @(
    (New-ScheduledTaskTrigger -Daily -At "08:00"),
    (New-ScheduledTaskTrigger -Daily -At "10:00"),
    (New-ScheduledTaskTrigger -Daily -At "12:00"),
    (New-ScheduledTaskTrigger -Daily -At "14:00"),
    (New-ScheduledTaskTrigger -Daily -At "16:00"),
    (New-ScheduledTaskTrigger -Daily -At "18:00")
)
$action   = New-ScheduledTaskAction -Execute $batPath -WorkingDirectory $workDir
$settings = New-ScheduledTaskSettingsSet -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew -StartWhenAvailable

Register-ScheduledTask -TaskName $taskName -Trigger $triggers -Action $action -Settings $settings
```

---

## Configuration

All settings live in `apple_monitor_config.py`.

### Keywords (21 total)

```python
KEYWORDS = [
    # Apple-specific
    "macbook", "imac", "mac mini", "mac pro",
    "iphone", "ipad", "airpods", "apple watch",
    # General IT (Vietnamese)
    "máy tính", "máy tính xách tay", "máy tính bảng", "laptop",
    "di động", "điện tử", "thiết bị cntt",
    # Branded
    "điện thoại apple", "máy tính bảng apple",
    # English terms
    "tablet", "smartphone",
]
```

### API Token

The portal requires a browser session token (`API_TOKEN`) + cookies (`JSESSIONID`, `LFR_SESSION_STATE_20103`).

**Automatic refresh:** `token_refresh.py` launches headless Chrome, navigates to the portal, and intercepts the first API call to extract a fresh token. This runs automatically before every scheduled run.

**Manual refresh (fallback):**
1. Open Chrome → go to `muasamcong.mpi.gov.vn`
2. F12 → Network tab → find any POST to `/services/smart/search`
3. Right-click → Copy as cURL
4. Extract `token=` from the URL and `JSESSIONID`, `LFR_SESSION_STATE_20103` from cookies
5. Update `apple_monitor_config.py`

**Stable cookie (never changes):** `NSC_WT_QSE_QPSUBM_NTD_NQJ`

---

## How Each Component Works

### `token_refresh.py`

1. Tries to launch real Chrome (`channel="chrome"`) headless — less detectable than Playwright's bundled Chromium
2. Falls back to Edge, then Playwright Chromium
3. Navigates to the portal, waits 10s for JS to boot
4. Intercepts the first `smart/search` network request to extract the token
5. If no token captured, tries clicking search button / filling input / pressing Enter
6. If Playwright fails entirely, falls back to `_test_current_token()` — checks if the existing token in config is still valid
7. Patches `apple_monitor_config.py` in-place with the new token + cookies

### `run_crawl.py`

Runs at 03:00 VN. Three steps:

1. **Token refresh** — runs `token_refresh.py` via subprocess; exits with alert email if it fails
2. **Crawl** — runs `apple_monitor.py crawl`; exits with alert email if it fails
3. **Secret sync** — pushes fresh `apple_monitor_config.py` to GitHub Secret `APPLE_MONITOR_CONFIG` via `gh` CLI, so the GitHub Actions backup always has an up-to-date token. Non-blocking — a failure here only prints a warning.

> `gh` CLI is authenticated on the VM with a Fine-grained PAT (repo: `vietnam-procurement-monitor`, permission: `secrets:write`). PAT stored in `~/.config/gh/hosts.yml`.

### `apple_monitor.py`

**Key technical details:**

- **SSL:** The portal uses a weak DH key. `LegacySSLAdapter` sets `DEFAULT:@SECLEVEL=0` ciphers and disables cert verification.
- **API endpoint:** `POST /o/egp-portal-contractor-selection-v2/services/smart/search?token=<TOKEN>`
- **Pagination:** 50 records per page, iterates all pages per keyword with 1.5–3.0s random delay
- **Deduplication:** Two-tier — cross-run via Google Sheet IDs + cross-keyword via in-memory `seen_ids` set
- **Analysis:** Only active bids (deadline not passed) are sent to Gemini, to conserve API calls
- **Status tracking:** `refresh_recently_closed()` re-fetches status/winner/award price for the 200 most-recently-closed stale bids per run (sorted newest first; backlog of ~3,500 clears in ~17 days)
- **Status map:** English labels — `Open`, `Bidding Open`, `Under Evaluation`, `Technical Evaluation`, `Result Published`, `Invitation Published`, `Cancelled`
- **Number formatting:** Prices displayed as `1,50B VND` (Vietnamese decimal) in email; Excel cells use `#,##0.00` format
- **Error handling:**
  - `429 RESOURCE_EXHAUSTED` → stops Gemini calls, pipeline continues, remaining bids marked "(quota exceeded)"
  - `503 UNAVAILABLE` → retries up to 3× with 5/10/15s backoff
  - `401` on API → prints token expired message
  - `403/429` on API → backs off 30/60/90s and retries

### `send_catchup.py`

Used to send a one-time email to new recipients with all currently active bids (not just today's new ones). Run manually:

```bash
python send_catchup.py
```

> **Note:** Current version only sends records from today's crawl. For a full active-bids catchup, run the inline script described in the troubleshooting section.

---

## Scheduled Tasks

| Task | Schedule | Host | Command |
| ---- | -------- | ---- | ------- |
| Crawl + token refresh | 20:00 UTC (03:00 VN) | GCP VM `apple-monitor` | `python3 run_crawl.py` |
| Email send | 23:00 UTC (06:00 VN) | GCP VM `apple-monitor` | `python3 run_monitor.py` |
| Backup crawl | 20:30 UTC (03:30 VN) | GitHub Actions | `python apple_monitor.py crawl` |

**VM details:** `apple-monitor`, us-central1-a, e2-micro (Always Free tier)

**Log file:** `/home/<user>/apple_monitor/monitor.log`

**Useful commands on VM:**

```bash
# View recent log
tail -100 ~/apple_monitor/monitor.log

# Run crawl manually
cd ~/apple_monitor && python3 run_crawl.py

# Send email manually (uses today's Sheet data, no crawl needed)
cd ~/apple_monitor && python3 run_monitor.py

# Edit cron
crontab -e
```

**Deploy updated code from Windows:**

```powershell
gcloud compute scp "apple_monitor.py" <user>@apple-monitor:~/apple_monitor/ --zone=us-central1-a --quiet
```

---

## Known Limitations

| Limitation | Impact | Suggested Fix |
| ---------- | ------ | ------------- |
| ~~Runs on local Windows laptop~~ ✅ | ~~Missed runs if machine is off~~ | Migrated to GCP VM |
| ~~Token refresh blocked by VM IP~~ ✅ | ~~VM IP was blocked by portal~~ | VM IP changed (2026-06-03); token auto-syncs to GitHub Secret daily |
| Keyword-based search only | May miss bids with non-standard terminology | Add fuzzy/semantic matching layer |
| ~~No bid status tracking~~ ✅ | ~~Won't alert when a bid is cancelled/awarded~~ | `refresh_recently_closed()` implemented (14-day lookback) |
| Email-only delivery | No CRM or workflow integration | Integrate with Salesforce or Teams/Slack |
| AI analysis not always accurate | Gemini may misclassify or miss context | Use better prompts or a stronger model |
| `send_catchup.py` only covers today | New recipients miss historical active bids | Run inline catch-up script (see Troubleshooting) |

---

## Troubleshooting

### Token expired (`401` on all crawls)

```bash
python token_refresh.py
```
If Playwright fails, copy cURL manually (see Configuration → API Token above).

### Send catch-up email with ALL active bids

Run this from the `apple_monitor/` folder:

```python
python -c "
import sys; sys.path.insert(0, '.')
from apple_monitor import connect_sheet, send_email, SHEET_COLS, _days_left

ws = connect_sheet()
rows = ws.get_all_records(head=1)
all_sheet = [{col: str(row.get(col, '')) for col in SHEET_COLS} for row in rows]
active = [r for r in all_sheet if (_days_left(r.get('bidCloseDate')) or -1) >= 0]
print(f'{len(active)} active bids')
send_email(active, all_sheet)
"
```

### Re-analyze bids with quota/error in analysis

```python
python -c "
import sys, time; sys.path.insert(0, '.')
from google import genai
from apple_monitor import connect_sheet, send_email, analyze_bid, SHEET_COLS, _days_left, _QUOTA_EXCEEDED
from apple_monitor_config import GEMINI_API_KEY

ws = connect_sheet()
rows = ws.get_all_values()
headers = rows[0]; data_rows = rows[1:]
analysis_idx = headers.index('analysis'); close_idx = headers.index('bidCloseDate')
client = genai.Client(api_key=GEMINI_API_KEY)

ERROR_MARKERS = ['quota exceeded', '429', 'RESOURCE_EXHAUSTED', 'analysis error']
to_fix = [(i+2, {h: row[j] if j < len(row) else '' for j, h in enumerate(headers)})
          for i, row in enumerate(data_rows)
          if any(m in (row[analysis_idx] if len(row) > analysis_idx else '') for m in ERROR_MARKERS)
          and (_days_left(row[close_idx] if len(row) > close_idx else '') or -1) >= 0]

print(f'Found {len(to_fix)} bids to fix')
updates = []
for idx, (sheet_row, record) in enumerate(to_fix):
    result = analyze_bid(client, record)
    if result == _QUOTA_EXCEEDED: break
    updates.append({'range': f'N{sheet_row}', 'values': [[result]]})
    time.sleep(1.0)

if updates:
    ws.spreadsheet.values_batch_update({'valueInputOption': 'RAW', 'data': updates})
    print('Sheet updated')
"
```

### Test email delivery

```bash
python apple_monitor.py test
```

### Check Task Scheduler status

```powershell
Get-ScheduledTaskInfo -TaskName "AppleProcurementMonitor"
```

---

## Development Roadmap

### Short-term

- ~~**Cloud hosting**~~ ✅ — running on GCP VM `apple-monitor` (us-central1-a)
- ~~**Bid status tracking**~~ ✅ — `refresh_recently_closed()` refreshes status/winner/price for last 14 days
- ~~**Run failure alerting**~~ ✅ — `run_crawl.py` sends alert email + GitHub Actions backup if VM IP is blocked
- **Semantic keyword matching** — catch bids with non-standard Apple product descriptions

### Medium-term

- **Deadline change alerts** — notify when a bid's closing date is extended
- **Bid scoring** — structured priority score from Gemini (value tier, agency, Apple-specificity, urgency)
- **Slack / Teams integration** — real-time channel notifications alongside email

### Long-term

- **CRM integration** — auto-create Salesforce opportunities from qualifying bids
- **PDF spec analysis** — parse bid documents to check if Apple products are specified or competitor-specified
- **Multi-portal coverage** — extend to provincial portals and international databases (ADB, World Bank)

---

## External Services & Credentials

| Service | Purpose | Where to find credentials |
|---|---|---|
| muasamcong.mpi.gov.vn | Data source | Browser session (auto-refreshed daily by `token_refresh.py`) |
| Google Sheets | Database | `GOOGLE_SHEET_ID` in config; service account JSON in Secret Manager |
| Google AI Studio | Gemini API | Secret Manager → `GEMINI_API_KEY` |
| Gmail | Email delivery | Secret Manager → `GMAIL_APP_PASSWORD` |
| GCP Secret Manager | Credential store | `console.cloud.google.com` → project `apple-monitor` → Secret Manager |

**Secrets in GCP Secret Manager (project `apple-monitor`):**

| Secret name | Nội dung |
|---|---|
| `GMAIL_APP_PASSWORD` | Gmail App Password dùng để gửi email |
| `GEMINI_API_KEY` | Google AI Studio API key |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service account JSON (Sheets + Drive access) |

> **Security note:** Không còn credential nào lưu plaintext trên VM. `apple_monitor_config.py` đọc credentials từ Secret Manager lúc runtime. `google_service_account.json` trên VM là bản backup — có thể xóa đi sau khi xác nhận ổn định.
