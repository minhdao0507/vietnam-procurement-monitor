"""
token_refresh.py
Opens muasamcong.mpi.gov.vn in a headless Chromium browser, waits for the
JS app to fire its first smart/search API call, then extracts the fresh
token + session cookies and patches apple_monitor_config.py in-place.

Usage:
  python token_refresh.py
"""

import re
import asyncio
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    from playwright.async_api import async_playwright
except ImportError:
    print(
        "playwright not found.\n"
        "Install with:\n"
        "  pip install playwright\n"
        "  playwright install chromium\n"
    )
    raise SystemExit(1)

CONFIG_FILE = Path(__file__).parent / "apple_monitor_config.py"
PORTAL_URL  = "https://muasamcong.mpi.gov.vn/web/guest/contractor-selection?render=index"
# JSESSIONID is from an authenticated browser session — never overwrite automatically.
# Only refresh it manually when goods endpoint stops returning data.
STABLE_COOKIES = {"NSC_WT_QSE_QPSUBM_NTD_NQJ", "JSESSIONID", "LFR_SESSION_STATE_20103"}


async def _capture(timeout_ms: int = 40_000) -> dict:
    captured: dict = {}

    async with async_playwright() as pw:
        # Try real Chrome first (less detectable), fall back to Playwright Chromium
        launch_opts_list = [
            dict(channel="chrome", headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]),
            dict(channel="msedge", headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]),
            dict(headless=True, args=[
                "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--ignore-certificate-errors", "--disable-web-security",
            ]),
        ]

        browser = None
        for opts in launch_opts_list:
            try:
                browser = await pw.chromium.launch(**opts)
                print(f"  Browser: {opts.get('channel', 'chromium')} headless={opts.get('headless', True)}")
                break
            except Exception:
                continue
        if browser is None:
            raise RuntimeError("Could not launch any browser")

        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
        )
        page = await ctx.new_page()

        def _on_request(req):
            if "smart/search" in req.url and "token=" in req.url and not captured:
                params = parse_qs(urlparse(req.url).query)
                token = (params.get("token") or [""])[0]
                if token:
                    captured["token"] = token

        page.on("request", _on_request)

        print("  Opening muasamcong (headless)...")
        try:
            await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception:
            await page.goto(PORTAL_URL, wait_until="commit", timeout=timeout_ms)

        # Wait for JS app to fully boot and fire the initial search API call
        await page.wait_for_timeout(10000)

        # If the page didn't auto-load results, try multiple search triggers
        if not captured:
            print("  No auto-load detected — triggering search...")
            # Try 1: click search button
            try:
                btn = page.locator("button[type='submit'], button.search-btn, button:has-text('Tìm')").first
                await btn.click(timeout=3000)
                await page.wait_for_timeout(5000)
            except Exception:
                pass
            # Try 2: fill input + Enter
            if not captured:
                try:
                    box = page.locator("input[type='text'], input[type='search'], input[placeholder]").first
                    await box.fill("laptop", timeout=3000)
                    await box.press("Enter")
                    await page.wait_for_timeout(6000)
                except Exception:
                    pass
            # Try 3: press Enter on the page directly
            if not captured:
                try:
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(4000)
                except Exception:
                    pass

        # Collect all browser cookies at this point
        captured["cookies"] = {c["name"]: c["value"] for c in await ctx.cookies()}
        await browser.close()

    return captured


def _patch_config(token: str, cookies: dict) -> None:
    text = CONFIG_FILE.read_text(encoding="utf-8")

    # Rebuild the API_TOKEN block split into ~80-char quoted chunks
    chunk_size = 80
    chunks = [token[i : i + chunk_size] for i in range(0, len(token), chunk_size)]
    inner = "\n    ".join(f'"{c}"' for c in chunks)
    new_block = f"API_TOKEN = (\n    {inner}\n)"
    text = re.sub(r"API_TOKEN\s*=\s*\([^)]+\)", new_block, text, flags=re.DOTALL)

    # Update only analytics/non-auth cookies (JSESSIONID preserved — see STABLE_COOKIES)

    CONFIG_FILE.write_text(text, encoding="utf-8")
    print(f"  Config patched: {CONFIG_FILE.name}")


def _test_current_token() -> bool:
    """Return True if the token already in config can reach the API."""
    try:
        import ssl, requests, urllib3
        from apple_monitor_config import API_TOKEN, API_COOKIES

        urllib3.disable_warnings()

        class _SSL(requests.adapters.HTTPAdapter):
            def init_poolmanager(self, *a, **kw):
                ctx = ssl.create_default_context()
                ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                kw["ssl_context"] = ctx
                return super().init_poolmanager(*a, **kw)

        s = requests.Session()
        s.mount("https://", _SSL())
        s.cookies.update(API_COOKIES)
        url = (
            "https://muasamcong.mpi.gov.vn"
            "/o/egp-portal-contractor-selection-v2/services/smart/search"
            f"?token={API_TOKEN}"
        )
        payload = [{"pageSize": 1, "pageNumber": 0, "query": [{"index": "es-contractor-selection",
            "keyWord": "ipad", "matchType": "all-1",
            "matchFields": ["notifyNo", "bidName"],
            "filters": [{"fieldName": "type", "searchType": "in", "fieldValues": ["es-notify-contractor"]}]}]}]
        headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
        resp = s.post(url, headers=headers, json=payload, verify=False, timeout=15)
        return resp.status_code == 200
    except Exception:
        return False


def refresh() -> bool:
    print("=== Token Refresh ===")

    # Step 1: try Playwright
    try:
        captured = asyncio.run(_capture())
    except Exception as e:
        print(f"  [WARN] Playwright failed: {e}")
        captured = {}

    if captured.get("token"):
        print(f"  Token: {captured['token'][:32]}...")
        found_cookies = [k for k in captured.get("cookies", {}) if k not in STABLE_COOKIES]
        print(f"  Session cookies updated: {found_cookies}")
        _patch_config(captured["token"], captured.get("cookies", {}))
        print("  Done.\n")
        return True

    # Step 2: Playwright failed — check if existing token still works
    print("  Playwright blocked — checking existing token...")
    if _test_current_token():
        print("  Existing token still valid — proceeding without refresh.\n")
        return True

    # Step 3: both failed
    print("  [FAIL] Token expired. Refresh manually:")
    print("  Chrome -> F12 -> Network -> any POST to smart/search -> Copy as cURL")
    print("  Extract token + JSESSIONID + LFR_SESSION_STATE_20103 -> update apple_monitor_config.py")
    return False


if __name__ == "__main__":
    ok = refresh()
    raise SystemExit(0 if ok else 1)
