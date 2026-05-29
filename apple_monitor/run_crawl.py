"""
run_crawl.py — Chạy lúc 3 AM VN (20:00 UTC).

Flow:
  1. Refresh muasamcong token via Playwright
  2. Crawl keywords + refresh statuses + Gemini analyze → ghi vào Sheet
  3. Không gửi email (email do run_monitor.py lúc 6 AM)
"""

import sys
import subprocess
from pathlib import Path

HERE = Path(__file__).parent


def main():
    print("Step 1/2 — Refreshing token...")
    result = subprocess.run([sys.executable, str(HERE / "token_refresh.py")])
    if result.returncode != 0:
        print(
            "\n[!] Token refresh failed.\n"
            "    Fix: update API_TOKEN + JSESSIONID manually in apple_monitor_config.py\n"
        )
        sys.exit(1)

    print("Step 2/2 — Crawling (no email)...")
    subprocess.run([sys.executable, str(HERE / "apple_monitor.py"), "crawl"], check=True)


if __name__ == "__main__":
    main()
