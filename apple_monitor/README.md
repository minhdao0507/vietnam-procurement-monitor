# Apple Procurement Monitor

Tự động crawl gói thầu Apple/IT từ muasamcong.mpi.gov.vn, phân tích bằng Claude, gửi email khi có thầu mới.

## Setup (1 lần duy nhất)

### 1. Cài thư viện

```bash
pip install -r requirements.txt
```

### 2. Gmail App Password

1. Vào [myaccount.google.com](https://myaccount.google.com) → **Security** → bật **2-Step Verification**
2. Tìm **App Passwords** → chọn "Other" → đặt tên "Apple Monitor" → Copy password (dạng `xxxx xxxx xxxx xxxx`)
3. Điền vào `apple_monitor_config.py`:
   ```python
   GMAIL_SENDER       = "your@gmail.com"
   GMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"
   ```

### 3. Google Sheet + Service Account

1. Tạo một Google Sheet mới, copy **Sheet ID** từ URL:
   `docs.google.com/spreadsheets/d/**<SHEET_ID>**/edit`

2. Vào [console.cloud.google.com](https://console.cloud.google.com):
   - Tạo project mới (hoặc dùng project có sẵn)
   - **APIs & Services** → Enable **Google Sheets API** và **Google Drive API**
   - **IAM & Admin** → **Service Accounts** → Create Service Account
   - Tạo xong → vào service account → **Keys** → **Add Key** → JSON → Download

3. Đặt file JSON vào folder `apple_monitor/`, đặt tên `google_service_account.json`

4. **Share Google Sheet** với email của service account (dạng `xxx@xxx.iam.gserviceaccount.com`) → quyền **Editor**

5. Điền vào config:
   ```python
   GOOGLE_SHEET_ID         = "1BxiMVs0XRA..."
   GOOGLE_CREDENTIALS_FILE = "google_service_account.json"
   ```

### 4. Claude API Key

Vào [console.anthropic.com](https://console.anthropic.com) → API Keys → tạo key mới:
```python
ANTHROPIC_API_KEY = "sk-ant-..."
```

### 5. muasamcong Token (cần refresh định kỳ)

1. Mở Chrome → vào `muasamcong.mpi.gov.vn/web/guest/contractor-selection`
2. **F12** → **Network** → filter **Fetch/XHR**
3. Thực hiện 1 lần search bất kỳ
4. Tìm request POST đến `/services/smart/search?token=...`
5. Right-click → **Copy as cURL**
6. Copy token (sau `?token=`) và các cookies (`JSESSIONID`, `NSC_WT_...`, `LFR_SESSION_...`) vào config

> Token hết hạn sau ~1-2h session không hoạt động. Script sẽ in `[401] Token expired` nếu cần refresh.

---

## Chạy thử

```bash
cd apple_monitor

# Test email (không cần token/sheet — chỉ cần Gmail config)
python apple_monitor.py test

# Chạy thật
python apple_monitor.py
```

---

## Tự động hóa (Windows Task Scheduler)

Chạy mỗi sáng 8:00 để check thầu mới trong ngày:

1. Mở **Task Scheduler** → Create Basic Task
2. Trigger: **Daily**, 8:00 AM
3. Action: **Start a program**
   - Program: `C:\Users\...\AppData\Local\Programs\Python\Python3x\python.exe`
   - Arguments: `M:\Working\Apple\apple_monitor\apple_monitor.py`
   - Start in: `M:\Working\Apple\apple_monitor`

> Lưu ý: Token muasamcong hết hạn theo session — cần refresh thủ công khi bị 401.
> Giải pháp lâu dài: dùng Playwright để tự login lấy token mới (có thể thêm sau).

---

## Email mẫu

- Subject: `[Apple Thầu] 12 gói mới — 21/05/2026`
- Body: grouped theo keyword, mỗi gói có tên, đơn vị, giá trị, deadline, phân tích Claude

---

## Chi phí ước tính

| Item | Cost |
|---|---|
| Claude Haiku (100 bids/ngày) | ~$0.002/ngày |
| Google Sheets API | Miễn phí |
| Gmail SMTP | Miễn phí |
