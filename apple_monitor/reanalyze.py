"""
reanalyze.py
Re-analyze Sheet rows that have 429/quota/error in analysis column.
Commits every BATCH rows. Safe to re-run — skips rows that already have clean analysis.
"""
import time
import sys
from apple_monitor_config import GEMINI_API_KEY, GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS_FILE
from apple_monitor import connect_sheet, analyze_bid, _QUOTA_EXCEEDED, SHEET_COLS
from google import genai
from google.oauth2.service_account import Credentials
import gspread

BATCH = 50

def needs_reanalysis(analysis):
    a = str(analysis).lower()
    return ('429' in a or 'quota' in a or 'resource_exhausted' in a or 
            'analysis error' in a or 'quota exceeded' in a)

def run():
    print('Connecting to sheet...')
    ws = connect_sheet()
    rows = ws.get_all_records(head=1)
    print(f'  {len(rows):,} total records')

    to_fix = [(i+2, r) for i, r in enumerate(rows) if needs_reanalysis(r.get('analysis', ''))]
    print(f'  {len(to_fix):,} rows need re-analysis')
    if not to_fix:
        print('  Nothing to fix.')
        return

    analysis_col = SHEET_COLS.index('analysis') + 1
    client = genai.Client(api_key=GEMINI_API_KEY)
    updates = []
    done = 0

    for sheet_row, record in to_fix:
        result = analyze_bid(client, record)
        if result == _QUOTA_EXCEEDED:
            print(f'  [!] Quota hit at row {sheet_row} after {done} records — stopping')
            break
        updates.append({'range': f'{chr(64+analysis_col)}{sheet_row}', 'values': [[result]]})
        done += 1
        if len(updates) >= BATCH:
            ws.spreadsheet.values_batch_update({'valueInputOption': 'RAW', 'data': updates})
            print(f'  Progress: {done}/{len(to_fix)} committed')
            updates = []
        time.sleep(0.5)

    if updates:
        ws.spreadsheet.values_batch_update({'valueInputOption': 'RAW', 'data': updates})
        print(f'  Progress: {done}/{len(to_fix)} committed')

    print(f'Done. Fixed {done} records.')

if __name__ == '__main__':
    run()
