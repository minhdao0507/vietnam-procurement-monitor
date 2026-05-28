# ─────────────────────────────────────────────────────────────
# Apple Procurement Monitor — Config Template
# Copy this file to apple_monitor_config.py and fill in values.
# ─────────────────────────────────────────────────────────────

# Keywords to monitor
KEYWORDS = [
    "macbook", "imac", "mac mini", "mac pro",
    "iphone", "ipad", "airpods", "apple watch",
    "máy tính", "máy tính xách tay", "máy tính bảng", "laptop",
    "di động", "điện tử", "thiết bị cntt",
    "điện thoại apple", "máy tính bảng apple",
    "tablet", "smartphone",
]

# ── Gmail ──────────────────────────────────────────────────────
# Sender account (must have 2FA enabled + App Password created)
# Setup: myaccount.google.com → Security → App Passwords
GMAIL_SENDER       = "your_gmail@gmail.com"
GMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"

# Who receives the alert emails
EMAIL_RECIPIENTS = [
    "recipient1@example.com",
    "recipient2@example.com",
]

# ── Google Sheet ───────────────────────────────────────────────
GOOGLE_SHEET_ID         = "your_google_sheet_id"
GOOGLE_CREDENTIALS_FILE = "google_service_account.json"

# ── Gemini API (free tier — aistudio.google.com/apikey) ───────
GEMINI_API_KEY = "your_gemini_api_key"

# ── muasamcong API credentials ─────────────────────────────────
# These expire with the browser session (~1-2h).
# Refresh: Chrome → F12 → Network → any POST to /services/smart/search
#          → Copy as cURL → extract token + cookies below.
API_TOKEN = (
    "your_api_token_here"
)

API_COOKIES = {
    "JSESSIONID":                 "your_jsessionid",
    "NSC_WT_QSE_QPSUBM_NTD_NQJ": "your_nsc_value",
    "LFR_SESSION_STATE_20103":    "your_lfr_session_state",
}
