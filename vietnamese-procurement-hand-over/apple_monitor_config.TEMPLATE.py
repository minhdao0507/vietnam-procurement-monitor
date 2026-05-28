# ─────────────────────────────────────────────────────────────
# Apple Procurement Monitor — Config TEMPLATE
# Copy this to apple_monitor_config.py and fill in all values.
# ─────────────────────────────────────────────────────────────

# Keywords to monitor
KEYWORDS = [
    # Apple-specific
    "macbook",
    "imac",
    "mac mini",
    "mac pro",
    "iphone",
    "ipad",
    "airpods",
    "apple watch",
    # General IT / devices (Vietnamese)
    "máy tính",
    "máy tính xách tay",
    "máy tính bảng",
    "laptop",
    "di động",
    "điện tử",
    "thiết bị cntt",
    # Branded / specific
    "điện thoại apple",
    "máy tính bảng apple",
    # English terms sometimes used in bids
    "tablet",
    "smartphone",
]

# ── Gmail ──────────────────────────────────────────────────────
# Sender account (must have 2FA enabled + App Password created)
# Setup: myaccount.google.com → Security → App Passwords
GMAIL_SENDER       = "your_gmail@gmail.com"
GMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"

# Who receives the alert emails
EMAIL_RECIPIENTS = [
    "recipient@apple.com",
]

# ── Google Sheet ───────────────────────────────────────────────
# Sheet ID is the long string in the URL:
#   docs.google.com/spreadsheets/d/<SHEET_ID>/edit
GOOGLE_SHEET_ID = "your_sheet_id_here"

# Path to service account JSON downloaded from Google Cloud Console
# Setup: console.cloud.google.com → IAM → Service Accounts → Create Key
GOOGLE_CREDENTIALS_FILE = "google_service_account.json"

# ── Gemini API ─────────────────────────────────────────────────
# Get key at: aistudio.google.com/apikey
# Enable billing on the linked GCP project to remove free-tier limits
GEMINI_API_KEY = "AIza..."

# ── muasamcong API credentials ─────────────────────────────────
# These expire with the browser session (~hours).
# Automatic refresh: token_refresh.py uses headless Chrome to capture a fresh token.
# Manual refresh: Chrome → F12 → Network → any POST to /services/smart/search
#                 → Copy as cURL → extract token= from URL + cookies below.
API_TOKEN = (
    "paste_full_token_here"
)

API_COOKIES = {
    "JSESSIONID":                  "paste_jsessionid_here",
    "NSC_WT_QSE_QPSUBM_NTD_NQJ":  "paste_nsc_cookie_here",  # stable, rarely changes
    "LFR_SESSION_STATE_20103":     "paste_lfr_state_here",
}
