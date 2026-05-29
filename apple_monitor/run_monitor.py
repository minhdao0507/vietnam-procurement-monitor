"""
run_monitor.py — Chạy lúc 6 AM VN (23:00 UTC).

Flow:
  1. Đọc records đã crawl hôm nay từ Google Sheet
  2. Gửi email với Excel attachment

Crawl chạy riêng lúc 3 AM VN bằng run_crawl.py.
Token không cần refresh ở đây (chỉ đọc Sheet + gửi mail).

Usage:
  python run_monitor.py
"""

import sys
import subprocess
from pathlib import Path

HERE = Path(__file__).parent


def _sync_to_vm():
    """Push updated config to GCP VM after token refresh."""
    sync_script = HERE / "sync_to_vm.ps1"
    if not sync_script.exists():
        return
    try:
        subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File", str(sync_script)],
                       timeout=30)
    except Exception as e:
        print(f"  [WARN] VM sync skipped: {e}")


def main():
    print("Sending today's email...")
    subprocess.run([sys.executable, str(HERE / "apple_monitor.py"), "send-only"], check=True)


if __name__ == "__main__":
    main()
