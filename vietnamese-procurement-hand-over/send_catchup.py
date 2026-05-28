"""
send_catchup.py
Manual catchup: reads ALL records from Google Sheet,
sends email with all currently active bids (deadline not passed).
"""

import sys

from apple_monitor import (
    connect_sheet, send_email,
    SHEET_COLS, _days_left,
)

NOTE_HTML = """
<div style="background:#e8f0fe;border:1px solid #c5cae9;border-radius:8px;
            padding:16px 20px;margin-bottom:20px;font-size:13px;color:#1a237e;line-height:1.7">
  <p style="margin:0 0 10px 0"><b>Dear team,</b></p>
  <p style="margin:0 0 10px 0">
    This is the <b>final email for today</b>. Earlier emails this morning had two issues
    that have since been resolved:
  </p>
  <ul style="margin:0 0 10px 0;padding-left:20px">
    <li><b>Incorrect links</b> — Excel links were pointing to the wrong bid.
        This has been fixed; all links now open the correct bid page on the muasamcong portal.</li>
    <li><b>Incomplete AI Insights</b> — Some records showed quota errors during analysis.
        Re-analysis is currently running and will complete in the background.</li>
  </ul>
  <p style="margin:0 0 10px 0">
    This email provides a <b>consolidated list of all currently active bids</b>, sorted by value
    (high &rarr; low), with a full Excel report attached (Active tab + All Records tab).
  </p>
  <p style="margin:0 0 10px 0">
    <b>Starting tomorrow</b>, you will receive one email per day at <b>6:00 AM GMT+7</b>
    containing only <b>new bids</b> discovered since the previous run, with the same Excel format.
  </p>
  <p style="margin:0">Thank you for your patience.</p>
</div>"""


def run():
    print("Reading from Google Sheet...")
    ws   = connect_sheet()
    rows = ws.get_all_records(head=1)
    print(f"  {len(rows):,} total records in sheet")

    all_sheet = [{col: str(row.get(col, "")) for col in SHEET_COLS} for row in rows]

    active = [r for r in all_sheet if (_days_left(r.get("bidCloseDate")) or -1) >= 0]
    print(f"  {len(active)} active bids → sending email to all recipients...")

    if not active:
        print("  No active bids found.")
        sys.exit(0)

    send_email(
        active,
        all_sheet,
        note_html=NOTE_HTML,
        subject=f"[Procurement] Catchup — {len(active)} active bid(s) — final email today",
    )
    print("Done.")


if __name__ == "__main__":
    run()
