"""
run_monitor.py — Entry point called by Task Scheduler (or run_monitor.bat).

Flow:
  1. Refresh muasamcong token via Playwright (headless)
  2. Run the full Apple Procurement Monitor pipeline

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
    # ── Step 1: Refresh token ──────────────────────────────────
    print("Step 1/2 — Refreshing token...")
    result = subprocess.run([sys.executable, str(HERE / "token_refresh.py")])
    if result.returncode != 0:
        print(
            "\n[!] Token refresh failed.\n"
            "    Fix: update API_TOKEN + JSESSIONID manually in apple_monitor_config.py\n"
            "    then re-run:  python run_monitor.py\n"
        )
        sys.exit(1)

    # Sync fresh token to VM
    _sync_to_vm()

    # ── Step 2: Run monitor ────────────────────────────────────
    print("Step 2/2 — Running monitor...")
    subprocess.run([sys.executable, str(HERE / "apple_monitor.py")], check=True)


if __name__ == "__main__":
    main()
