"""
run_crawl.py — Chạy lúc 3 AM VN (20:00 UTC).

Flow:
  1. Refresh muasamcong token via Playwright
  2. Crawl keywords + refresh statuses + Gemini analyze → ghi vào Sheet
  3. Không gửi email (email do run_monitor.py lúc 6 AM)
  4. Nếu crawl fail → gửi alert email ngay để fix tay trước 6 AM
"""

import sys
import ssl
import smtplib
import subprocess
import traceback
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))


def _send_alert(reason: str, detail: str = ""):
    try:
        from apple_monitor_config import (
            GMAIL_SENDER, GMAIL_APP_PASSWORD, EMAIL_RECIPIENTS
        )
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        subject = f"[Apple Monitor] Crawl thất bại — {now}"
        body = (
            f"Crawl lúc {now} bị lỗi — email 6 AM sẽ không có dữ liệu mới.\n\n"
            f"Lý do: {reason}\n"
        )
        if detail:
            body += f"\nChi tiết:\n{detail}\n"
        body += (
            "\n— Để fix tay:\n"
            "  1. Mở Chrome → muasamcong.mpi.gov.vn → F12 → Network → copy token\n"
            "  2. Chạy trên máy local: python apple_monitor.py crawl\n"
        )

        msg = MIMEMultipart()
        msg["From"]    = formataddr(("Apple Monitor", GMAIL_SENDER))
        msg["To"]      = GMAIL_SENDER
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
            s.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_SENDER, [GMAIL_SENDER], msg.as_string())

        print(f"  Alert sent → {GMAIL_SENDER}")
    except Exception as e:
        print(f"  [WARN] Could not send alert email: {e}")


def main():
    # ── Step 1: Refresh token ──────────────────────────────────
    print("Step 1/2 — Refreshing token...")
    result = subprocess.run([sys.executable, str(HERE / "token_refresh.py")])
    if result.returncode != 0:
        msg = "Token refresh thất bại (Playwright timeout hoặc site block VM IP)"
        print(f"\n[!] {msg}")
        _send_alert(msg)
        sys.exit(1)

    # ── Step 2: Crawl ──────────────────────────────────────────
    print("Step 2/2 — Crawling (no email)...")
    result = subprocess.run([sys.executable, str(HERE / "apple_monitor.py"), "crawl"])
    if result.returncode != 0:
        msg = f"apple_monitor.py crawl thoát với code {result.returncode}"
        print(f"\n[!] {msg}")
        _send_alert(msg)
        sys.exit(1)

    # ── Step 3: Sync fresh token to GitHub Secret ──────────────
    print("Step 3/3 — Syncing token to GitHub Secret...")
    config_content = (HERE / "apple_monitor_config.py").read_text(encoding="utf-8")
    r = subprocess.run(
        ["gh", "secret", "set", "APPLE_MONITOR_CONFIG",
         "--repo", "minhdao0507/vietnam-procurement-monitor",
         "--body", config_content],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        print("  GitHub Secret updated.")
    else:
        print(f"  [WARN] Secret sync failed (non-blocking): {r.stderr.strip()}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _send_alert("Lỗi không xác định trong run_crawl.py", traceback.format_exc())
        raise
