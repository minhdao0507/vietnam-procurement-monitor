"""
backfill_urls.py
Re-fetches source_url for closed/awarded bids by calling the API to get
inputResultId and bidOpenId — fields not present at initial crawl time.

Only processes bids where:
  - source_url still has inputResultId=undefined (i.e. not yet fixed), AND
  - bid is confirmed closed/awarded (has winner, or status=PUB_KQLCNT)
"""
import random
import time
from apple_monitor_config import API_TOKEN
from apple_monitor import (
    connect_sheet, SHEET_COLS, _build_source_url,
    BASE_URL, API_PATH, HEADERS, make_session,
)
from openpyxl.utils import get_column_letter

BATCH = 100
CLOSED_STATUSES = {"PUB_KQLCNT", "CANCEL_BID", "CANCELED", "IS_CANCEL", "3"}


def _needs_backfill(record):
    url = record.get("source_url", "")
    if "inputResultId=undefined" not in url:
        return False
    if record.get("winner", "").strip():
        return True
    if record.get("status", "") in CLOSED_STATUSES:
        return True
    return False


def run():
    print("Connecting to sheet...")
    ws = connect_sheet()
    rows = ws.get_all_records(head=1)
    print(f"  {len(rows):,} total records")

    to_fix = [
        (i + 2, r)
        for i, r in enumerate(rows)
        if r.get("notifyNo", "").strip() and _needs_backfill(r)
    ]
    print(f"  {len(to_fix):,} closed/awarded bids with incomplete URLs\n")

    if not to_fix:
        print("Nothing to backfill.")
        return

    session = make_session()
    url_col = get_column_letter(SHEET_COLS.index("source_url") + 1)
    api_url = f"{BASE_URL}{API_PATH}?token={API_TOKEN}"
    updates = []
    improved = failed = unchanged = 0

    for idx, (sheet_row, record) in enumerate(to_fix):
        notify_no = record["notifyNo"]
        payload = [{
            "pageSize": 1, "pageNumber": 0,
            "query": [{
                "index": "es-contractor-selection",
                "keyWord": notify_no,
                "matchType": "all-1",
                "matchFields": ["notifyNo"],
                "filters": [{"fieldName": "type", "searchType": "in",
                             "fieldValues": ["es-notify-contractor"]}],
            }],
        }]
        try:
            resp  = session.post(api_url, headers=HEADERS, json=payload, verify=False, timeout=15)
            data  = resp.json()
            items = (data[0] if isinstance(data, list) else data).get("page", {}).get("content", [])
            if not items:
                failed += 1
            else:
                new_url = _build_source_url(items[0])
                old_url = record.get("source_url", "")
                if new_url != old_url and "inputResultId=undefined" not in new_url:
                    updates.append({"range": f"{url_col}{sheet_row}", "values": [[new_url]]})
                    improved += 1
                else:
                    unchanged += 1
        except Exception as e:
            print(f"  [err] row {sheet_row} ({notify_no}): {e}")
            failed += 1

        if len(updates) >= BATCH:
            ws.spreadsheet.values_batch_update({"valueInputOption": "RAW", "data": updates})
            print(f"  {idx+1}/{len(to_fix)} — improved: {improved}, unchanged: {unchanged}, failed: {failed}")
            updates = []
            time.sleep(1)

        time.sleep(random.uniform(0.8, 1.2))

    if updates:
        ws.spreadsheet.values_batch_update({"valueInputOption": "RAW", "data": updates})

    print(f"\nDone. Improved: {improved} | Unchanged (API has no result yet): {unchanged} | Not found/error: {failed}")


if __name__ == "__main__":
    run()
