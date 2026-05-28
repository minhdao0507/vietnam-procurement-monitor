"""
backfill_goods.py — Upload goods Excel to Drive for all PUB_KQLCNT bids.

3 parallel workers, resumable: bids already having goods_url are skipped.
Run with: nohup python3 -u backfill_goods.py > backfill_goods.log 2>&1 &
"""
import sys, time, threading, re, queue
sys.path.insert(0, '/home/dphm57/apple_monitor')

from apple_monitor import (
    connect_sheet, make_session, SHEET_COLS,
    _url_param, _fetch_goods_items, _create_goods_excel, _upload_to_drive,
)
from openpyxl.utils import get_column_letter

WORKERS          = 3
DELAY_PER_WORKER = 0.5   # seconds between calls per worker
BATCH_SIZE       = 20    # sheet update batch

# ── Worker ────────────────────────────────────────────────────────────────────

def process_one(session, sheet_row, record):
    """Returns (sheet_row, drive_url, notify_no, n_items) or None if no data."""
    source_url      = record.get("source_url", "")
    input_result_id = _url_param(source_url, "inputResultId")
    process_apply   = _url_param(source_url, "processApply") or "LDT"
    notify_no       = record.get("notifyNo", "unknown")

    if not input_result_id:
        return None

    items = _fetch_goods_items(session, input_result_id, process_apply)
    if not items:
        return None

    safe_name  = re.sub(r"[^\w\-]", "_", notify_no)
    xlsx_bytes = _create_goods_excel(
        items,
        bid_name  = record.get("bid_name", ""),
        notify_no = notify_no,
        winner    = record.get("winner", ""),
    )
    drive_url = _upload_to_drive(xlsx_bytes, f"HangHoa_{safe_name}.xlsx")
    return sheet_row, drive_url, notify_no, len(items)


def worker_fn(worker_id, task_q, result_q):
    session = make_session()

    while True:
        try:
            sheet_row, record = task_q.get(timeout=10)
        except queue.Empty:
            break

        try:
            result = process_one(session, sheet_row, record)
            result_q.put(("ok", result))
        except Exception as e:
            result_q.put(("err", record.get("notifyNo", "?"), str(e)))

        task_q.task_done()
        time.sleep(DELAY_PER_WORKER)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Connecting to sheet...")
    ws   = connect_sheet()
    rows = ws.get_all_records(head=1)

    gc = get_column_letter(SHEET_COLS.index("goods_url") + 1)

    to_process = [
        (i + 2, r) for i, r in enumerate(rows)
        if r.get("status") == "PUB_KQLCNT"
        and not str(r.get("goods_url", "")).strip()
        and _url_param(r.get("source_url", ""), "inputResultId")
    ]

    total = len(to_process)
    skipped_no_id = sum(
        1 for r in rows
        if r.get("status") == "PUB_KQLCNT"
        and not str(r.get("goods_url", "")).strip()
        and not _url_param(r.get("source_url", ""), "inputResultId")
    )
    print(f"  {total} bids to process | {skipped_no_id} skipped (no inputResultId)")
    print(f"  Workers: {WORKERS} | Delay: {DELAY_PER_WORKER}s/worker\n")

    if total == 0:
        print("Nothing to do.")
        return

    task_q   = queue.Queue()
    result_q = queue.Queue()

    for item in to_process:
        task_q.put(item)

    threads = [
        threading.Thread(target=worker_fn, args=(i, task_q, result_q), daemon=True)
        for i in range(WORKERS)
    ]
    for t in threads:
        t.start()

    done            = 0
    no_data         = 0
    failed          = 0
    processed       = 0
    pending_updates = []
    start_time      = time.time()

    while processed < total:
        try:
            msg = result_q.get(timeout=5)
        except queue.Empty:
            if not any(t.is_alive() for t in threads):
                break
            continue

        processed += 1
        status = msg[0]

        if status == "ok":
            result = msg[1]
            if result:
                sheet_row, drive_url, notify_no, n_items = result
                pending_updates.append({"range": f"{gc}{sheet_row}", "values": [[drive_url]]})
                done += 1
            else:
                no_data += 1
        else:
            _, notify_no, err = msg
            failed += 1
            print(f"  [ERR] {notify_no}: {err}")

        if processed % 100 == 0 or processed == total:
            elapsed = time.time() - start_time
            rate    = processed / elapsed if elapsed > 0 else 0
            eta_s   = (total - processed) / rate if rate > 0 else 0
            eta_m   = int(eta_s / 60)
            print(f"  {processed}/{total} — uploaded:{done} no_data:{no_data} "
                  f"failed:{failed} | ETA ~{eta_m}m")

        if len(pending_updates) >= BATCH_SIZE:
            ws.spreadsheet.values_batch_update({"valueInputOption": "RAW", "data": pending_updates})
            pending_updates = []

    if pending_updates:
        ws.spreadsheet.values_batch_update({"valueInputOption": "RAW", "data": pending_updates})

    for t in threads:
        t.join()

    elapsed = int(time.time() - start_time)
    print(f"\nDone in {elapsed//60}m{elapsed%60}s")
    print(f"  Uploaded: {done} | No goods data: {no_data} | Failed: {failed}")


if __name__ == "__main__":
    main()
