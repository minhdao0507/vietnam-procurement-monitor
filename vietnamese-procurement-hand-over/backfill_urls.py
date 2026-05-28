"""
backfill_urls.py
Rebuilds source_url for all rows that have the placeholder render=index URL.
Uses notifyId already stored in the sheet to build correct detail links.
Commits in batches of 200.
"""
import time
from apple_monitor_config import GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS_FILE
from apple_monitor import connect_sheet, SHEET_COLS

BASE = "https://muasamcong.mpi.gov.vn/web/guest/contractor-selection"
BATCH = 200


def build_url(row):
    uid       = row.get("notifyId", "")
    notify_no = row.get("notifyNo", "") or "undefined"
    bid_form  = row.get("bidForm", "")  or "undefined"
    bid_mode  = row.get("bidMode", "")  or "undefined"

    def _p(v):
        return v if v not in (None, "", []) else "undefined"

    params = (
        "p_p_id=egpportalcontractorselectionv2_WAR_egpportalcontractorselectionv2"
        "&p_p_lifecycle=0&p_p_state=normal&p_p_mode=view"
        "&_egpportalcontractorselectionv2_WAR_egpportalcontractorselectionv2_render=detail-v2"
        "&type=es-notify-contractor"
        "&stepCode=undefined"
        f"&id={_p(uid)}"
        f"&notifyId={_p(uid)}"
        "&inputResultId=undefined"
        "&bidOpenId=undefined&techReqId=undefined"
        "&bidPreNotifyResultId=undefined&bidPreOpenId=undefined"
        "&processApply=undefined"
        f"&bidMode={bid_mode}"
        f"&notifyNo={notify_no}"
        "&planNo=undefined"
        "&pno=undefined"
        "&step=tbmt"
        "&isInternet=undefined"
        "&caseKHKQ=undefined"
        f"&bidForm={bid_form}"
    )
    return f"{BASE}?{params}"


def run():
    print("Connecting to sheet...")
    ws = connect_sheet()
    rows = ws.get_all_records(head=1)
    print(f"  {len(rows):,} total records")

    url_col = SHEET_COLS.index("source_url") + 1
    to_fix = [
        (i + 2, r)
        for i, r in enumerate(rows)
        if r.get("notifyId", "").strip()
    ]
    print(f"  {len(to_fix):,} rows need URL backfill")
    if not to_fix:
        print("  Nothing to fix.")
        return

    updates = []
    done = 0
    col_letter = chr(64 + url_col)

    for sheet_row, record in to_fix:
        new_url = build_url(record)
        updates.append({"range": f"{col_letter}{sheet_row}", "values": [[new_url]]})
        done += 1
        if len(updates) >= BATCH:
            ws.spreadsheet.values_batch_update({"valueInputOption": "RAW", "data": updates})
            print(f"  Progress: {done}/{len(to_fix)} updated")
            updates = []
            time.sleep(1)

    if updates:
        ws.spreadsheet.values_batch_update({"valueInputOption": "RAW", "data": updates})
        print(f"  Progress: {done}/{len(to_fix)} updated")

    print(f"Done. Backfilled {done} URLs.")


if __name__ == "__main__":
    run()
