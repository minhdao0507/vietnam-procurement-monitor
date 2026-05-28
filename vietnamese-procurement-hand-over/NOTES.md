# Vietnam Procurement Monitor — Project Notes

**Author:** dphm57  
**Built:** May 2026  
**Status:** Live — chạy tự động trên Windows Task Scheduler + GCP VM song song

---

## 1. Mục đích

Monitor tự động toàn bộ gói thầu trên cổng [muasamcong.mpi.gov.vn](https://muasamcong.mpi.gov.vn) (cổng đấu thầu quốc gia Việt Nam) theo 21 từ khóa liên quan đến Apple và thiết bị CNTT.

Mỗi ngày (06:00 GMT+7), hệ thống:
1. Làm mới token phiên đăng nhập (tự động qua Chrome headless)
2. Crawl tất cả gói thầu mới theo từng từ khóa
3. Loại bỏ trùng lặp qua Google Sheet
4. Phân tích từng gói thầu active bằng Gemini AI
5. Gửi email HTML + file Excel đính kèm đến 5 người nhận

---

## 2. Luồng hoạt động

```text
[Windows Task Scheduler] 06:00 hàng ngày
         │
         ▼
[run_monitor.bat]  → ghi log vào monitor.log
         │
         ▼
[run_monitor.py]
    │
    ├─ Step 1: token_refresh.py
    │      └─ Chrome headless → muasamcong.mpi.gov.vn
    │             → bắt request smart/search → lấy token mới
    │             → patch apple_monitor_config.py tại chỗ
    │
    ├─ Step 2: sync_to_vm.ps1
    │      └─ gcloud compute scp → đẩy config mới lên GCP VM
    │
    └─ Step 3: apple_monitor.py
           ├─ connect Google Sheet → lấy danh sách notifyId đã có
           ├─ Crawl 21 keywords (50 records/page, random delay 1.5–3s)
           ├─ Dedup: cross-run (Sheet IDs) + cross-keyword (seen_ids set)
           ├─ Gemini 2.5 Flash Lite → phân tích từng gói active
           ├─ Append vào Google Sheet
           └─ Gửi email HTML + Excel (.xlsx, 2 tab)

[GCP VM — us-central1-a — e2-micro]  (song song, độc lập)
    └─ cron: 0 1,3,5,7,9,11 * * *  (= 08:00–18:00 GMT+7)
           └─ run_monitor.py (tương tự, dùng config được sync từ Windows)
```

---

## 3. Cấu trúc file

```
apple_monitor/
├── apple_monitor.py              # Pipeline chính
├── apple_monitor_config.py       # Toàn bộ credentials + config (KHÔNG commit)
├── token_refresh.py              # Tự động refresh session token qua Chrome
├── run_monitor.py                # Entry point: refresh → sync → monitor
├── run_monitor.bat               # Wrapper cho Task Scheduler, ghi monitor.log
├── sync_to_vm.ps1                # Push config mới lên GCP VM sau refresh
├── send_catchup.py               # Gửi mail thủ công với tất cả bid đang active
├── backfill_urls.py              # Rebuild source_url cho records cũ từ notifyId
├── backfill_winner.py            # Backfill winner + winner_price cho records cũ
├── reanalyze.py                  # Re-analyze records bị lỗi quota trong Sheet
├── requirements.txt              # Python dependencies
├── google_service_account.json   # GCP service account key (KHÔNG commit)
└── monitor.log                   # Log tự động append mỗi lần chạy
```

---

## 4. Config — apple_monitor_config.py

```python
# 21 từ khóa tìm kiếm
KEYWORDS = [
    "macbook", "imac", "mac mini", "mac pro",
    "iphone", "ipad", "airpods", "apple watch",
    "máy tính", "máy tính xách tay", "máy tính bảng", "laptop",
    "di động", "điện tử", "thiết bị cntt",
    "điện thoại apple", "máy tính bảng apple",
    "tablet", "smartphone",
]

# Gmail gửi đi (2FA + App Password)
GMAIL_SENDER       = "dphm57@gmail.com"
GMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"

# Người nhận
EMAIL_RECIPIENTS = [
    "minh_dao@apple.com",
    "cyan_lee@apple.com",
    "mnguyen34@apple.com",
    "camaret.c@apple.com",
    "jacknguyen@apple.com",
]

# Google Sheet (database)
GOOGLE_SHEET_ID          = "1gtsSjOkj0_tiP0g1Y4N_ruxd0iDnKkSiowcffVln3Fo"
GOOGLE_CREDENTIALS_FILE  = "google_service_account.json"

# Gemini AI (paid tier — aistudio.google.com/apikey)
GEMINI_API_KEY = "AIza..."

# muasamcong API — tự động refresh mỗi lần chạy
API_TOKEN = ("...")  # ~1200 chars, hết hạn sau vài giờ
API_COOKIES = {
    "JSESSIONID":                 "...",          # hết hạn theo session
    "NSC_WT_QSE_QPSUBM_NTD_NQJ": "ffffff...",    # ổn định, không đổi
    "LFR_SESSION_STATE_20103":    "...",           # hết hạn theo session
}
```

---

## 5. Chi tiết từng file

### apple_monitor.py

**API endpoint:**
```
POST https://muasamcong.mpi.gov.vn/o/egp-portal-contractor-selection-v2/services/smart/search?token=<TOKEN>
Content-Type: application/json
Cookie: JSESSIONID=...; NSC_...=...; LFR_SESSION_STATE_20103=...
```

**Payload:**
```json
[{
  "pageSize": 50,
  "pageNumber": 0,
  "query": [{
    "index": "es-contractor-selection",
    "keyWord": "macbook",
    "matchType": "all-1",
    "matchFields": ["notifyNo", "bidName"],
    "filters": [
      {"fieldName": "type",     "searchType": "in",     "fieldValues": ["es-notify-contractor"]},
      {"fieldName": "caseKHKQ", "searchType": "not_in", "fieldValues": ["1"]}
    ]
  }]
}]
```

**Response:** `[{"page": {"content": [...], "totalPages": N, "totalElements": M}}]`

**LegacySSLAdapter** — portal dùng DH key yếu, phải hạ cipher:
```python
ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
```

**Dedup 2 tầng:**
- Cross-run: load `notifyId` từ Google Sheet trước khi crawl
- Cross-keyword: set `seen_ids` trong RAM, tránh đếm trùng khi 1 gói thầu khớp nhiều keyword

**Gemini — analyze_bid():**
- Model: `gemini-2.5-flash-lite` (paid tier, không bị quota 429)
- Retry: 429/RESOURCE_EXHAUSTED → trả `_QUOTA_EXCEEDED` sentinel, dừng Gemini nhưng pipeline tiếp tục
- Retry: 503/500/UNAVAILABLE → thử lại 3 lần (5s, 10s, 15s backoff)
- Output 4 dòng cố định: `Product / Value / Deadline / Priority`

**Email HTML:**

- Bids active sort theo `priceInit` cao → thấp (hàm `_sort_by_value`)
- Hiển thị flat list (không groupby keyword), mỗi card có badge keyword góc phải
- Nút **"View on Portal →"** dẫn thẳng đến trang chi tiết gói thầu
- `send_email()` nhận param `recipients=None` — nếu truyền vào thì override `EMAIL_RECIPIENTS` (dùng cho test mode)
- `send_catchup.py` — gửi thủ công tất cả active tenders (không chỉ new), dùng khi cần blast toàn bộ danh sách

**URL gói thầu — `_build_source_url(item)`:**

```text
https://muasamcong.mpi.gov.vn/web/guest/contractor-selection
  ?p_p_id=egpportalcontractorselectionv2_WAR_egpportalcontractorselectionv2
  &p_p_lifecycle=0&p_p_state=normal&p_p_mode=view
  &_egpportalcontractorselectionv2_WAR_egpportalcontractorselectionv2_render=detail-v2
  &type=es-notify-contractor
  &stepCode={item.stepCode}
  &id={item.id}               ← UUID cho bid 2026+, string "IB25..." cho bid cũ
  &notifyId={item.id}
  &inputResultId={item.inputResultId or "undefined"}
  &processApply={item.processApply}
  &bidMode={item.bidMode}
  &notifyNo={item.notifyNo}
  &planNo={item.planNo}
  &step={last segment of stepCode}
  &isInternet={item.isInternet}
  &caseKHKQ={item.caseKHKQ or "undefined"}
  &bidForm={item.bidForm}
```

> **Lưu ý:** `id` field trong API response: bid 2026+ dùng UUID (`e79ee415-...`), bid 2025 cũ dùng string (`IB2500563445`). Dùng `item.get('id') or item.get('notifyId')` để lấy đúng.

**Excel đính kèm (2 tab):**

- Tab `Active (N)` — tất cả gói thầu chưa hết deadline từ toàn bộ Sheet, sort: Value cao → thấp, rồi Days Left thấp → cao
- Tab `All Records (N)` — toàn bộ Sheet export, sort: Value cao → thấp
- Cột: `No. | Ref No. | Bid Name | Buyer | Province | Value (B VND) | Created Date | Deadline | Days Left | Status | Winner | Award (B VND) | Bid Form | Keyword | AI Analysis | Link`
- Format: header xanh, hàng khẩn (≤5 ngày còn lại) nền đỏ nhạt, tổng giá trị ở cuối
- Cột Link: click "View on Portal" → mở thẳng trang chi tiết gói thầu (Liferay portlet URL)
- Cột Status: map từ API code → tiếng Anh. Các giá trị: `IS_PUBLISH`/`1` → Open, `3` → Closed, `OPEN_BID` → Bidding Open, `OPEN_DXTC` → Under Evaluation, `PUB_KQLCNT` → Result Published, `CANCEL_BID`/`CANCELED`/`IS_CANCEL` → Cancelled, `NEW` → New, `INIT_MT` → Initializing
- Cột Winner + Award: chỉ có giá trị với bid đã đóng thầu và có kết quả trúng thầu

**Error handling API:**
- 401 → in cảnh báo token hết hạn, skip keyword
- 403/429 → backoff 30/60/90s, thử lại 3 lần
- Connection error → thử lại 3 lần (5/10/15s)

---

### token_refresh.py

Thứ tự thử browser:
1. `channel="chrome"` — Chrome thật cài trên máy (ít bị detect nhất)
2. `channel="msedge"` — Edge (fallback)
3. Playwright Chromium thuần (fallback cuối)

Cách lấy token:
- Attach listener `page.on("request")` → bắt URL chứa `smart/search?token=`
- Chờ 10 giây để trang JS boot và tự fire request
- Nếu không thấy: thử click nút search → fill input + Enter → press Enter
- Nếu Playwright fail hoàn toàn: kiểm tra token cũ còn valid không (`_test_current_token()`)

Sau khi có token → `_patch_config()`:
- Regex replace `API_TOKEN = (...)` block
- Cập nhật `JSESSIONID` và `LFR_SESSION_STATE_20103`
- Giữ nguyên `NSC_WT_QSE_QPSUBM_NTD_NQJ` (ổn định, không refresh)

---

### sync_to_vm.ps1

Chạy sau `token_refresh.py`, đẩy config mới lên VM:
```powershell
gcloud compute scp "M:\...\apple_monitor_config.py" \
  "dphm57@apple-monitor:/home/dphm57/apple_monitor/apple_monitor_config.py" \
  --zone=us-central1-a --quiet
```

Tại sao cần: VM chạy monitor độc lập nhưng không tự refresh token được nếu không có Chrome được cài đúng cách → Windows refresh token → sync lên VM → VM dùng token đó.

---

### run_monitor.bat

Entry point cho Task Scheduler:
```batch
cd /d "M:\Working\Apple\apple_monitor"
set PYTHON=C:\Users\Laptop\AppData\Local\Programs\Python\Python312\python.exe
set LOGFILE=M:\Working\Apple\apple_monitor\monitor.log
echo [%date% %time%] Starting monitor >> "%LOGFILE%"
"%PYTHON%" run_monitor.py >> "%LOGFILE%" 2>&1
echo [%date% %time%] Exit code: %ERRORLEVEL% >> "%LOGFILE%"
```

---

## 6. Infrastructure

### Windows Task Scheduler

| Field | Value |
|---|---|
| Task name | `AppleProcurementMonitor` |
| Action | `run_monitor.bat` |
| Working dir | `M:\Working\Apple\apple_monitor` |
| Triggers | Daily: 06:00 |
| WakeToRun | Enabled |
| ExecutionTimeLimit | 2 giờ |
| MultipleInstances | IgnoreNew |
| StartWhenAvailable | True |

Kiểm tra:
```powershell
Get-ScheduledTaskInfo -TaskName "AppleProcurementMonitor"
```

### GCP VM (backup 24/7)

| Field | Value |
|---|---|
| Instance name | `apple-monitor` |
| Machine type | `e2-micro` (Always Free tier) |
| Region | `us-central1-a` |
| External IP | `136.113.45.54` (dynamic — check `gcloud compute instances list` nếu thay đổi) |
| OS | Debian 12 |
| Disk | 30 GB standard |
| Cost | ~$0/tháng (nằm trong free tier) |

**Crontab trên VM** (`crontab -e` của user `dphm57`):

```bash
0 23 * * * cd /home/dphm57/apple_monitor && python3 run_monitor.py >> /home/dphm57/apple_monitor/monitor.log 2>&1
```

23:00 UTC = 06:00 GMT+7.

**SSH vào VM:**
```bash
gcloud compute ssh apple-monitor --zone=us-central1-a
```

**Xem log VM:**
```bash
tail -50 /home/dphm57/apple_monitor/monitor.log
```

---

## 7. Google Sheet

- URL: `https://docs.google.com/spreadsheets/d/1gtsSjOkj0_tiP0g1Y4N_ruxd0iDnKkSiowcffVln3Fo`
- Sheet name: `Bids`
- 18 cột: `notifyId | keyword | notifyNo | bid_name | investorName | investorCode | prov_name | publicDate | bidCloseDate | priceInit | bidForm | bidMode | status | analysis | crawled_at | source_url | winner | winner_price`
- Cột `source_url`: Liferay portlet URL dẫn thẳng đến trang chi tiết gói thầu
- Cột `winner` + `winner_price`: tên bên trúng thầu và giá trúng (đơn vị đồng), chỉ có với bid đã có kết quả
- Dedup key: `notifyId`
- Đang có: 11,455+ records (tính đến tháng 5/2026)

---

## 8. External services & credentials

| Service | Dùng để làm gì | Lấy credentials ở đâu |
|---|---|---|
| muasamcong.mpi.gov.vn | Nguồn dữ liệu gói thầu | Browser session (tự refresh) |
| Google Sheets | Database dedup + lưu trữ | `GOOGLE_SHEET_ID` + `google_service_account.json` |
| Google Gemini AI | Phân tích từng gói thầu | `GEMINI_API_KEY` — aistudio.google.com/apikey |
| Gmail SMTP | Gửi email alert | `GMAIL_APP_PASSWORD` — myaccount.google.com → Security → App Passwords |
| GCP Compute Engine | VM chạy 24/7 | console.cloud.google.com |

> **Bảo mật:** `apple_monitor_config.py` và `google_service_account.json` chứa credentials thật.  
> Không commit lên Git public. Không chia sẻ qua email/chat.

---

## 9. Các lỗi đã gặp và cách fix

### 9.1 Gemini 429 RESOURCE_EXHAUSTED

**Nguyên nhân:** Model `gemini-2.0-flash-lite` bị deprecated trên paid account, trả 404. Free tier bị quota rất thấp.

**Fix:**
- Nâng lên paid tier: aistudio.google.com → Billing → "My Billing Account - Tier 1 · Prepay"
- Đổi model sang `gemini-2.5-flash-lite`
- Thêm logic: nếu quota hết → đánh dấu "(quota exceeded — re-run tomorrow)", pipeline tiếp tục gửi mail

### 9.2 Gemini 503 UNAVAILABLE

**Nguyên nhân:** Gemini server quá tải tạm thời.

**Fix:** Retry 3 lần với backoff 5/10/15 giây:
```python
if ("503" in err or "500" in err or "UNAVAILABLE" in err) and attempt < 3:
    time.sleep(5 * (attempt + 1))
    continue
```

### 9.3 Playwright ERR_CONNECTION_RESET khi refresh token

**Nguyên nhân:** muasamcong.mpi.gov.vn detect Playwright Chromium (fingerprint) và block.

**Fix:** Dùng `channel="chrome"` để launch Chrome thật thay vì Playwright Chromium:
```python
browser = await pw.chromium.launch(channel="chrome", headless=True, ...)
```

### 9.4 Tab Active trong Excel chỉ hiện bid mới

**Nguyên nhân:** Code dùng `new_records` thay vì `all_records` để lọc active.

**Fix:**
```python
# Active tab = ALL active bids from full sheet (not just new ones)
active = [r for r in all_records if (_days_left(r.get("bidCloseDate")) or -1) >= 0]
```

### 9.5 Task Scheduler không tìm thấy Python

**Fix:** Hard-code full path Python trong `run_monitor.bat`:
```batch
set PYTHON=C:\Users\Laptop\AppData\Local\Programs\Python\Python312\python.exe
```

### 9.6 gcloud scp lỗi "no such file" với đường dẫn `~/`

**Nguyên nhân:** PuTTY's pscp không expand `~/` trên Linux.

**Fix:** Dùng absolute path:
```
dphm57@apple-monitor:/home/dphm57/apple_monitor/apple_monitor_config.py
```

### 9.7 SSL error khi crawl muasamcong

**Nguyên nhân:** Server dùng DH key yếu, requests từ chối kết nối.

**Fix:** `LegacySSLAdapter` với `DEFAULT:@SECLEVEL=0`, tắt verify cert:
```python
ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
```

---

## 10. Refresh token thủ công (khi auto-refresh fail)

```
1. Mở Chrome → vào muasamcong.mpi.gov.vn
2. F12 → tab Network
3. Tìm bất kỳ request POST nào đến /services/smart/search
4. Chuột phải → Copy as cURL
5. Từ URL, copy phần sau token=  (chuỗi dài ~1200 ký tự, đến hết URL)
6. Từ headers Cookie:, copy JSESSIONID và LFR_SESSION_STATE_20103
7. Update apple_monitor_config.py:
   - API_TOKEN = ("...")
   - "JSESSIONID": "..."
   - "LFR_SESSION_STATE_20103": "..."
8. Chạy: python run_monitor.py
```

---

## 11. Lệnh vận hành thường dùng

### Chạy thủ công

```bash
# Chạy full pipeline (token refresh + monitor)
python run_monitor.py

# Chỉ refresh token
python token_refresh.py

# Chỉ chạy monitor (bỏ qua refresh)
python apple_monitor.py

# Test gửi email (1 record giả)
python apple_monitor.py test

# Gửi catch-up email tất cả bid đang active
python send_catchup.py
```

### Xem log Windows

```powershell
Get-Content "M:\Working\Apple\apple_monitor\monitor.log" -Tail 50
```

### Xem log VM

```bash
gcloud compute ssh apple-monitor --zone=us-central1-a
tail -50 /home/dphm57/apple_monitor/monitor.log
```

### Sync config lên VM thủ công

```powershell
powershell -ExecutionPolicy Bypass -File "M:\Working\Apple\apple_monitor\sync_to_vm.ps1"
```

### Re-phân tích bid bị lỗi quota trong Sheet

```bash
# Chạy script chuyên dụng (commit mỗi 50 records, safe to re-run)
python reanalyze.py

# Hoặc chạy ngầm trên VM
nohup python3 reanalyze.py > /tmp/reanalyze.log 2>&1 &
```

### Backfill source_url cho records cũ

```bash
# Rebuild Liferay URL từ notifyId + bidMode + notifyNo + bidForm đang có trong Sheet
python backfill_urls.py
```

### Gửi catch-up email tất cả active bids từ Sheet

```bash
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

---

## 12. Giới hạn hiện tại và hướng cải thiện

| Vấn đề | Mức độ ảnh hưởng | Cách fix gợi ý |
|---|---|---|
| Token refresh dùng Chrome headless | Có thể fail nếu portal update frontend | Monitor log, fallback cURL thủ công |
| Tìm kiếm theo keyword đơn thuần | Bỏ lọt bid dùng thuật ngữ không chuẩn | Thêm fuzzy/semantic matching |
| Không theo dõi thay đổi bid | Không biết bid bị hủy/gia hạn | Build re-crawl + change detection |
| Chỉ gửi qua email | Không tích hợp workflow | Thêm Slack/Teams webhook |
| VM cần config được sync từ Windows | VM không tự refresh token | Cài Chrome trên VM đúng cách |
| Không có alert khi pipeline fail | Có thể miss nhiều giờ mà không biết | Gửi email khi log không có output |

---

## 13. Dependencies (requirements.txt)

```
requests      # HTTP crawling
gspread       # Google Sheets API
google-auth   # Service account credentials
google-genai  # Gemini AI SDK
openpyxl      # Tạo file Excel
urllib3       # SSL warning suppression
playwright    # Headless Chrome cho token refresh
```

**Cài đặt:**
```bash
pip install -r requirements.txt
playwright install chrome
```

---

## 14. Lịch sử phát triển (tóm tắt)

| Giai đoạn | Thay đổi |
|---|---|
| V1 | Crawl + Google Sheet dedup + Gmail gửi đến minh_dao |
| V2 | Thêm Gemini AI analysis |
| V3 | Fix SSL (LegacySSLAdapter), fix pagination |
| V4 | Token auto-refresh qua Playwright; fix Chrome headless (channel="chrome") |
| V5 | Thêm 4 người nhận; Tab Active Excel hiện all bids thay vì chỉ new |
| V6 | Nâng Gemini lên paid tier; đổi model sang gemini-2.5-flash-lite |
| V7 | Thêm retry 503; fix xử lý quota exceeded |
| V8 | Set up GCP e2-micro VM; cron 6x/ngày; sync config qua gcloud scp |
| V9 | Cột `source_url` trong Sheet hiển thị URL đầy đủ; Excel thêm cột Link clickable |
| V10 | Đổi lịch chạy từ 6x/ngày xuống 1x/ngày lúc 06:00 GMT+7; `send_catchup.py` gửi all active thay vì chỉ new |
| V11 | Fix hyperlink bug (openpyxl Hyperlink object per cell); fix sort Active (Value desc + Days Left asc) và All Records (Value desc); thêm cột Created Date; backfill 11,420 source_url cũ bị sai; thêm `reanalyze.py` và `backfill_urls.py` |
| V12 | Thêm 3 cột Excel: Status (mapped từ API code), Winner (bên trúng thầu), Award B VND (giá trúng); SHEET_COLS mở rộng lên 18 cột; `backfill_winner.py` để backfill dữ liệu cũ; `apple_monitor.py crawl` để crawl không gửi mail |
