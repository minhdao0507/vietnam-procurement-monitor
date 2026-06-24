"""
Apple Vietnam — Monthly Procurement Intelligence Report
Chạy vào mùng 1 hàng tháng:
  1. Pull data từ Google Sheet
  2. Chạy analysis
  3. Generate HTML report
  4. Upload lên GCS
  5. Gửi email với link
"""

import json
import os
import smtplib
import subprocess
import sys
import io
from collections import defaultdict
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from google.cloud import secretmanager
from google.oauth2.service_account import Credentials
import gspread

# ── Secrets ──────────────────────────────────────────────────────
def _secret(name):
    c = secretmanager.SecretManagerServiceClient()
    return c.access_secret_version(
        name=f"projects/apple-monitor/secrets/{name}/versions/latest"
    ).payload.data.decode("utf-8")

SA_JSON       = _secret("GOOGLE_SERVICE_ACCOUNT_JSON")
GMAIL_PASS    = _secret("GMAIL_APP_PASSWORD")
SHEET_ID      = "1gtsSjOkj0_tiP0g1Y4N_ruxd0iDnKkSiowcffVln3Fo"
GMAIL_USER    = "dphm57@gmail.com"
# Recipients come from env (REPORT_RECIPIENTS, comma-separated) and default to the
# owner's personal inbox only — so a stray run never emails the client. To send the
# real report, set REPORT_RECIPIENTS on the VM cron, e.g.
#   REPORT_RECIPIENTS="dphm57.1@gmail.com,minh_dao@apple.com"
RECIPIENTS    = [e.strip() for e in
                 os.environ.get("REPORT_RECIPIENTS", "dphm57.1@gmail.com").split(",")
                 if e.strip()]
DRAFT_RECIPIENTS = []  # disabled — send manually only
GCS_BUCKET    = "apple-procurement-reports"

# ── Bid form classification ────────────────────────────────────────
# muasamcong.mpi.gov.vn bidForm values
OPEN_FORMS = frozenset(("DTRR", "HCQT"))      # Đấu thầu rộng rãi / Hạn chế
CHCT_FORMS = frozenset(("CHCT", "CHCTRG"))     # Chào hàng cạnh tranh (competitive shopping)
CDNT_FORMS = frozenset(("CDNT", "CDNTRG", "MSDD"))  # Chỉ định thầu / Mua sắm trực tiếp


# ── Pull & analyse data ───────────────────────────────────────────
def analyse():
    info  = json.loads(SA_JSON)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://spreadsheets.google.com/feeds",
                      "https://www.googleapis.com/auth/drive"]
    )
    ws   = gspread.authorize(creds).open_by_key(SHEET_ID).worksheets()[0]
    rows = ws.get_all_values()
    HDR  = {h: i for i, h in enumerate(rows[0])}
    data = rows[1:]

    TECH_KW = [
        "máy tính", "laptop", "máy chủ", "server", "thiết bị công nghệ",
        "thiết bị it", "phần mềm", "máy in", "máy tính bảng", "ipad",
        "iphone", "macbook", "apple", "màn hình", "máy scan", "scanner",
        "switch", "router", "mạng", "thiết bị điện tử", "thiết bị văn phòng",
        "camera", "ups", "lưu trữ", "storage", "workstation", "tablet",
        "điện thoại", "thiết bị thông tin",
    ]
    APPLE_DEV_KW = [
        "máy tính xách tay", "laptop",
        "máy tính bảng", "tablet",
        "máy tính để bàn", "desktop",
        "máy vi tính để bàn",   # explicit desktop form
        "máy vi tính xách tay", # explicit laptop form
        "máy vi tính bảng",     # explicit tablet form
        "máy tính cá nhân", "all-in-one",
        "điện thoại thông minh", "smartphone", "điện thoại di động",
        "thiết bị di động",
    ]
    WIN_KW = [
        "windows", "microsoft office", "ms office", "office 365",
        "exchange server", "windows server", "active directory",
    ]
    INFRA_KW = [
        "máy chủ", "server rack", "san storage", "thiết bị lưu trữ san",
        "thiết bị chuyển mạch san", "công tơ điện", "thiết bị đo xa",
        "thiết bị y tế", "máy gia tốc", "máy xạ trị", "linh kiện tên lửa",
        "vật tư bán thành phẩm",
    ]
    SW_KW = [
        "bản quyền phần mềm", "license phần mềm", "hệ thống phần mềm",
        "triển khai phần mềm", "dịch vụ cloud", "thuê dịch vụ",
    ]
    APPLE_EXPLICIT = ["apple", "iphone", "ipad", "macbook", "imac",
                      "mac pro", "mac mini", "mac studio", "airpods"]

    def classify(bid_name, analysis):
        name = bid_name.lower()
        text = (bid_name + " " + analysis).lower()  # for device detection only

        # apple_explicit: bid_name only — analysis column is AI-generated, unreliable
        if any(k in name for k in APPLE_EXPLICIT):
            return "apple_explicit"

        # windows/infra/software: use full text (these are conservative exclusions)
        if any(k in text for k in WIN_KW):
            return "windows_locked"
        if any(k in text for k in INFRA_KW) and not any(k in name for k in APPLE_DEV_KW):
            return "infra_only"
        if any(k in text for k in SW_KW) and not any(k in name for k in APPLE_DEV_KW):
            return "software_only"

        # apple_possible: bid_name only — don't rely on analysis to classify devices
        if any(k in name for k in APPLE_DEV_KW):
            return "apple_possible"

        return "other_tech"

    # Dedup by notifyId — Sheet accumulates duplicate rows (legacy keyword crawl
    # wrote same bid under multiple keywords). Keep last occurrence (latest crawl,
    # winner/price most up to date). Rows without notifyId are kept as-is.
    nid_idx = HDR.get("notifyId", 0)
    deduped, seen = [], {}
    for r in data:
        nid = r[nid_idx].strip() if len(r) > nid_idx else ""
        if not nid:
            deduped.append(r)
            continue
        seen[nid] = r          # later row overwrites earlier → latest wins
    deduped.extend(seen.values())
    data = deduped

    all_tech, all_with_winner = [], []
    for r in data:
        if len(r) < 14:
            continue
        bid_name = r[HDR["bid_name"]].strip()
        keyword  = r[HDR["keyword"]].strip().lower()
        analysis = r[HDR["analysis"]].strip() if len(r) > HDR.get("analysis", 999) else ""
        winner   = r[HDR["winner"]].strip() if len(r) > HDR.get("winner", 16) else ""
        wp_str   = r[HDR["winner_price"]].strip() if len(r) > HDR.get("winner_price", 17) else ""
        pi_str   = r[HDR["priceInit"]].strip()
        bid_form   = r[HDR["bidForm"]].strip()
        pub_date   = r[HDR["publicDate"]].strip()
        year       = pub_date[:4] if pub_date else ""
        investor   = r[HDR["investorName"]].strip()
        source_url = r[HDR["source_url"]].strip() if len(r) > HDR.get("source_url", 999) else ""

        is_tech = any(k in bid_name.lower() for k in TECH_KW) or keyword in [
            "iphone", "ipad", "macbook", "apple", "máy tính", "laptop",
            "thiết bị cntt", "điện tử", "di động", "máy tính xách tay",
            "thiết bị it", "smartphone",
        ]
        if not is_tech:
            continue

        try:
            pi = float(pi_str) if pi_str else 0
        except:
            pi = 0
        try:
            wp = float(wp_str) if wp_str else 0
        except:
            wp = 0

        # winner_price > budget: two distinct cases (confirmed via validate_data dump).
        #  • ratio > 1.5x  → corrupt budget (e.g. priceInit=1 placeholder when dự toán
        #    wasn't published). Drop the bogus budget (pi=0) so it can't pollute
        #    discount or market sizing — but KEEP the real award value (wp) in vendor
        #    totals. Only 1 such row in current data.
        #  • 1.01–1.5x     → legitimate mild over-budget award (real procurement
        #    outcome, ~11 rows). Leave untouched; the small negative discount is true.
        if wp > 0 and pi > 0 and wp > pi * 1.5:
            pi = 0

        cat = classify(bid_name, analysis)
        row = dict(cat=cat, bid_name=bid_name, winner=winner,
                   wp=wp, pi=pi, bid_form=bid_form, year=year,
                   pub_date=pub_date, investor=investor, source_url=source_url)
        all_tech.append(row)
        if winner and wp > 0:
            all_with_winner.append(row)

    # Aggregations
    cats = defaultdict(list)
    for b in all_tech:
        cats[b["cat"]].append(b)

    possible = cats["apple_possible"]

    # Top investors in apple-possible + winner breakdown per investor
    inv         = defaultdict(lambda: {"count": 0, "value": 0.0})
    inv_winners = defaultdict(lambda: defaultdict(float))

    CHART_CATS = ["Desktop / iMac", "Laptop", "Tablet / iPad"]
    vendor_cat = defaultdict(lambda: defaultdict(float))

    def device_cat(bid_name):
        n = bid_name.lower()
        if any(k in n for k in ["laptop", "máy tính xách tay"]):
            return "Laptop"
        if any(k in n for k in ["máy tính bảng", "tablet"]):
            return "Tablet / iPad"
        if any(k in n for k in ["máy tính để bàn", "desktop", "all-in-one"]):
            return "Desktop / iMac"
        return None  # skip non-device bids

    def clean_winner(raw):
        name = raw.split("|")[0].strip()
        for prefix in ["CÔNG TY TNHH MỘT THÀNH VIÊN ", "CÔNG TY TNHH ",
                       "CÔNG TY CỔ PHẦN ", "TỔNG CÔNG TY CỔ PHẦN ",
                       "TỔNG CÔNG TY ", "TẬP ĐOÀN "]:
            if name.upper().startswith(prefix):
                name = name[len(prefix):]
                break
        return name.strip()

    for b in possible:
        inv[b["investor"]]["count"] += 1
        inv[b["investor"]]["value"] += b["pi"]
        if b["winner"] and b["wp"] > 0:
            w   = clean_winner(b["winner"])
            cat = device_cat(b["bid_name"])
            inv_winners[b["investor"]][w] += b["wp"]
            if cat:
                vendor_cat[w][cat] += b["wp"]

    top_investors_raw = sorted(inv.items(), key=lambda x: -x[1]["value"])[:10]

    def winner_breakdown(investor_name):
        winners = inv_winners[investor_name]
        total   = sum(winners.values())
        if not total:
            return []
        top3 = sorted(winners.items(), key=lambda x: -x[1])[:3]
        return [{"name": w, "pct": round(v / total * 100)} for w, v in top3]

    top_investors = [
        {"name": n[:55], "value_b": round(d["value"] / 1e9, 1),
         "count": d["count"], "winners": winner_breakdown(n)}
        for n, d in top_investors_raw
    ]

    # Top 20 vendors by device contract value only
    top_vendors = sorted(
        [(v, cats) for v, cats in vendor_cat.items() if sum(cats.values()) > 0],
        key=lambda x: -sum(x[1].values())
    )[:20]
    vendor_chart = {
        "vendors": [v for v, _ in top_vendors],
        "cats": {
            cat: [round(vendor_cat[v].get(cat, 0) / 1e9, 1) for v, _ in top_vendors]
            for cat in CHART_CATS
        }
    }

    # Half-year trend — dùng pi (ngân sách dự toán), không dùng wp
    # wp thấp ở kỳ hiện tại vì nhiều gói chưa có kết quả trúng thầu
    yr = defaultdict(lambda: {"count": 0, "value": 0.0})
    for b in all_tech:
        pd = b.get("pub_date", "")
        if len(pd) >= 7 and pd[:4] in ("2022","2023","2024","2025","2026") and b["pi"] > 0:
            half = "H1" if int(pd[5:7]) <= 6 else "H2"
            yr[f"{pd[:4]} {half}"]["count"] += 1
            yr[f"{pd[:4]} {half}"]["value"] += b["pi"]

    # Device sub-categories in apple_possible
    dev_cats = defaultdict(lambda: {"count": 0, "value": 0.0})
    for b in possible:
        n = b["bid_name"].lower()
        if any(k in n for k in ["laptop", "máy tính xách tay"]):
            k = "Laptop"
        elif any(k in n for k in ["máy tính bảng", "tablet"]):
            k = "Tablet / iPad"
        elif any(k in n for k in ["máy tính để bàn", "desktop", "all-in-one"]):
            k = "Desktop / iMac"
        else:
            k = "Other devices"
        dev_cats[k]["count"] += b["pi"] > 0 and 1 or 0
        dev_cats[k]["value"] += b["pi"]

    # ── Gap chart: FPT IS trust vs. device gap at key accounts ──────
    KEY_ACCOUNTS = {
        "BIDV":         "ĐẦU TƯ VÀ PHÁT TRIỂN",
        "Vietcombank":  "NGOẠI THƯƠNG",
        "Vietinbank":   "CÔNG THƯƠNG",
        "Tổng cục Thuế":"Tổng cục Thuế",
        "Kho bạc NN":   "Kho bạc Nhà nước",
    }
    FPTIS_KEYS   = ["HỆ THỐNG THÔNG TIN FPT", "FPT IS", "FPT INFORMATION"]
    SUNVIET_KEYS = ["SUN VIỆT", "SUN VIET", "VIỄN THÔNG TIN HỌC SUN", "SVTECH", "SV TECH", "SV-TECH"]

    def is_lead(winner, keys):
        # Only count as a win if the vendor is the FIRST entity in the winner field
        # (handles liên danh / consortium bids where vendor may be a minor partner)
        lead = winner.split("|")[0].upper().strip()
        return any(f in lead for f in keys)

    gap_chart = []
    for label, kw in KEY_ACCOUNTS.items():
        acct_bids = [b for b in all_with_winner if kw in b["investor"]]
        fptis_val = sum(b["wp"] for b in acct_bids
                        if is_lead(b["winner"], FPTIS_KEYS))
        device_bids = [b for b in acct_bids if device_cat(b["bid_name"])]
        device_val  = sum(b["wp"] for b in device_bids
                          if not is_lead(b["winner"], FPTIS_KEYS))
        gap_chart.append({
            "account":    label,
            "fptis_b":    round(fptis_val / 1e9, 1),
            "device_b":   round(device_val / 1e9, 1),
        })

    # Sun Viet footprint: their wins at each key account + Viettel
    SUNVIET_ACCOUNTS = {
        "BIDV":         "ĐẦU TƯ VÀ PHÁT TRIỂN",
        "Tổng cục Thuế":"Tổng cục Thuế",
        "Viettel":      "VIỄN THÔNG QUÂN ĐỘI",
        "EVN":          "ĐIỆN LỰC MIỀN NAM",
        "Kho bạc NN":   "Kho bạc Nhà nước",
    }
    sunviet_chart = []
    for label, kw in SUNVIET_ACCOUNTS.items():
        acct_bids = [b for b in all_with_winner if kw in b["investor"]]
        sv_bids   = [b for b in acct_bids if is_lead(b["winner"], SUNVIET_KEYS)]
        sv_val    = sum(b["wp"] for b in sv_bids)
        other_val = sum(b["wp"] for b in acct_bids if not is_lead(b["winner"], SUNVIET_KEYS))
        sunviet_chart.append({
            "account":  label,
            "sv_b":     round(sv_val / 1e9, 1),
            "sv_wins":  len(sv_bids),
            "other_b":  round(min(other_val / 1e9, 500), 1),
        })

    # ── Account capture: who wins device bids at key accounts ─────────
    # For each key account, top 4 vendors by device contract value
    acct_capture = {}
    for label, kw in KEY_ACCOUNTS.items():
        vendor_vals = defaultdict(float)
        for b in possible:
            if kw in b["investor"] and b["winner"] and b["wp"] > 0:
                vendor_vals[clean_winner(b["winner"])] += b["wp"]
        top4 = sorted(vendor_vals.items(), key=lambda x: -x[1])[:4]
        acct_capture[label] = [{"vendor": v[:25], "value_b": round(val/1e9, 1)}
                                for v, val in top4]

    # ── FPT IS top deals (all tech categories, not just devices) ────────
    fptis_all_bids = [b for b in all_with_winner if is_lead(b["winner"], FPTIS_KEYS)]
    fptis_deals = []
    for b in sorted(fptis_all_bids, key=lambda x: -x["wp"])[:20]:
        disc = round((1 - b["wp"]/b["pi"])*100, 1) if b["pi"] > 0 else None
        fptis_deals.append({
            "buyer":   b["investor"][:45],
            "bid":     b["bid_name"][:60],
            "award_b": round(b["wp"]/1e9, 2),
            "form":    b["bid_form"],
            "year":    b["year"],
            "url":     b.get("source_url", ""),
        })

    # Keep hmap data (used only if needed)
    hmap_vendors = []
    hmap_buyers  = []
    hmap_z       = []

    # ── Analysis 1: Who wins device bids at target accounts ──────────
    TARGET_ACCOUNTS = ["ĐẦU TƯ VÀ PHÁT TRIỂN", "NGOẠI THƯƠNG", "CÔNG THƯƠNG",
                       "Tổng cục Thuế", "NÔNG NGHIỆP", "Kho bạc Nhà nước"]

    target_bids = [b for b in possible if b["winner"] and b["wp"] > 0
                   and any(k in b["investor"] for k in TARGET_ACCOUNTS)]

    target_rows = []
    for b in sorted(target_bids, key=lambda x: -x["wp"])[:30]:
        disc = round((1 - b["wp"]/b["pi"])*100, 1) if b["pi"] > 0 else 0
        target_rows.append({
            "buyer":    b["investor"][:40],
            "bid":      b["bid_name"][:55],
            "winner":   clean_winner(b["winner"]),
            "budget_b": round(b["pi"]/1e9, 2),
            "award_b":  round(b["wp"]/1e9, 2),
            "disc_pct": disc,
            "cat":      device_cat(b["bid_name"]) or "Other",
            "year":     b["year"],
            "url":      b.get("source_url", ""),
        })

    # ── Analysis 2: Margin/discount by vendor ────────────────────────
    vendor_disc = defaultdict(list)
    vendor_wins = defaultdict(lambda: {"count": 0, "total_pi": 0.0, "total_wp": 0.0})
    for b in possible:
        if b["winner"] and b["wp"] > 0 and b["pi"] > 0:
            w = clean_winner(b["winner"])
            d = round((1 - b["wp"]/b["pi"])*100, 1)
            vendor_disc[w].append(d)
            vendor_wins[w]["count"]    += 1
            vendor_wins[w]["total_pi"] += b["pi"]
            vendor_wins[w]["total_wp"] += b["wp"]

    margin_table = []
    for w, discs in vendor_disc.items():
        vw = vendor_wins[w]
        overall_disc = round((1 - vw["total_wp"]/vw["total_pi"])*100, 1) if vw["total_pi"] > 0 else 0
        margin_table.append({
            "vendor":       w,
            "wins":         vw["count"],
            "value_b":      round(vw["total_wp"]/1e9, 1),
            "avg_disc":     round(sum(discs)/len(discs), 1),
            "overall_disc": overall_disc,
        })
    margin_table = sorted(margin_table, key=lambda x: -x["value_b"])[:20]

    # ── Analysis 3: Repeat wins — vendor loyalty per buyer ───────────
    vendor_buyer = defaultdict(lambda: defaultdict(lambda: {"count": 0, "value": 0.0}))
    for b in possible:
        if b["winner"] and b["wp"] > 0:
            w = clean_winner(b["winner"])
            vendor_buyer[w][b["investor"][:40]]["count"] += 1
            vendor_buyer[w][b["investor"][:40]]["value"] += b["wp"]

    repeat_rows = []
    for vendor, buyers in vendor_buyer.items():
        for buyer, stats in buyers.items():
            if stats["count"] >= 2:
                repeat_rows.append({
                    "vendor":   vendor,
                    "buyer":    buyer,
                    "wins":     stats["count"],
                    "value_b":  round(stats["value"]/1e9, 2),
                })
    repeat_rows = sorted(repeat_rows, key=lambda x: (-x["wins"], -x["value_b"]))[:25]

    # ── Channel bubble chart — dynamic vendor stats ──────────────────
    CHANNEL_VENDORS = [
        ("FPT IS",   ["HỆ THỐNG THÔNG TIN FPT", "FPT IS", "FPT INFORMATION"], "Recommended"),
        ("Sun Viet", ["SUN VIỆT", "SUN VIET", "VIỄN THÔNG TIN HỌC SUN", "SVTECH", "SV TECH", "SV-TECH"], "Enterprise compute"),
        ("Viettel",  ["VIỄN THÔNG QUÂN ĐỘI", "VIETTEL"],                       "Co-sell"),
        ("VNPT",     ["VNPT", "BƯU CHÍNH VIỄN THÔNG"],                         "Co-sell"),
        ("ƯKTS",     ["ỨNG DỤNG KỸ THUẬT VÀ SẢN XUẤT"],                       "Potential"),
    ]
    channel_vendors = []
    for label, keys, role in CHANNEL_VENDORS:
        bids  = [b for b in all_with_winner
                 if any(k in b["winner"].split("|")[0].upper() for k in keys)]
        wins  = len(bids)
        val   = sum(b["wp"] for b in bids)
        open_cnt = sum(1 for b in bids if b["bid_form"] in OPEN_FORMS)
        chct_cnt = sum(1 for b in bids if b["bid_form"] in CHCT_FORMS)
        cdnt_cnt = sum(1 for b in bids if b["bid_form"] in CDNT_FORMS)
        channel_vendors.append({
            "name":     label,
            "role":     role,
            "wins":     wins,
            "value_b":  round(val / 1e9, 1),
            "open_pct": round(open_cnt / max(wins, 1) * 100),
            "chct_pct": round(chct_cnt / max(wins, 1) * 100),
            "cdnt_pct": round(cdnt_cnt / max(wins, 1) * 100),
        })

    total_tech   = len(all_tech)
    apple_poss_v = sum(b["pi"] for b in possible if b["pi"] > 0)
    apple_expl_v = sum(b["pi"] for b in cats["apple_explicit"] if b["pi"] > 0)
    apple_curr   = sum(b["wp"] for b in all_with_winner
                       if any(k in (b["bid_name"] + "").lower()
                              for k in APPLE_EXPLICIT) and b["wp"] > 0)
    # Device-only total: apple_possible + apple_explicit (no infra/software)
    total_v      = sum(b["pi"] for b in all_tech
                       if b["pi"] > 0 and b["cat"] in ("apple_possible", "apple_explicit"))
    # All tech bids budget (incl. infra/software — for funnel chart stage 1)
    total_tech_v = sum(b["pi"] for b in all_tech if b["pi"] > 0)

    # Global bid form breakdown (awarded bids only)
    n_aw = max(len(all_with_winner), 1)
    chct_pct     = round(sum(1 for b in all_with_winner if b["bid_form"] in CHCT_FORMS) / n_aw * 100, 1)
    open_bid_pct = round(sum(1 for b in all_with_winner if b["bid_form"] in OPEN_FORMS)  / n_aw * 100, 1)
    cdnt_pct_g   = round(sum(1 for b in all_with_winner if b["bid_form"] in CDNT_FORMS)  / n_aw * 100, 1)

    # Windows requirement rate (of all tech bids)
    windows_pct  = round(len(cats.get("windows_locked", [])) / max(total_tech, 1) * 100, 1)

    # Year range for dynamic headline text
    yr_by_year = defaultdict(float)
    for k, v in yr.items():
        yr_by_year[k[:4]] += v["value"]
    yr_years = sorted(yr_by_year.keys())
    year_first  = yr_years[0]  if yr_years else "2022"
    year_last   = yr_years[-1] if yr_years else "2025"
    val_first_b = round(yr_by_year[year_first] / 1e9, 0) if yr_years else 0
    val_last_b  = round(yr_by_year[year_last]  / 1e9, 0) if yr_years else 0

    # Apple current capture: use winner_price if available, else budget estimate of explicit bids
    if apple_curr > 0:
        apple_curr_b   = round(apple_curr / 1e9, 1)
        apple_curr_est = False
    else:
        apple_curr_b   = round(apple_expl_v / 1e9, 1)
        apple_curr_est = True  # no winner_price found — using budget estimate
    apple_share_pct = round(apple_curr_b / max(apple_poss_v / 1e9, 0.1) * 100, 1)

    return {
        "generated_at":    datetime.now(timezone.utc).strftime("%B %Y"),
        "total_bids":      total_tech,
        "total_value_b":   round(total_v    / 1e9, 0),
        "total_tech_v_b":  round(total_tech_v / 1e9, 0),
        "apple_possible_b": round(apple_poss_v / 1e9, 0),
        "apple_explicit_b": round(apple_expl_v / 1e9, 0),
        "apple_current_b":  apple_curr_b,
        "apple_curr_est":   apple_curr_est,
        "apple_share_pct":  apple_share_pct,
        "chct_pct":      chct_pct,
        "open_bid_pct":  open_bid_pct,
        "cdnt_pct":      cdnt_pct_g,
        "windows_pct":   windows_pct,
        "year_range": {
            "first": year_first, "last": year_last,
            "val_first_b": val_first_b, "val_last_b": val_last_b,
        },
        "top_investors": top_investors,
        "year_trend": {
            yr_k: {"count": v["count"], "value_b": round(v["value"] / 1e9, 1)}
            for yr_k, v in sorted(yr.items())
            if not (yr_k.endswith("H2") and yr_k[:4] == str(datetime.now().year))
        },
        "device_cats": {
            k: {"count": v["count"], "value_b": round(v["value"] / 1e9, 1)}
            for k, v in sorted(dev_cats.items(), key=lambda x: -x[1]["value"])
        },
        "cat_summary": {
            cat: {"count": len(bids),
                  "value_b": round(sum(b["pi"] for b in bids if b["pi"] > 0) / 1e9, 1)}
            for cat, bids in cats.items()
        },
        "vendor_chart":  vendor_chart,
        "target_bids":   target_rows,
        "margin_table":  margin_table,
        "repeat_wins":   repeat_rows,
        "gap_chart":     gap_chart,
        "sunviet_chart": sunviet_chart,
        "acct_capture":    acct_capture,
        "channel_vendors": channel_vendors,
        "fptis_deals":     fptis_deals,
    }


# ── Generate HTML ─────────────────────────────────────────────────
def generate_html(d: dict) -> str:
    multiplier = round(d["apple_possible_b"] / max(d["apple_current_b"], 0.1))

    # Priority mapping — dùng keywords match với tên đầy đủ tiếng Việt
    HIGH_KEYWORDS = [
        "ĐẦU TƯ VÀ PHÁT TRIỂN",   # BIDV
        "NGOẠI THƯƠNG",             # Vietcombank
        "CÔNG THƯƠNG",              # Vietinbank
        "NÔNG NGHIỆP",              # Agribank
        "Tổng cục Thuế",
        "Tổng Cục Thuế",
        "Bảo hiểm xã hội",
        "Kho bạc Nhà nước",
        "Cục Công nghệ thông tin",
    ]
    MEDIUM_KEYWORDS = [
        "Điện lực", "EVN", "ĐIỆN LỰC",
        "Bệnh viện", "BỆNH VIỆN",
        "Chuyển đổi số",
        "Trang bị",
    ]

    def get_priority(name):
        if any(k in name for k in HIGH_KEYWORDS):
            return "High", "dot-high", "color:var(--blue)"
        if any(k in name for k in MEDIUM_KEYWORDS):
            return "Medium", "dot-medium", ""
        return "Monitor", "dot-low", "color:var(--gray)"

    def shorten_name(name, maxlen=999):
        return name

    top_inv_rows = ""
    for inv in d["top_investors"]:
        name     = inv["name"]
        plabel, dot_class, color_style = get_priority(name)
        winners  = inv.get("winners", [])

        # Render winner breakdown bars
        if winners:
            winner_html = '<div class="winner-breakdown">'
            for w in winners:
                bar_w = max(w['pct'], 4)
                winner_html += f"""
                  <div class="wb-row">
                    <div class="wb-name">{w['name']}</div>
                    <div class="wb-bar-wrap">
                      <div class="wb-bar" style="width:{bar_w}%"></div>
                    </div>
                    <div class="wb-pct">{w['pct']}%</div>
                  </div>"""
            winner_html += '</div>'
        else:
            winner_html = '<div class="winner-breakdown wb-empty">No awarded results yet</div>'

        top_inv_rows += f"""
        <div class="priority-card">
          <div class="buyer-name" title="{name}">{name}</div>
          <div class="buyer-budget">VND {inv['value_b']}B</div>
          <div class="buyer-bids">{inv['count']}</div>
          <div class="priority-badge">
            <span class="dot {dot_class}"></span>
            <span style="{color_style}">{plabel}</span>
          </div>
          <div class="buyer-winners">{winner_html}</div>
        </div>"""

    # Pre-compute table HTML (avoid nested f-strings, Python 3.10 limitation)
    target_rows_html = ""
    for r in d["target_bids"]:
        disc_color = "#FF3B30" if r["disc_pct"] > 10 else "var(--text2)"
        bid_cell = (
            f'<a href="{r["url"]}" target="_blank" style="color:var(--blue);text-decoration:none">{r["bid"]}</a>'
            if r.get("url") else r["bid"]
        )
        target_rows_html += (
            f'<tr><td style="font-size:11px">{r["buyer"]}</td>'
            f'<td style="font-size:11px;color:var(--text2)">{bid_cell}</td>'
            f'<td style="font-weight:600;font-size:12px">{r["winner"]}</td>'
            f'<td style="text-align:right;font-size:12px">VND {r["budget_b"]}B</td>'
            f'<td style="text-align:right;font-size:12px;color:var(--blue)">VND {r["award_b"]}B</td>'
            f'<td style="text-align:right;font-size:12px;color:{disc_color}">{r["disc_pct"]}%</td>'
            f'<td style="font-size:11px;color:var(--text2)">{r["cat"]}</td>'
            f'<td style="font-size:11px;color:var(--text2)">{r["year"]}</td></tr>'
        )

    repeat_rows_html = ""
    for r in d["repeat_wins"]:
        repeat_rows_html += (
            f'<tr><td style="font-weight:600;font-size:12px">{r["vendor"]}</td>'
            f'<td style="font-size:11px;color:var(--text2)">{r["buyer"]}</td>'
            f'<td style="text-align:center;font-size:13px;font-weight:700;color:var(--blue)">{r["wins"]}</td>'
            f'<td style="text-align:right;font-size:12px">VND {r["value_b"]}B</td></tr>'
        )

    fptis_rows_html = ""
    for r in d["fptis_deals"]:
        bid_cell = (
            f'<a href="{r["url"]}" target="_blank" style="color:var(--blue);text-decoration:none">{r["bid"]}</a>'
            if r.get("url") else r["bid"]
        )
        fptis_rows_html += (
            f'<tr><td style="font-size:11px">{r["buyer"]}</td>'
            f'<td style="font-size:11px;color:var(--text2)">{bid_cell}</td>'
            f'<td style="text-align:right;font-size:12px;font-weight:600;color:var(--blue)">VND {r["award_b"]}B</td>'
            f'<td style="font-size:11px;color:var(--text2)">{r["form"]}</td>'
            f'<td style="font-size:11px;color:var(--text2)">{r["year"]}</td></tr>'
        )

    yr_labels = list(d["year_trend"].keys())
    yr_values = [v["value_b"] for v in d["year_trend"].values()]
    yr_counts = [v["count"] for v in d["year_trend"].values()]
    dev_labels = list(d["device_cats"].keys())
    dev_values = [v["value_b"] for v in d["device_cats"].values()]

    last_period = yr_labels[-1] if yr_labels else d["generated_at"]
    trend_annotation_text = f"Data through {d['generated_at']}"

    yr = d["year_range"]
    _curr_note = " (ước tính từ budget gói thầu — chưa có dữ liệu winner_price)" if d["apple_curr_est"] else ""

    html = f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vietnam Government IT Procurement — Apple Vietnam Sales — {d['generated_at']}</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

:root {{
  --bg: #F5F5F7;
  --card: #FFFFFF;
  --text: #1D1D1F;
  --text2: #6E6E73;
  --blue: #0071E3;
  --border: #D2D2D7;
  --gray: #86868B;
  --font: -apple-system, "SF Pro Display", "SF Pro Text", system-ui, sans-serif;
}}

html {{ scroll-behavior: smooth; }}

body {{
  font-family: var(--font);
  background: var(--bg);
  color: var(--text);
  font-size: 17px;
  line-height: 1.47059;
  -webkit-font-smoothing: antialiased;
}}

nav {{
  position: sticky;
  top: 0;
  z-index: 100;
  background: rgba(245,245,247,0.85);
  backdrop-filter: saturate(180%) blur(20px);
  border-bottom: 1px solid var(--border);
  padding: 0 48px;
  height: 48px;
  display: flex;
  align-items: center;
  gap: 32px;
}}

nav a {{
  font-size: 12px;
  font-weight: 400;
  color: var(--text);
  text-decoration: none;
  letter-spacing: -0.01em;
  white-space: nowrap;
  opacity: 0.7;
  transition: opacity 0.2s;
}}
nav a:hover {{ opacity: 1; color: var(--blue); }}

header {{
  background: var(--card);
  border-bottom: 1px solid var(--border);
  padding: 40px 64px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}}

.header-left {{ display: flex; align-items: center; gap: 24px; }}
.logo svg {{ width: 28px; height: 34px; fill: var(--text); }}
.header-titles h1 {{
  font-size: 21px;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--text);
}}
.header-titles p {{
  font-size: 14px;
  color: var(--text2);
  margin-top: 2px;
}}
.header-right {{
  font-size: 12px;
  color: var(--text2);
  text-align: right;
  line-height: 1.6;
}}

main {{ max-width: 1200px; margin: 0 auto; padding: 0 48px 80px; }}

section {{ padding: 80px 0 0; }}
section + section {{ border-top: 1px solid var(--border); margin-top: 80px; }}

.section-label {{
  font-size: 11px;
  font-weight: 600;
  color: var(--blue);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  margin-bottom: 16px;
}}

.section-headline {{
  font-size: 28px;
  font-weight: 700;
  letter-spacing: -0.03em;
  color: var(--text);
  line-height: 1.14286;
  max-width: 800px;
  margin-bottom: 48px;
}}

.kpi-row {{
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 32px;
}}

.kpi-card {{
  background: var(--card);
  border-radius: 18px;
  padding: 32px 28px;
  box-shadow: 0 2px 20px rgba(0,0,0,0.08);
}}

.kpi-value {{
  font-size: 48px;
  font-weight: 700;
  letter-spacing: -0.04em;
  color: var(--text);
  line-height: 1;
  margin-bottom: 8px;
}}

.kpi-value.blue {{ color: var(--blue); }}

.kpi-label {{
  font-size: 14px;
  color: var(--text2);
  line-height: 1.4;
}}

.insight-bar {{
  background: #F0F7FF;
  border-left: 3px solid var(--blue);
  border-radius: 0 12px 12px 0;
  padding: 20px 24px;
  font-size: 15px;
  color: var(--text);
  line-height: 1.5;
}}

.stat-row {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
  margin-top: 32px;
}}

.stat-box {{
  background: var(--card);
  border-radius: 14px;
  padding: 24px;
  box-shadow: 0 2px 20px rgba(0,0,0,0.08);
  text-align: center;
}}

.stat-box .stat-num {{
  font-size: 28px;
  font-weight: 700;
  letter-spacing: -0.025em;
  color: var(--blue);
  margin-bottom: 6px;
}}

.stat-box .stat-desc {{
  font-size: 13px;
  color: var(--text2);
  line-height: 1.4;
}}

.chart-container {{
  background: var(--card);
  border-radius: 18px;
  box-shadow: 0 2px 20px rgba(0,0,0,0.08);
  padding: 32px;
  margin-bottom: 24px;
}}

.chart-title {{
  font-size: 17px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 4px;
  letter-spacing: -0.01em;
}}

.chart-sub {{
  font-size: 13px;
  color: var(--text2);
  margin-bottom: 20px;
}}

.two-col {{
  display: grid;
  grid-template-columns: 60fr 40fr;
  gap: 20px;
  align-items: start;
}}

.priority-grid {{
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 32px;
}}

.priority-card {{
  background: var(--card);
  border-radius: 14px;
  box-shadow: 0 2px 20px rgba(0,0,0,0.08);
  display: grid;
  grid-template-columns: 2fr 120px 50px 100px;
  grid-template-rows: auto auto;
  align-items: start;
  padding: 20px 28px;
  gap: 8px 16px;
}}
.buyer-winners {{
  grid-column: 1 / -1;
  padding-top: 10px;
  border-top: 1px solid #F5F5F7;
  margin-top: 4px;
}}
.winner-breakdown {{ display: flex; flex-direction: column; gap: 5px; }}
.wb-empty {{ font-size: 12px; color: var(--text2); font-style: italic; }}
.wb-row {{ display: grid; grid-template-columns: 1fr 120px 36px; gap: 8px; align-items: center; }}
.wb-name {{ font-size: 11px; color: var(--text2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.wb-bar-wrap {{ background: #F0F0F5; border-radius: 4px; height: 6px; overflow: hidden; }}
.wb-bar {{ height: 100%; background: var(--blue); border-radius: 4px; opacity: 0.7; }}
.wb-pct {{ font-size: 11px; font-weight: 600; color: var(--text); text-align: right; }}

.priority-card.header-row {{
  background: transparent;
  box-shadow: none;
  padding: 0 28px 8px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 4px;
}}

.priority-card.header-row span {{
  font-size: 12px;
  font-weight: 600;
  color: var(--text2);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}}

.buyer-name {{ font-size: 13px; font-weight: 600; color: var(--text); line-height: 1.4; word-break: break-word; }}
.buyer-budget {{ font-size: 15px; font-weight: 700; color: var(--text); letter-spacing: -0.02em; }}
.buyer-bids {{ font-size: 14px; color: var(--text2); text-align: center; }}

.priority-badge {{
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 500;
}}

.dot {{
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}}
.dot-high {{ background: var(--blue); }}
.dot-medium {{ background: var(--gray); }}
.dot-low {{ background: var(--border); }}

.archetype-grid {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 20px;
  margin-bottom: 32px;
}}

.archetype-card {{
  background: var(--card);
  border-radius: 18px;
  box-shadow: 0 2px 20px rgba(0,0,0,0.08);
  padding: 36px 28px;
}}

.archetype-number {{
  font-size: 11px;
  font-weight: 600;
  color: var(--text2);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-bottom: 12px;
}}

.archetype-title {{
  font-size: 20px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--text);
  margin-bottom: 16px;
}}

.archetype-examples {{
  font-size: 13px;
  color: var(--text2);
  margin-bottom: 12px;
}}

.archetype-stat {{
  font-size: 24px;
  font-weight: 700;
  letter-spacing: -0.03em;
  color: var(--text);
  margin-bottom: 8px;
  line-height: 1.1;
}}

.archetype-desc {{
  font-size: 14px;
  color: var(--text2);
  line-height: 1.5;
  margin-bottom: 20px;
}}

.archetype-fit {{
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  padding-top: 20px;
  border-top: 1px solid var(--border);
}}

.fit-yes {{ color: var(--blue); }}
.fit-no {{ color: var(--gray); }}

.fit-icon {{
  width: 20px;
  height: 20px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 700;
  flex-shrink: 0;
}}

.fit-icon-yes {{ background: #E8F4FF; color: var(--blue); }}
.fit-icon-no {{ background: #F5F5F7; color: var(--gray); }}

.action-grid {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 20px;
}}

.action-card {{
  background: var(--card);
  border-radius: 18px;
  box-shadow: 0 2px 20px rgba(0,0,0,0.08);
  padding: 36px 32px;
  display: flex;
  flex-direction: column;
}}

.action-number {{
  font-size: 56px;
  font-weight: 700;
  letter-spacing: -0.04em;
  color: var(--blue);
  line-height: 1;
  margin-bottom: 20px;
}}

.action-timeline {{
  display: inline-block;
  font-size: 11px;
  font-weight: 600;
  color: var(--blue);
  background: #E8F4FF;
  padding: 3px 10px;
  border-radius: 100px;
  margin-bottom: 16px;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  align-self: flex-start;
}}

.action-title {{
  font-size: 19px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--text);
  margin-bottom: 12px;
}}

.action-body {{
  font-size: 14px;
  color: var(--text2);
  line-height: 1.65;
  margin-bottom: 16px;
  flex: 1;
}}

.action-target {{
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  border-top: 1px solid var(--border);
  padding-top: 14px;
}}

.action-target span {{
  font-weight: 400;
  color: var(--text2);
}}

.data-table {{ width:100%; border-collapse:collapse; font-family:var(--font); }}
.data-table thead tr {{ border-bottom: 2px solid var(--border); }}
.data-table thead th {{ font-size:11px; font-weight:600; color:var(--text2); text-transform:uppercase;
  letter-spacing:.05em; padding:10px 12px; text-align:left; white-space:nowrap; }}
.data-table tbody tr {{ border-bottom:1px solid #F5F5F7; }}
.data-table tbody tr:hover {{ background:#FAFAFA; }}
.data-table tbody td {{ padding:10px 12px; vertical-align:top; }}

.glossary-grid {{
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
}}

.glossary-card {{
  background: #F5F5F7;
  border-radius: 14px;
  padding: 20px 24px;
}}

.glossary-term {{
  font-size: 15px;
  font-weight: 700;
  color: var(--text);
  margin-bottom: 6px;
}}

.glossary-def {{
  font-size: 14px;
  color: var(--text2);
  line-height: 1.6;
}}

footer {{
  border-top: 1px solid var(--border);
  margin-top: 80px;
  padding: 48px 64px;
  background: var(--card);
}}

.footer-grid {{
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 32px;
}}

.footer-left svg {{ fill: var(--text2); opacity: 0.4; width: 20px; height: 24px; margin-bottom: 12px; display: block; }}

.footer-meta {{
  font-size: 12px;
  color: var(--text2);
  line-height: 1.8;
}}

.footer-right {{
  text-align: right;
  font-size: 12px;
  color: var(--text2);
  line-height: 1.8;
}}

@media print {{
  nav {{ display: none; }}
  body {{ background: white; }}
  .kpi-card, .chart-container, .priority-card, .archetype-card, .action-card, .stat-box {{
    box-shadow: none;
    border: 1px solid var(--border);
  }}
  section {{ page-break-inside: avoid; }}
}}

@media (max-width: 900px) {{
  nav {{ padding: 0 24px; gap: 20px; overflow-x: auto; }}
  header {{ padding: 32px 24px; flex-direction: column; align-items: flex-start; gap: 16px; }}
  main {{ padding: 0 24px 60px; }}
  .kpi-row {{ grid-template-columns: repeat(2, 1fr); }}
  .two-col {{ grid-template-columns: 1fr; }}
  .archetype-grid {{ grid-template-columns: 1fr; }}
  .action-grid {{ grid-template-columns: 1fr; }}
  .priority-card {{ grid-template-columns: 1fr 1fr; }}
  .glossary-grid {{ grid-template-columns: 1fr; }}
}}

.story-map {{
  position: sticky;
  top: 48px;
  z-index: 99;
  background: white;
  border-bottom: 1px solid #D2D2D7;
  padding: 10px 48px;
  display: flex;
  gap: 8px;
  align-items: center;
  overflow-x: auto;
}}
.story-pill {{
  font-size: 11px;
  font-weight: 500;
  padding: 5px 14px;
  border-radius: 100px;
  border: 1px solid #D2D2D7;
  color: #6E6E73;
  text-decoration: none;
  white-space: nowrap;
  transition: all 0.15s;
}}
.story-pill:hover, .story-pill.highlight {{
  background: #0071E3;
  color: white;
  border-color: #0071E3;
}}
.story-arrow {{ color: #D2D2D7; font-size: 13px; flex-shrink: 0; }}

.insight-callout {{
  background: #F0F7FF;
  border-left: 3px solid #0071E3;
  border-radius: 0 12px 12px 0;
  padding: 18px 22px;
  font-size: 15px;
  color: #1D1D1F;
  line-height: 1.55;
  margin-top: 20px;
}}
.insight-callout strong {{ color: #0071E3; }}

.closing-callout {{
  background: #FFFBF0;
  border-left: 3px solid #F5A623;
  border-radius: 0 12px 12px 0;
  padding: 20px 24px;
  font-size: 15px;
  color: #1D1D1F;
  line-height: 1.6;
  margin-top: 28px;
}}
</style>
</head>
<body>

<nav>
  <a href="#s1">The Opportunity</a>
  <a href="#s2">Market Structure</a>
  <a href="#s3">Who's Winning</a>
  <a href="#s4">The Gap</a>
  <a href="#s5">Channel</a>
  <a href="#s6">Why Vendors Win</a>
  <a href="#s7">Quick Wins</a>
  <a href="#s7b">Action Plan</a>
  <a href="#s8fptis">FPT IS Deals</a>
  <a href="#s8">Evidence Base</a>
  <a href="#s9">Glossary</a>
</nav>

<div class="story-map">
  <a href="#s0" class="story-pill active">★ What to do now</a>
  <div class="story-arrow">→</div>
  <a href="#s1" class="story-pill">01 · Invisible Giant</a>
  <div class="story-arrow">→</div>
  <a href="#s2" class="story-pill">02 · Relationship Market</a>
  <div class="story-arrow">→</div>
  <a href="#s3" class="story-pill">03 · Price War</a>
  <div class="story-arrow">→</div>
  <a href="#s4" class="story-pill">04 · The Gap</a>
  <div class="story-arrow">→</div>
  <a href="#s5" class="story-pill">05 · The Landscape</a>
  <div class="story-arrow">→</div>
  <a href="#s6" class="story-pill">06 · Sun Viet</a>
  <div class="story-arrow">→</div>
  <a href="#s7" class="story-pill">07 · Quick Wins</a>
  <div class="story-arrow">→</div>
  <a href="#s7b" class="story-pill">✦ Action Plan</a>
</div>

<header>
  <div class="header-left">
    <div class="logo">
      <svg viewBox="0 0 814 1000" xmlns="http://www.w3.org/2000/svg">
        <path d="M788.1 340.9c-5.8 4.5-108.2 62.2-108.2 190.5 0 148.4 130.3 200.9 134.2 202.2-.6 3.2-20.7 71.9-68.7 141.9-42.8 61.6-87.5 123.1-155.5 123.1s-85.5-39.5-164-39.5c-76 0-103.7 40.8-165.9 40.8s-105-57.8-155.5-127.4C46 790.7 0 663 0 541.8c0-207.1 134.8-316.5 267.4-316.5 70.1 0 128.4 46.4 172.5 46.4 42.4 0 109.2-49.8 186.4-49.8 30.5 0 110.5 2.6 170.3 85.1zm-252.3-166.1c31.1-36.9 53.1-88.1 53.1-139.3 0-7.1-.6-14.3-1.9-20.1-50.6 1.9-110.8 33.7-147.1 75.8-28.5 32.4-55.1 83.6-55.1 135.5 0 7.8 1.3 15.6 1.9 18.1 3.2.6 8.4 1.3 13.6 1.3 45.4 0 102.5-30.4 135.5-71.3z"/>
      </svg>
    </div>
    <div class="header-titles">
      <h1>Vietnam Government IT Procurement</h1>
      <p>Sales Intelligence Report — {d['generated_at']}</p>
    </div>
  </div>
  <div class="header-right">
    Confidential · Apple Vietnam Sales<br>
    <span style="color: var(--border);">Internal Use Only</span>
  </div>
</header>

<main>

<section id="s0" style="background:linear-gradient(135deg,#003D82 0%,#0071E3 100%);border-radius:18px;padding:48px 44px;margin-bottom:56px;color:#fff">
  <div style="font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;opacity:.85;margin-bottom:14px">Bottom line · What to do now</div>
  <p style="font-size:26px;font-weight:700;line-height:1.3;letter-spacing:-.02em;margin:0 0 14px;max-width:820px">VND {int(d['apple_possible_b']):,}B of device procurement is open to Apple with no OS barrier. Capturing it needs two partner conversations — not a new product, price, or budget cycle.</p>
  <p style="font-size:15px;line-height:1.6;opacity:.9;max-width:760px;margin:0 0 32px">Apple holds {d['apple_share_pct']}% today — a {multiplier}× upside. The plays below are ranked by speed-to-revenue. Each names who to contact and the first target. Full evidence in sections 01–09.</p>

  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px">
    <div style="background:rgba(255,255,255,.12);border-radius:14px;padding:22px 24px;backdrop-filter:blur(6px)">
      <div style="font-size:11px;font-weight:600;letter-spacing:.05em;opacity:.85;margin-bottom:8px">THIS WEEK · ZERO COST</div>
      <div style="font-size:16px;font-weight:700;line-height:1.35;margin-bottom:10px">Co-sell meeting with FPT IS</div>
      <div style="font-size:13px;line-height:1.55;opacity:.88">Already Apple Authorized Reseller with live contracts at BIDV, Vietcombank, Tổng cục Thuế — but has never proposed Apple in a device bid. <b>Contact:</b> FPT IS device/public-sector sales lead.</div>
      <div style="font-size:20px;font-weight:800;margin-top:14px">VND 529B unlocked</div>
    </div>
    <div style="background:rgba(255,255,255,.12);border-radius:14px;padding:22px 24px;backdrop-filter:blur(6px)">
      <div style="font-size:11px;font-weight:600;letter-spacing:.05em;opacity:.85;margin-bottom:8px">MONTH 1–3 · REFERENCE CASE</div>
      <div style="font-size:16px;font-weight:700;line-height:1.35;margin-bottom:10px">500-unit iPad pilot at Tổng cục Thuế</div>
      <div style="font-size:13px;line-height:1.55;opacity:.88">Structured as direct appointment via FPT IS (VND 316B foothold there). Proof point that unlocks 63 provinces. <b>Contact:</b> Tổng cục Thuế IT procurement, via FPT IS.</div>
      <div style="font-size:20px;font-weight:800;margin-top:14px">VND 20B → 150B+</div>
    </div>
    <div style="background:rgba(255,255,255,.12);border-radius:14px;padding:22px 24px;backdrop-filter:blur(6px)">
      <div style="font-size:11px;font-weight:600;letter-spacing:.05em;opacity:.85;margin-bottom:8px">MONTH 4–8 · ENTERPRISE COMPUTE</div>
      <div style="font-size:16px;font-weight:700;line-height:1.35;margin-bottom:10px">Mac Studio Ultra to Sun Viet for Viettel</div>
      <div style="font-size:13px;line-height:1.55;opacity:.88">Sun Viet (= SVTech) won VND 1,346B at Viettel via open bids on HPC/compute. Mac Studio as AI inference node is a credible TCO play. <b>Contact:</b> Sun Viet enterprise compute team.</div>
      <div style="font-size:20px;font-weight:800;margin-top:14px">Viettel DC refresh</div>
    </div>
  </div>

  <div style="font-size:12px;opacity:.75;margin-top:24px">Detailed plan in <a href="#s7b" style="color:#fff;text-decoration:underline">✦ Action Plan</a>. The conversations that matter — FPT IS on devices, Sun Viet on compute — have not happened yet.</div>
</section>

<section id="s1">
  <div class="section-label">01 · Act One — The Invisible Giant</div>
  <p class="section-headline">A {round(d['year_range']['val_last_b'] / max(d['year_range']['val_first_b'], 1))}× growing market. Apple holds {d['apple_share_pct']}% of addressable device procurement. The only barrier is activation — and that is fully within reach.</p>
  <p style="font-size:16px;color:var(--text2);margin-top:-32px;margin-bottom:48px;max-width:720px;line-height:1.6">Vietnam government IT device procurement has grown from VND {int(d['year_range']['val_first_b']):,}B in {d['year_range']['first']} to VND {int(d['year_range']['val_last_b']):,}B in {d['year_range']['last']}. Apple's near-zero share is not a product problem, not a pricing problem, and not a demand problem — it is a channel and presence problem. The market is open. The timing is right. The question is who moves first.</p>

  <div class="kpi-row">
    <div class="kpi-card">
      <div class="kpi-value">{int(d['total_value_b']):,}</div>
      <div class="kpi-label">B VND · Total Device Procurement (Laptop + Tablet + Desktop)</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-value blue">{int(d['apple_possible_b']):,}</div>
      <div class="kpi-label">B VND · Apple-Addressable (no OS requirement)</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-value">{d['apple_current_b']}</div>
      <div class="kpi-label">B VND · Apple's Captured Addressable Market ({d['apple_share_pct']}%){_curr_note}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-value blue">{multiplier}×</div>
      <div class="kpi-label">Potential vs. Current — upside multiplier</div>
    </div>
  </div>

  <div class="insight-bar">
    Only {d['windows_pct']}% of bids explicitly require Windows-locked software. The market is structurally open — Apple simply isn't showing up.
  </div>

  <div style="font-size:12px;color:#86868B;line-height:1.6;padding:12px 16px;background:#F9F9F9;border-radius:8px;margin-top:12px">
    <strong style="color:#1D1D1F">Data methodology:</strong>
    Data source: Vietnam Government E-Procurement Portal (muasamcong.mpi.gov.vn).
    Collection period: 2022 Q1 – {d['generated_at']} (data extracted continuously; historical records may be incomplete for earlier periods).
    From June 2026: category-based collection (investField = Hàng hóa + Hỗn hợp). Prior periods: keyword-based collection (laptop, tablet, desktop, CNTT, etc.).
    Estimated coverage: 60–75% of actual addressable market — bids with generic names may be missed.
    Absolute VND values = floor estimates. Vendor rankings and trend direction are reliable. Discount calculations use published budget estimate (dự toán) and awarded price (giá trúng thầu) from portal records.
  </div>

  <div class="two-col" style="margin-top:24px">
    <div class="chart-container">
      <div class="chart-title">Apple's Share of the Addressable Market</div>
      <div class="chart-sub">VND {d['apple_current_b']}B captured out of VND {int(d['apple_possible_b']):,}B addressable. {d['apple_share_pct']}%.</div>
      <div id="share-donut" style="height:280px;"></div>
    </div>
    <div class="chart-container">
      <div class="chart-title">H1 vs. H2 Pattern — and the 2026 H2 Opportunity</div>
      <div class="chart-sub">H2 is consistently 1.5–2× larger than H1. Based on this pattern, 2026 H2 is forecast to be the largest single half-year in this dataset — and the window is now.</div>
      <div id="growth-compare-chart" style="height:320px;"></div>
    </div>
  </div>
</section>

<section id="s2">
  <div class="section-label">02 · Act Two — The Market Structure</div>
  <p class="section-headline">The market runs two parallel games — and Apple can win both, with the right playbook for each.</p>
  <p style="font-size:16px;color:var(--text2);margin-top:-32px;margin-bottom:48px;max-width:720px;line-height:1.6">muasamcong covers competitive procurement only. Awarded contracts break down into: <b>{d['open_bid_pct']}%</b> Đấu thầu rộng rãi / Hạn chế (open &amp; restricted competitive bidding — DTRR/HCQT) and <b>{d['chct_pct']}%</b> Chào hàng cạnh tranh (competitive shopping — CHCT/CHCTRG, simplified 3-vendor quotes). Chỉ định thầu and Mua sắm trực tiếp (direct/non-competitive forms) do not appear here — they bypass the portal entirely.</p>

  <div class="chart-container">
    <div class="chart-title">Market Funnel — From Total Tech Spend to Apple Addressable</div>
    <div class="chart-sub">Budget in VND billions (B) at each filter stage — from all keyword-matched tech bids down to Apple-named bids</div>
    <div id="funnel-chart" style="height:320px;"></div>
  </div>

  <div class="insight-callout">
    <strong>Đấu thầu rộng rãi — Open Bidding ({d['open_bid_pct']}%):</strong> Full competitive tender open to any qualified vendor. Published RFP, technical evaluation, scored proposal. Largest contracts. Apple enters here through any authorized reseller — FPT IS is already positioned.
    <br><br>
    <strong>Chào hàng cạnh tranh — Competitive Shopping ({d['chct_pct']}%):</strong> Simplified competitive process — 3 vendors submit quotes. Still competitive, not appointed. Smaller contracts, faster cycle. Apple wins here when a trusted reseller with an existing relationship is one of the 3 invitees.
  </div>

  <div class="stat-row">
    <div class="stat-box">
      <div class="stat-num">{d['total_bids']:,}</div>
      <div class="stat-desc">total tech bids analyzed</div>
    </div>
    <div class="stat-box">
      <div class="stat-num">{d['windows_pct']}%</div>
      <div class="stat-desc">of bids explicitly require Windows-locked software</div>
    </div>
    <div class="stat-box">
      <div class="stat-num">{multiplier}×</div>
      <div class="stat-desc">addressable vs. current Apple captured value</div>
    </div>
  </div>
</section>

<section id="s3">
  <div class="section-label">03 · Act Three — Who's Winning Right Now</div>
  <p class="section-headline">Small resellers dominate device procurement by cutting price — not by delivering quality. That sustainability gap is where Apple enters.</p>
  <p style="font-size:16px;color:var(--text2);margin-top:-32px;margin-bottom:48px;max-width:720px;line-height:1.6">The top 20 device vendors are competing on one dimension: price. Their average discount exceeds 10%. This is not a market Apple should join on those terms — it's a market Apple should disrupt with a TCO argument.</p>

  <div class="two-col">
    <div class="chart-container">
      <div class="chart-title">Device Categories by Budget (VND B)</div>
      <div class="chart-sub">Apple-possible bids, by device type</div>
      <div id="device-chart" style="height:320px;"></div>
    </div>
    <div class="chart-container">
      <div class="chart-title">Device Budget by Period — Only Apple-Possible Bids</div>
      <div class="chart-sub">Estimated budget for laptop/tablet/desktop bids only (VND B)</div>
      <div id="trend-chart-2" style="height:320px;"></div>
    </div>
  </div>

  <div class="chart-container" style="margin-top:20px">
    <div class="chart-title">How Vendors Are Winning — Price Discount vs. Total Awarded Value</div>
    <div class="chart-sub">Red = aggressive discounting (&gt;15%). Orange = moderate (8–15%). Blue = differentiated/premium (&lt;8%). Bar length = total awarded value (VND B).</div>
    <div style="font-size:12px;color:#86868B;padding:8px 12px;background:#F5F5F7;border-radius:6px;margin-bottom:12px">
      <strong style="color:#1D1D1F">How discount is calculated:</strong> Discount % = (Budget Estimate − Awarded Price) / Budget Estimate × 100.
      Budget Estimate (dự toán / priceInit) = government's published ceiling before bidding.
      Awarded Price (giá trúng thầu / winner_price) = final contract value after competitive process.
      A 15% discount means the vendor won at 85% of the budgeted amount.
    </div>
    <div id="margin-chart" style="height:460px;"></div>
  </div>
  <div class="insight-callout">
    <strong>The pattern is clear:</strong> vendors discounting &gt;15% are trading margin for volume — unsustainable and often followed by poor post-sale service. When their buyer's next refresh cycle arrives, they will be looking for alternatives. <strong>These accounts are Apple's warmest leads.</strong>
  </div>
</section>

<section id="s4">
  <div class="section-label">04 · Act Four — The Gap</div>
  <p class="section-headline">The right buyers are spending. The wrong vendors are winning. FPT IS has the access — but hasn't walked in with Apple yet.</p>
  <p style="font-size:16px;color:var(--text2);margin-top:-32px;margin-bottom:48px;max-width:720px;line-height:1.6">BIDV, Vietcombank, Vietinbank, and Tổng cục Thuế collectively control VND 529B in annual device procurement. Every year, that budget flows to small resellers competing on price. FPT Information Systems is already trusted at all four institutions — but has never shown up to their device bid with an Apple product.</p>

  <div class="priority-grid">
    <div class="priority-card header-row">
      <span>Buyer</span>
      <span>Budget</span>
      <span style="text-align:center;">Bids</span>
      <span>Priority</span>
      <span style="grid-column:1/-1;padding-top:4px">Current top winners (% of awarded device value)</span>
    </div>
    {top_inv_rows}
  </div>

  <div class="chart-container" style="margin-top:28px">
    <div class="chart-title">Who Is Winning Their Device Budgets Right Now</div>
    <div class="chart-sub">Top 20 vendors by awarded device contract value (winner_price) — stacked by device type (Laptop / Tablet / Desktop). These are the companies FPT IS needs to displace.</div>
    <div id="vendor-cat-chart" style="height:520px;"></div>
  </div>

  <div class="insight-callout" style="margin-top:12px">
    <strong>Why FPT IS doesn't appear in this chart:</strong> FPT IS wins contracts at BIDV, Vietcombank, and Tổng cục Thuế — but in IT <em>services and systems integration</em>, not device procurement. Device bids at these accounts go to smaller resellers competing on price. FPT IS has never submitted an Apple device bid at any of these accounts. Their absence from this chart is the opportunity — not a data gap.
  </div>

  <div class="chart-container" style="margin-top:20px">
    <div class="chart-title">Which Vendor Owns Each Account's Device Budget</div>
    <div class="chart-sub">Top 4 vendors by device contract value per account. No FPT IS in any of these bars — that is the gap.</div>
    <div id="acct-capture-chart" style="height:360px;"></div>
  </div>

  <div class="chart-container" style="margin-top:20px">
    <div class="chart-title">FPT IS Institutional Presence vs. Device Procurement Gap</div>
    <div class="chart-sub">Blue = FPT IS total contracts at this account (all categories — software, infra, services). Gray = device contracts awarded to other vendors. The blue shows trust. The gray shows the opportunity.</div>
    <div id="gap-chart" style="height:320px;"></div>
  </div>
  <div class="insight-callout">
    <strong>The case for activating FPT IS is in this chart.</strong> At Tổng cục Thuế, FPT IS has won VND 316B in contracts — more than any other vendor. Yet their device bids (VND 170B going to competitors) are entirely untouched. At BIDV and Vietcombank, the gap is even starker. One co-sell agreement gives Apple access to every upcoming device procurement cycle at these accounts without building a single new relationship from scratch.
  </div>
</section>

<section id="s5">
  <div class="section-label">05 · Act Five — The Competitive Map</div>
  <p class="section-headline">"1,331 vendors in this market. Three ways to win. Only one is compatible with Apple."</p>
  <p style="font-size:16px;color:var(--text2);margin-top:-32px;margin-bottom:48px;max-width:720px;line-height:1.6">The bubble chart maps every vendor by how they win: through existing relationships (CHCT — invited to quote), price aggression (lowest bid), or open technical merit (DTRR). Three archetypes emerge — and only one is compatible with Apple.</p>

  <div class="chart-container">
    <div class="chart-title">Channel Landscape — Vendor Competitiveness vs. Contract Wins</div>
    <div class="chart-sub">X = % wins via open bidding · Y = number of contract wins · Bubble size = total contract value (VND B)</div>
    <div id="channel-chart" style="height:460px;"></div>
  </div>

  <div class="archetype-grid" style="margin-top:40px">
    <div class="archetype-card">
      <div class="archetype-number">Archetype 01</div>
      <div class="archetype-title">Competitive Shopping Dominance</div>
      <div class="archetype-examples">Viettel, VNPT, Gia Bảo</div>
      <div class="archetype-stat">{next((v['chct_pct'] for v in d['channel_vendors'] if v['name']=='Viettel'), '—')}%</div>
      <div class="archetype-desc">of Viettel's wins via Chào hàng cạnh tranh (CHCT — 3-vendor competitive shopping). Buyers invite Viettel to quote alongside 2 others because they already trust them.
        <br><br><strong style="color:#1D1D1F">Note on Viettel's dual role:</strong> Viettel appears in this chart as a <em>seller</em> — they win IT contracts at government agencies. Separately, Viettel is also one of the <em>largest buyers</em> of IT infrastructure in Vietnam (VND 2,791B+). These are two distinct Apple plays: co-sell through Viettel as a channel partner (device bids), and sell Mac Studio <em>to</em> Viettel via SVTech (enterprise compute). See Section 06.</div>
      <div class="archetype-fit" style="color:#F5A623">
        <div class="fit-icon" style="background:#FFF8E8;color:#F5A623">→</div>
        Apple's play: co-sell with Viettel/VNPT. When they are invited to submit a quote, Apple should be on their product list. Requires a reseller agreement, not a new relationship.
      </div>
    </div>
    <div class="archetype-card">
      <div class="archetype-number">Archetype 02</div>
      <div class="archetype-title">Price Aggression</div>
      <div class="archetype-examples">NLT, Tek-Solution, Phi Long</div>
      <div class="archetype-stat">15–20%</div>
      <div class="archetype-desc">average price cuts below budget estimate. Wins on lowest quote in CHCT or open bidding. Thin margin, high churn, low post-sale service quality.</div>
      <div class="archetype-fit fit-no">
        <div class="fit-icon fit-icon-no">✕</div>
        Apple cannot match on price — and shouldn't try. The play is to wait for these vendors to underdeliver, then present a TCO case to the buyer at the next refresh cycle.
      </div>
    </div>
    <div class="archetype-card">
      <div class="archetype-number">Archetype 03</div>
      <div class="archetype-title">Technical Capability</div>
      <div class="archetype-examples">Sun Viet, FPT Information Systems, ƯKTS</div>
      <div class="archetype-stat">{next((v['open_pct'] for v in d['channel_vendors'] if v['name']=='Sun Viet'), '—')}%</div>
      <div class="archetype-desc">of SVTech wins via open bidding (Đấu thầu rộng rãi). They win on technical merit in full competitive tenders — not through relationships or price discounting.</div>
      <div class="archetype-fit fit-yes">
        <div class="fit-icon fit-icon-yes">✓</div>
        Apple's primary channel. These vendors win on merit — Apple products strengthen their technical proposal, not weaken it.
      </div>
    </div>
  </div>
</section>

<section id="s6">
  <div class="section-label">06 · Partner Spotlight — Enterprise Compute</div>
  <h2 class="section-headline">Sun Viet: the partner who already sits inside Viettel's procurement room.</h2>
  <p style="font-size:16px;color:var(--text2);margin-top:-32px;margin-bottom:48px;max-width:720px;line-height:1.6">While FPT IS is the key to banking and government device bids, Sun Viet opens a completely different door — the enterprise compute market inside Vietnam's largest telecom. VND 1,346B in Viettel contracts, all won through open competition.</p>
  <div class="divider" style="height:1px;background:var(--border);margin-bottom:40px"></div>

  <div style="background:var(--card);border-radius:18px;box-shadow:0 2px 20px rgba(0,0,0,0.08);padding:36px 40px">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:40px;align-items:start">
      <div>
        <div style="font-size:11px;font-weight:600;color:#003D82;letter-spacing:.06em;text-transform:uppercase;margin-bottom:12px">Partner Profile — Enterprise Compute</div>
        <div style="font-size:22px;font-weight:700;letter-spacing:-.02em;color:var(--text);margin-bottom:16px;line-height:1.2">SVTech / Sun Viet</div>
        <p style="font-size:15px;color:var(--text2);line-height:1.65;margin-bottom:20px">
          Sun Viet is Vietnam's largest IT infrastructure integrator competing primarily through open bidding — {next((v['open_pct'] for v in d['channel_vendors'] if v['name']=='Sun Viet'), '—')}% of their VND {next((v['value_b'] for v in d['channel_vendors'] if v['name']=='Sun Viet'), '—')}B in contracts won via Đấu thầu rộng rãi. They are not a reseller. They are an engineering company that wins on technical specification.
        </p>
        <p style="font-size:15px;color:var(--text2);line-height:1.65;margin-bottom:20px">
          Their primary client is <strong style="color:var(--text)">Viettel</strong> — Vietnam's largest telecommunications and technology conglomerate. SVTech has won {next((r['sv_wins'] for r in d['sunviet_chart'] if r['account']=='Viettel'), 'N/A')} contracts at Viettel totaling VND {next((r['sv_b'] for r in d['sunviet_chart'] if r['account']=='Viettel'), 0)}B, supplying HPC clusters, SAN storage systems, and enterprise networking. Viettel is currently running multi-year data center expansion programs.
        </p>
        <p style="font-size:15px;color:var(--text2);line-height:1.65">
          <strong style="color:var(--text)">The Apple angle:</strong> Mac Studio Ultra and Mac Pro with M-series chips deliver inference-level AI compute at a fraction of the cost and power consumption of equivalent GPU server configurations. Sun Viet already sits inside Viettel's procurement process. Apple products on Sun Viet's next bid could displace VND 50–200B in traditional server hardware.
        </p>
      </div>
      <div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px">
          <div style="background:#F5F5F7;border-radius:12px;padding:18px 16px">
            <div style="font-size:28px;font-weight:700;color:#003D82;letter-spacing:-.03em">VND 1,949B</div>
            <div style="font-size:12px;color:var(--text2);margin-top:4px">Total contract value (all clients)</div>
          </div>
          <div style="background:#F5F5F7;border-radius:12px;padding:18px 16px">
            <div style="font-size:28px;font-weight:700;color:#003D82;letter-spacing:-.03em">96%</div>
            <div style="font-size:12px;color:var(--text2);margin-top:4px">Wins via open competitive bidding</div>
          </div>
          <div style="background:#F5F5F7;border-radius:12px;padding:18px 16px">
            <div style="font-size:28px;font-weight:700;color:#003D82;letter-spacing:-.03em">VND 1,346B</div>
            <div style="font-size:12px;color:var(--text2);margin-top:4px">Contracts won at Viettel alone</div>
          </div>
          <div style="background:#F5F5F7;border-radius:12px;padding:18px 16px">
            <div style="font-size:28px;font-weight:700;color:#003D82;letter-spacing:-.03em">25</div>
            <div style="font-size:12px;color:var(--text2);margin-top:4px">Viettel contracts, 2022–2026</div>
          </div>
        </div>
        <div style="background:#E8F4FF;border-left:3px solid #003D82;border-radius:0 10px 10px 0;padding:14px 18px;font-size:14px;color:var(--text);line-height:1.6">
          <strong>Recommended play:</strong> Introduce Mac Studio Ultra as an AI inference node in Sun Viet's next Viettel data center bid. Cost: ~VND 80M per unit. Comparable GPU server: VND 800M–2B. Power consumption: 6× lower. Sun Viet wins the bid on TCO — Apple gets the foothold.
        </div>
      </div>
    </div>
  </div>

  <div class="chart-container" style="margin-top:20px">
    <div class="chart-title">Sun Viet — Contract Presence at Key Accounts</div>
    <div class="chart-sub">Blue = Sun Viet awarded contracts. Gray = all other vendors at the same account. Viettel dominates — this is where the Mac Studio opportunity lives.</div>
    <div id="sunviet-chart" style="height:320px;"></div>
  </div>
  <div class="insight-callout">
    <strong>Two partners. Two channels. One coordinated Apple presence.</strong> FPT IS activates the device market (banking + government laptops and tablets). Sun Viet activates the enterprise compute market (Mac Studio / Mac Pro in Viettel data centers). Neither requires Apple to compete on price — both require Apple to show up with the right product at the right meeting.
  </div>

  <div style="background:var(--card);border-radius:18px;box-shadow:0 2px 20px rgba(0,0,0,0.08);padding:36px 40px;margin-top:20px">
    <div style="font-size:11px;font-weight:600;color:#F5A623;letter-spacing:.06em;text-transform:uppercase;margin-bottom:16px">Strategic Clarification — Why Not Work with Viettel Directly?</div>
    <h3 style="font-size:20px;font-weight:700;letter-spacing:-.02em;color:var(--text);margin-bottom:20px;line-height:1.3">Viettel is the largest IT buyer in Vietnam. Their business is telecom and infrastructure — not device distribution. That makes them a customer, not a channel partner.</h3>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:32px">
      <div>
        <p style="font-size:15px;color:var(--text2);line-height:1.65;margin-bottom:16px">
          Viettel spent <strong style="color:var(--text)">VND 2,791B</strong> on IT procurement — the single largest institutional buyer in this dataset. Their 133 contracts won at other agencies are for <strong style="color:var(--text)">network infrastructure, software systems, and telecom services</strong> — not devices. They are not competing with Apple's channel.
        </p>
        <p style="font-size:15px;color:var(--text2);line-height:1.65;margin-bottom:16px">
          The real barriers are structural: Viettel is a <strong style="color:var(--text)">state enterprise under the Ministry of Defense</strong>. Any new product partnership requires multiple approval layers — procurement committee, technical board, ministry sign-off. Timeline: 6–12 months minimum. Compare this to FPT IS, a private company where one meeting with the right person is sufficient.
        </p>
        <p style="font-size:15px;color:var(--text2);line-height:1.65">
          Viettel also has no Apple reseller certification, no device sales team, and no MDM expertise. Building that capability from scratch is not a quick win — it's a multi-year commitment that doesn't align with their core business. <strong style="color:var(--text)">They buy IT; they don't distribute it.</strong>
        </p>
      </div>
      <div>
        <div style="background:#FFF8E8;border-left:3px solid #F5A623;border-radius:0 12px 12px 0;padding:20px 22px;margin-bottom:20px">
          <div style="font-size:13px;font-weight:700;color:#1D1D1F;margin-bottom:8px">The right framing: Viettel as end customer</div>
          <p style="font-size:14px;color:var(--text2);line-height:1.6;margin:0">
            Viettel buys VND 2,791B of IT annually. They need enterprise compute, AI infrastructure, and secure devices. Apple's play is not to sell <em>through</em> Viettel — it is to sell <em>to</em> Viettel, via a trusted integrator who already has Viettel's procurement team on speed dial.
          </p>
        </div>
        <div style="background:#F5F5F7;border-radius:12px;padding:20px 22px">
          <div style="font-size:13px;font-weight:700;color:#1D1D1F;margin-bottom:12px">Sun Viet is that integrator</div>
          <div style="display:flex;flex-direction:column;gap:10px">
            <div style="display:flex;gap:12px;align-items:flex-start">
              <div style="min-width:8px;height:8px;border-radius:50%;background:#003D82;margin-top:6px"></div>
              <div style="font-size:14px;color:var(--text2);line-height:1.5">25 active contracts inside Viettel's procurement process</div>
            </div>
            <div style="display:flex;gap:12px;align-items:flex-start">
              <div style="min-width:8px;height:8px;border-radius:50%;background:#003D82;margin-top:6px"></div>
              <div style="font-size:14px;color:var(--text2);line-height:1.5">Win rate: 96% through open competitive bidding — no political dependency</div>
            </div>
            <div style="display:flex;gap:12px;align-items:flex-start">
              <div style="min-width:8px;height:8px;border-radius:50%;background:#003D82;margin-top:6px"></div>
              <div style="font-size:14px;color:var(--text2);line-height:1.5">Already writing technical specs for Viettel's data center RFPs</div>
            </div>
            <div style="display:flex;gap:12px;align-items:flex-start">
              <div style="min-width:8px;height:8px;border-radius:50%;background:#0071E3;margin-top:6px"></div>
              <div style="font-size:14px;color:var(--text);font-weight:600;line-height:1.5">Apple + Sun Viet = Mac Studio in Viettel's next HPC bid</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</section>

<section id="s7">
  <div class="section-label">07 · Quick Wins — This Month</div>
  <p class="section-headline">Three actions that require no budget, no new partners, and no procurement cycle. We can start this week.</p>
  <p style="font-size:16px;color:var(--text2);margin-top:-32px;margin-bottom:48px;max-width:720px;line-height:1.6">H2 2026 bids are being scoped now. Specs are being written now. The window to influence the next cycle is open — but it closes when the first RFP is published.</p>

  <div style="display:flex;flex-direction:column;gap:16px;margin-bottom:48px">

    <div style="background:var(--card);border-radius:16px;border-left:4px solid #0071E3;padding:28px 32px;display:grid;grid-template-columns:auto 1fr auto;gap:24px;align-items:start">
      <div style="font-size:32px;font-weight:800;color:#0071E3;letter-spacing:-.04em;line-height:1">01</div>
      <div>
        <div style="font-size:11px;font-weight:600;color:#0071E3;letter-spacing:.06em;text-transform:uppercase;margin-bottom:6px">This week · Zero cost</div>
        <div style="font-size:17px;font-weight:700;color:var(--text);margin-bottom:10px">Schedule a co-sell meeting with FPT Information Systems</div>
        <div style="font-size:14px;color:var(--text2);line-height:1.65">FPT IS is already Apple Authorized Reseller. They already have active contracts at BIDV, Vietcombank, and Tổng cục Thuế. They have never proposed Apple in a device bid. One meeting to align on product, pricing, and target accounts is the only thing standing between Apple and VND 529B in addressable procurement.</div>
      </div>
      <div style="background:#E8F4FF;border-radius:10px;padding:14px 18px;text-align:center;min-width:120px">
        <div style="font-size:22px;font-weight:700;color:#0071E3">529B</div>
        <div style="font-size:11px;color:#0071E3;margin-top:2px">VND unlocked</div>
      </div>
    </div>

    <div style="background:var(--card);border-radius:16px;border-left:4px solid #F5A623;padding:28px 32px;display:grid;grid-template-columns:auto 1fr auto;gap:24px;align-items:start">
      <div style="font-size:32px;font-weight:800;color:#F5A623;letter-spacing:-.04em;line-height:1">02</div>
      <div>
        <div style="font-size:11px;font-weight:600;color:#F5A623;letter-spacing:.06em;text-transform:uppercase;margin-bottom:6px">Weeks 2–4 · Before RFPs are published</div>
        <div style="font-size:17px;font-weight:700;color:var(--text);margin-bottom:10px">Get into spec consultations at Vietcombank and Tổng cục Thuế before H2 bids are written</div>
        <div style="font-size:14px;color:var(--text2);line-height:1.65">0.2% of bids require Windows — the market is open by default. But specs can embed Windows requirements without being explicit: "Office 365 integration," "Active Directory compatible." One meeting with the procurement technical team, introducing Apple Business Manager and MDM compatibility, prevents those specs from being written. FPT IS can make this introduction.</div>
      </div>
      <div style="background:#FFF8E8;border-radius:10px;padding:14px 18px;text-align:center;min-width:120px">
        <div style="font-size:22px;font-weight:700;color:#F5A623">H2 '26</div>
        <div style="font-size:11px;color:#F5A623;margin-top:2px">window open now</div>
      </div>
    </div>

    <div style="background:var(--card);border-radius:16px;border-left:4px solid #003D82;padding:28px 32px;display:grid;grid-template-columns:auto 1fr auto;gap:24px;align-items:start">
      <div style="font-size:32px;font-weight:800;color:#003D82;letter-spacing:-.04em;line-height:1">03</div>
      <div>
        <div style="font-size:11px;font-weight:600;color:#003D82;letter-spacing:.06em;text-transform:uppercase;margin-bottom:6px">Month 2 · Via FPT IS</div>
        <div style="font-size:17px;font-weight:700;color:var(--text);margin-bottom:10px">Submit a 500-unit iPad pilot proposal to Tổng cục Thuế via FPT IS</div>
        <div style="font-size:14px;color:var(--text2);line-height:1.65">FPT IS already has VND 316B in active contracts at Tổng cục Thuế — the strongest institutional foothold of any vendor in the dataset. A 500-unit iPad pilot for tax field officers (~VND 20B) can be structured as a direct appointment with FPT IS as implementing party, bypassing open bidding entirely. Success here creates the government reference case that unlocks 63 provinces.</div>
      </div>
      <div style="background:#E8F0FF;border-radius:10px;padding:14px 18px;text-align:center;min-width:120px">
        <div style="font-size:22px;font-weight:700;color:#003D82">20B</div>
        <div style="font-size:11px;color:#003D82;margin-top:2px">pilot → 150B+</div>
      </div>
    </div>

  </div>

</section>

<section id="s7b">
  <div class="section-label">08 · The Path Forward</div>
  <p class="section-headline">Four moves. Two partners. One window before this market gets locked in.</p>
  <p style="font-size:16px;color:var(--text2);margin-top:-32px;margin-bottom:48px;max-width:720px;line-height:1.6">The procurement relationships being formed right now will last 3–5 years. Every device bid won by a small reseller today is a reference case that makes Apple harder to introduce tomorrow. The two conversations that matter — activating FPT IS for devices and Sun Viet for enterprise compute — have not happened yet.</p>

  <div class="action-grid" style="grid-template-columns:repeat(2,1fr)">
    <div class="action-card">
      <div class="action-number">01</div>
      <div class="action-timeline">Month 1–2 · Device Channel</div>
      <div class="action-title">Activate FPT IS as the device channel at banking and government accounts</div>
      <div class="action-body">FPT Information Systems already holds active contracts at BIDV, Vietcombank, Vietinbank, and Tổng cục Thuế. They have never proposed Apple in a device bid. One co-sell agreement changes that — and activates VND 529B in annual device procurement immediately addressable with MacBook and iPad.</div>
      <div class="action-target">First bid target: <span>Vietcombank laptop replacement cycle (VND 110B budget, annual frequency).</span></div>
    </div>
    <div class="action-card">
      <div class="action-number">02</div>
      <div class="action-timeline">Month 2–4 · Spec Engineering</div>
      <div class="action-title">Commission a TCO study — and put it on the desk of every procurement committee</div>
      <div class="action-body">0.2% of bids explicitly require Windows. The market is open by spec — but closed by assumption. A co-branded study with IDC Vietnam showing MacBook 5-year total cost of ownership parity with Windows laptops changes the conversation before bids are written. Distribute to Bộ Tài chính, Ngân hàng Nhà nước, and Tổng cục Thuế procurement leadership.</div>
      <div class="action-target">Goal: <span>Embedded in 3+ agency technical assessment frameworks before year-end 2026.</span></div>
    </div>
    <div class="action-card">
      <div class="action-number">03</div>
      <div class="action-timeline">Month 3–6 · Reference Case</div>
      <div class="action-title">Run a 500-unit iPad pilot at Tổng cục Thuế — build the government reference case</div>
      <div class="action-body">300,000 tax officers currently use paper or low-end Android devices in the field. VND 171B in Tổng cục Thuế device budgets flows annually. iPad + Apple Configurator + centralized device management is the strongest field-worker pitch in this market. A 500-unit pilot at one Cục Thuế becomes the proof point that unlocks 63 provinces.</div>
      <div class="action-target">Pilot: <span>VND 15–25B → VND 150+B national rollout within 18 months.</span></div>
    </div>
    <div class="action-card">
      <div class="action-number">04</div>
      <div class="action-timeline">Month 4–8 · Enterprise Compute</div>
      <div class="action-title">Introduce Mac Studio Ultra to Sun Viet for Viettel's next data center bid</div>
      <div class="action-body">Sun Viet has won VND 1,346B at Viettel through 25 open competitive contracts. They supply HPC clusters, SAN storage, and enterprise compute. Viettel is expanding its AI and cloud infrastructure. Mac Studio Ultra — at VND 80M per unit vs. VND 800M–2B for equivalent GPU servers — is a credible total cost of ownership argument that Sun Viet can present in their next technical proposal.</div>
      <div class="action-target">Entry point: <span>Mac Studio Ultra as AI inference nodes in Viettel's next data center refresh cycle.</span></div>
    </div>
  </div>

  <div class="closing-callout">
    Two partners. Four moves. One market that is structurally open and growing 5× over three years. The conversations that need to happen — with FPT IS on device bids and with Sun Viet on enterprise compute — have not happened yet. Both can start this month.
  </div>
</section>

<section id="s8fptis">
  <div class="section-label">FPT IS · Deal History</div>
  <h2 class="section-headline">FPT IS top {len(d['fptis_deals'])} awarded contracts — all tech categories</h2>
  <p style="font-size:15px;color:var(--text2);margin-top:-32px;margin-bottom:32px;max-width:720px;line-height:1.6">These are FPT IS's largest awarded contracts across all IT categories — including services and software, not just devices. Note: FPT IS does not currently appear in the top device vendors chart because their wins are in IT services and systems integration, not device procurement. That gap is the opportunity.</p>

  <div class="chart-container">
    <div class="chart-title">FPT IS — Top {len(d['fptis_deals'])} Contracts by Awarded Value</div>
    <div class="chart-sub">Click bid name to view on muasamcong.mpi.gov.vn · All IT categories (services, software, systems, devices)</div>
    <div style="overflow-x:auto">
    <table class="data-table">
      <thead><tr>
        <th>Buyer</th><th>Bid</th><th style="text-align:right">Awarded</th><th>Form</th><th>Year</th>
      </tr></thead>
      <tbody>
        {fptis_rows_html}
      </tbody>
    </table>
    </div>
  </div>
</section>

<section id="s8">
  <div class="section-label">09 · Evidence Base</div>
  <h2 class="section-headline">"The data behind every claim — drill down into the numbers"</h2>

  <!-- 8A: Who wins at target accounts -->
  <div class="chart-container">
    <div class="chart-title">Who Is Winning Device Bids at Priority Accounts</div>
    <div class="chart-sub">BIDV · Vietcombank · Vietinbank · Tổng cục Thuế · Kho bạc — top 30 awarded contracts, sorted by value</div>
    <div style="overflow-x:auto">
    <table class="data-table">
      <thead><tr>
        <th>Buyer</th><th>Bid</th><th>Winner</th>
        <th>Budget</th><th>Awarded</th><th>Discount</th><th>Type</th><th>Year</th>
      </tr></thead>
      <tbody>
        {target_rows_html}
      </tbody>
    </table>
    </div>
  </div>

  <!-- 8B: Vendor risk map — discount vs. contract value scatter -->
  <div class="chart-container" style="margin-top:20px">
    <div class="chart-title">Vendor Risk Map — Price Aggressiveness vs. Contract Value</div>
    <div class="chart-sub">X = overall discount %. Y = total awarded value. Vendors in the red zone are high-discount, high-value — unsustainable and likely to create buyer dissatisfaction. These are Apple's best opportunities.</div>
    <div id="vendor-risk-chart" style="height:420px"></div>
  </div>

  <div class="insight-callout">
    Vendors discounting &gt;15% (red zone) are pricing themselves into sustainability problems. When service quality deteriorates and the buyer's next refresh cycle comes, they will be looking for alternatives. <strong>These are Apple's warmest leads.</strong>
  </div>

  <!-- 8C: Repeat wins -->
  <div class="chart-container" style="margin-top:20px">
    <div class="chart-title">Entrenched Relationships — Where Apple Needs a Partner, Not a Frontal Attack</div>
    <div class="chart-sub">Vendor–buyer pairs with 2+ wins represent locked relationships. Apple's strategy: find which vendor is winning at Apple's target accounts — and make that vendor an Apple reseller.</div>
    <div style="overflow-x:auto">
    <table class="data-table">
      <thead><tr>
        <th>Vendor</th><th>Buyer</th><th style="text-align:center">Wins</th><th style="text-align:right">Total Awarded</th>
      </tr></thead>
      <tbody>
        {repeat_rows_html}
      </tbody>
    </table>
    </div>
  </div>
</section>

<section id="s9" style="background:var(--card); border-radius:18px; margin-top:80px; padding:64px 48px;">
  <div class="section-label">10 · Appendix / Glossary</div>
  <p class="section-headline" style="font-size:22px; margin-bottom:32px;">Terms used in this report</p>

  <div class="glossary-grid">
    <div class="glossary-card">
      <div class="glossary-term">Competitive Shopping (CHCT — Chào hàng cạnh tranh)</div>
      <div class="glossary-def">A simplified competitive procurement method — typically 3 vendors submit price quotes, used for smaller-value contracts. Still competitive, not a direct appointment. Accounts for {d['chct_pct']}% of bids in this dataset.
        <br><span style="font-size:12px;color:#86868B;margin-top:4px;display:block">Hình thức cạnh tranh đơn giản hóa — thường 3 nhà thầu nộp báo giá. Vẫn là cạnh tranh, không phải chỉ định thầu.</span>
      </div>
    </div>
    <div class="glossary-card">
      <div class="glossary-term">Open Bidding (DTRR — Đấu thầu rộng rãi)</div>
      <div class="glossary-def">Competitive procurement open to any qualified vendor. This is the channel where Apple can participate directly through an authorized reseller.
        <br><span style="font-size:12px;color:#86868B;margin-top:4px;display:block">Đấu thầu cạnh tranh mở — kênh Apple có thể tham gia.</span>
      </div>
    </div>
    <div class="glossary-card">
      <div class="glossary-term">Authorized Reseller / VAR</div>
      <div class="glossary-def">A company officially certified by Apple to sell products and provide technical support. A prerequisite for participating in government procurement with Apple devices.
        <br><span style="font-size:12px;color:#86868B;margin-top:4px;display:block">Nhà phân phối được Apple cấp phép — điều kiện bắt buộc để dự thầu.</span>
      </div>
    </div>
    <div class="glossary-card">
      <div class="glossary-term">Total Cost of Ownership (TCO)</div>
      <div class="glossary-def">The full 5-year cost of a device: purchase price, maintenance, security software, repairs, training, and replacement. MacBook's TCO is competitive with Windows laptops despite higher upfront cost.
        <br><span style="font-size:12px;color:#86868B;margin-top:4px;display:block">Tổng chi phí thực tế 5 năm — MacBook cạnh tranh khi tính dài hạn.</span>
      </div>
    </div>
    <div class="glossary-card">
      <div class="glossary-term">Mobile Device Management (MDM)</div>
      <div class="glossary-def">Software that lets IT departments remotely manage, configure, and secure all devices from a central console. Apple provides Apple Business Manager and supports third-party MDM platforms.
        <br><span style="font-size:12px;color:#86868B;margin-top:4px;display:block">Hệ thống quản lý thiết bị tập trung từ xa.</span>
      </div>
    </div>
    <div class="glossary-card">
      <div class="glossary-term">Minimum Advertised Price (MAP)</div>
      <div class="glossary-def">Apple's policy setting the lowest price at which resellers may list products. Protects brand positioning and prevents race-to-the-bottom price competition between resellers.
        <br><span style="font-size:12px;color:#86868B;margin-top:4px;display:block">Giá sàn tối thiểu — lý do Apple không thể cạnh tranh bằng phá giá.</span>
      </div>
    </div>
    <div class="glossary-card">
      <div class="glossary-term">FPT Information Systems (FPT IS)</div>
      <div class="glossary-def">System integrator and technology distributor under the FPT Group. Already an Apple Authorized Reseller and an established vendor at Tổng cục Thuế, Vietcombank, and BIDV.
        <br><span style="font-size:12px;color:#86868B;margin-top:4px;display:block">Công ty TNHH Hệ thống Thông tin FPT — đối tác kênh Apple đề xuất.</span>
      </div>
    </div>
    <div class="glossary-card">
      <div class="glossary-term">ƯKTS (Ứng dụng Kỹ thuật và Sản xuất)</div>
      <div class="glossary-def">100% state-owned enterprise specializing in defense and government IT. Won VND 886B across 45 contracts via open bidding (77% competitive). Key clients: General Dept. of Taxation, Ministry of Defense cryptography unit.
        <br><span style="font-size:12px;color:#86868B;margin-top:4px;display:block">Công ty nhà nước chuyên CNTT quốc phòng — VND 886B, 77% đấu thầu mở.</span>
      </div>
    </div>
    <div class="glossary-card">
      <div class="glossary-term">Addressable Market</div>
      <div class="glossary-def">The portion of the total IT procurement market Apple can realistically compete in today — bids for devices (laptops, tablets, desktops) with no explicit Windows or Microsoft software requirement.
        <br><span style="font-size:12px;color:#86868B;margin-top:4px;display:block">Thị trường Apple có thể tham gia ngay — gói thầu không yêu cầu Windows.</span>
      </div>
    </div>
    <div class="glossary-card">
      <div class="glossary-term">HPC (High-Performance Computing)</div>
      <div class="glossary-def">Computing infrastructure designed for large-scale, parallel workloads — AI training, scientific simulation, data analytics. Sun Viet supplies HPC clusters to Viettel. Mac Studio Ultra's M-series chip competes in this category as an AI inference node.
        <br><span style="font-size:12px;color:#86868B;margin-top:4px;display:block">Hạ tầng tính toán hiệu suất cao — Sun Viet cung cấp cho Viettel; Mac Studio Ultra là lựa chọn thay thế tiết kiệm điện.</span>
      </div>
    </div>
    <div class="glossary-card">
      <div class="glossary-term">SAN Storage (Storage Area Network)</div>
      <div class="glossary-def">Enterprise-grade dedicated storage network, separate from general-purpose servers. One of Sun Viet's core product categories at Viettel. Not a direct Apple play, but part of the infrastructure context Sun Viet operates in.
        <br><span style="font-size:12px;color:#86868B;margin-top:4px;display:block">Hệ thống lưu trữ mạng — danh mục sản phẩm cốt lõi của Sun Viet tại Viettel.</span>
      </div>
    </div>
    <div class="glossary-card">
      <div class="glossary-term">RFP (Request for Proposal)</div>
      <div class="glossary-def">A formal procurement document issued by a government agency inviting vendors to submit technical and financial proposals. Spec-writing before an RFP is published is the key leverage point — it's where OS requirements and device specifications are set.
        <br><span style="font-size:12px;color:#86868B;margin-top:4px;display:block">Hồ sơ mời thầu — viết spec trước khi RFP phát hành là cơ hội ảnh hưởng lớn nhất.</span>
      </div>
    </div>
    <div class="glossary-card">
      <div class="glossary-term">Apple Business Manager (ABM)</div>
      <div class="glossary-def">Apple's free platform for deploying and managing Apple devices at scale in organizations. Supports zero-touch enrollment — devices ship directly to employees pre-configured. Key proof point in enterprise pitches.
        <br><span style="font-size:12px;color:#86868B;margin-top:4px;display:block">Nền tảng quản lý triển khai thiết bị Apple cho tổ chức — miễn phí, hỗ trợ cấu hình từ xa.</span>
      </div>
    </div>
    <div class="glossary-card">
      <div class="glossary-term">Budget Estimate (Dự toán — PI)</div>
      <div class="glossary-def">The government agency's published budget ceiling for a procurement bid, set before vendor proposals are received. Used in this report as the market size indicator. Awarded price (WP — Winner Price) is typically 5–20% lower after negotiation.
        <br><span style="font-size:12px;color:#86868B;margin-top:4px;display:block">Giá trị dự toán được phê duyệt trước khi đấu thầu — chỉ số quy mô thị trường trong báo cáo này.</span>
      </div>
    </div>
    <div class="glossary-card">
      <div class="glossary-term">Awarded Price (WP — Winner Price)</div>
      <div class="glossary-def">The final contract value agreed between the winning vendor and the buyer. Always ≤ budget estimate. The gap between PI and WP is the discount — used in this report to measure vendor price aggressiveness.
        <br><span style="font-size:12px;color:#86868B;margin-top:4px;display:block">Giá trúng thầu thực tế — chênh lệch với dự toán phản ánh mức độ phá giá của nhà thầu.</span>
      </div>
    </div>
    <div class="glossary-card">
      <div class="glossary-term">SVTech / Sun Viet</div>
      <div class="glossary-def">Trading name used in procurement portal: SVTech (or SV Tech). Full entity: Công ty Cổ phần Phát triển Công nghệ Viễn thông Tin học Sun Việt (PTCVT). Vietnam's largest infrastructure integrator by open-market contract value. Primary client: Viettel.
        <br><span style="font-size:12px;color:#86868B;margin-top:4px;display:block">Tên trong portal đấu thầu: SVTech. Nhà tích hợp hạ tầng lớn nhất theo giá trị đấu thầu mở. Khách hàng chính: Viettel.</span>
      </div>
    </div>
    <div class="glossary-card">
      <div class="glossary-term">Co-sell</div>
      <div class="glossary-def">A go-to-market arrangement where Apple works alongside a partner on a specific deal — the partner owns the customer relationship and the contract, Apple provides product access, pricing, and technical support. The recommended model for FPT IS and Sun Viet engagements.
        <br><span style="font-size:12px;color:#86868B;margin-top:4px;display:block">Mô hình bán hàng phối hợp — đối tác sở hữu hợp đồng, Apple hỗ trợ sản phẩm và kỹ thuật.</span>
      </div>
    </div>
  </div>
</section>

</main>

<footer>
  <div class="footer-grid">
    <div class="footer-left">
      <svg viewBox="0 0 814 1000" xmlns="http://www.w3.org/2000/svg">
        <path d="M788.1 340.9c-5.8 4.5-108.2 62.2-108.2 190.5 0 148.4 130.3 200.9 134.2 202.2-.6 3.2-20.7 71.9-68.7 141.9-42.8 61.6-87.5 123.1-155.5 123.1s-85.5-39.5-164-39.5c-76 0-103.7 40.8-165.9 40.8s-105-57.8-155.5-127.4C46 790.7 0 663 0 541.8c0-207.1 134.8-316.5 267.4-316.5 70.1 0 128.4 46.4 172.5 46.4 42.4 0 109.2-49.8 186.4-49.8 30.5 0 110.5 2.6 170.3 85.1zm-252.3-166.1c31.1-36.9 53.1-88.1 53.1-139.3 0-7.1-.6-14.3-1.9-20.1-50.6 1.9-110.8 33.7-147.1 75.8-28.5 32.4-55.1 83.6-55.1 135.5 0 7.8 1.3 15.6 1.9 18.1 3.2.6 8.4 1.3 13.6 1.3 45.4 0 102.5-30.4 135.5-71.3z"/>
      </svg>
      <div class="footer-meta">
        Data source: Vietnam Government E-Procurement Portal (muasamcong.mpi.gov.vn) · {d['total_bids']:,} bids · 2022–2026<br>
        Analysis period: 2022 Q1 – {d['generated_at']} · Generated: {d['generated_at']}<br>
        <span style="color:#ADADB8">Methodology: category-filtered dataset (Hàng hóa + Hỗn hợp, muasamcong.mpi.gov.vn). Collection: 2022–present (keyword-based pre-Jun 2026, category-based from Jun 2026). Coverage: est. 60–75% of addressable market. Absolute VND values = floor estimates. Discount = (dự toán − giá trúng thầu) / dự toán. Rankings and trends are directionally reliable.</span>
      </div>
    </div>
    <div class="footer-right">
      Copyright &copy; 2026 Apple Inc.<br>
      All rights reserved.<br>
      <span style="color: var(--border);">Internal — Do Not Distribute</span>
    </div>
  </div>
</footer>

<script>
const BLUE = '#0071E3';
const GRAY = '#86868B';
const LIGHT_GRAY = '#C7C7CC';
const BG = 'white';

const plotlyConfig = {{
  displayModeBar: false,
  responsive: true
}};

const layoutBase = {{
  paper_bgcolor: BG,
  plot_bgcolor: BG,
  font: {{ family: '-apple-system, "SF Pro Display", "SF Pro Text", system-ui, sans-serif', color: '#1D1D1F' }},
  margin: {{ t: 20, b: 40, l: 40, r: 20 }},
  xaxis: {{ gridcolor: '#F0F0F0', linecolor: '#D2D2D7', tickfont: {{ size: 12, color: '#6E6E73' }} }},
  yaxis: {{ gridcolor: '#F0F0F0', linecolor: '#D2D2D7', tickfont: {{ size: 12, color: '#6E6E73' }} }}
}};

// SHARE DONUT — Apple share vs rest
(function() {{
  const appleVal = {d['apple_current_b']};
  const totalVal = {int(d['apple_possible_b'])};
  const restVal  = Math.max(totalVal - appleVal, 0);
  Plotly.newPlot('share-donut', [{{
    type: 'pie',
    values: [appleVal, restVal],
    labels: ['Apple (VND ' + appleVal + 'B)', 'All Other Vendors'],
    hole: 0.65,
    marker: {{ colors: ['#0071E3', '#F0F0F5'] }},
    textinfo: 'none',
    hovertemplate: '%{{label}}<br>VND %{{value}}B<br>%{{percent}}<extra></extra>'
  }}], Object.assign({{}}, layoutBase, {{
    margin: {{ t:10, b:10, l:10, r:10 }},
    showlegend: true,
    legend: {{ orientation:'h', y:-0.15, x:0.5, xanchor:'center', font:{{size:11}} }},
    annotations: [{{
      text: '{d["apple_share_pct"]}%',
      x: 0.5, y: 0.5, xref:'paper', yref:'paper',
      font: {{ size:28, color:BLUE, family: '-apple-system, system-ui' }},
      showarrow: false
    }}]
  }}), plotlyConfig);
}})();

// FUNNEL CHART
(function() {{
  const labels = [
    'All tech procurement (keyword-matched)',
    'Device bids (laptop / tablet / desktop)',
    'Apple-addressable (no OS requirement)',
    'Apple explicitly named in bid'
  ];
  const values = [{int(d['total_tech_v_b'])}, {int(d['total_value_b'])}, {int(d['apple_possible_b'])}, {int(d['apple_explicit_b'])}];
  const colors = [LIGHT_GRAY, GRAY, BLUE, '#34AADC'];

  const data = [{{
    type: 'bar',
    orientation: 'h',
    y: labels,
    x: values,
    marker: {{ color: colors }},
    text: values.map(v => 'VND ' + v.toLocaleString() + 'B'),
    textposition: 'inside',
    insidetextanchor: 'start',
    textfont: {{ color: 'white', size: 13 }},
    hovertemplate: '<b>%{{y}}</b><br>VND %{{x:,}}B<extra></extra>'
  }}];

  const layout = Object.assign({{}}, layoutBase, {{
    margin: {{ t: 10, b: 40, l: 180, r: 40 }},
    xaxis: Object.assign({{}}, layoutBase.xaxis, {{
      tickformat: ',',
      ticksuffix: 'B',
      showgrid: true,
      zeroline: false
    }}),
    yaxis: Object.assign({{}}, layoutBase.yaxis, {{
      autorange: 'reversed',
      showgrid: false,
      tickfont: {{ size: 13, color: '#1D1D1F' }}
    }}),
    bargap: 0.35
  }});

  Plotly.newPlot('funnel-chart', data, layout, plotlyConfig);
}})();

// DEVICE CHART
(function() {{
  const devLabels = {json.dumps(dev_labels)};
  const devVals   = {json.dumps(dev_values)};
  const nCats = devLabels.length;

  const data = [{{
    type: 'bar',
    orientation: 'h',
    y: devLabels,
    x: devVals,
    marker: {{
      color: devLabels.map(l => {{
        if (l === 'Other devices') return '#ADADB8';
        if (l === 'Laptop')        return '#003D82';
        if (l === 'Desktop / iMac') return '#0071E3';
        if (l === 'Tablet / iPad') return '#5AC8FA';
        return '#ADADB8';
      }})
    }},
    text: devVals.map(v => 'VND ' + v + 'B'),
    textposition: 'inside',
    insidetextanchor: 'start',
    textfont: {{ color: 'white', size: 12 }},
    hovertemplate: '<b>%{{y}}</b><br>VND %{{x}}B<extra></extra>'
  }}];

  const layout = Object.assign({{}}, layoutBase, {{
    margin: {{ t: 10, b: 40, l: 130, r: 20 }},
    xaxis: Object.assign({{}}, layoutBase.xaxis, {{
      ticksuffix: 'B',
      showgrid: true,
      zeroline: false
    }}),
    yaxis: Object.assign({{}}, layoutBase.yaxis, {{
      showgrid: false,
      autorange: 'reversed',
      tickfont: {{ size: 12, color: '#1D1D1F' }}
    }}),
    bargap: 0.35
  }});

  Plotly.newPlot('device-chart', data, layout, plotlyConfig);
}})();

// TREND CHART
(function() {{
  const yrL = {json.dumps(yr_labels)};
  const yrV = {json.dumps(yr_values)};
  const lastIdx = yrL.length - 1;

  const data = [{{
    type: 'scatter',
    mode: 'lines+markers',
    x: yrL,
    y: yrV,
    line: {{ color: BLUE, width: 2.5, shape: 'spline', smoothing: 0.4 }},
    marker: {{ color: BLUE, size: 8, line: {{ color: 'white', width: 2 }} }},
    fill: 'tozeroy',
    fillcolor: 'rgba(0,113,227,0.08)',
    hovertemplate: '<b>%{{x}}</b><br>VND %{{y:,}}B<extra></extra>'
  }}];

  const annotations = [];
  if (yrL.length > 0) {{
    annotations.push({{
      x: yrL[lastIdx],
      y: yrV[lastIdx],
      xref: 'x',
      yref: 'y',
      text: '{trend_annotation_text}',
      showarrow: true,
      arrowhead: 2,
      arrowcolor: BLUE,
      arrowsize: 0.8,
      ax: -60,
      ay: -36,
      font: {{ size: 11, color: BLUE }},
      bgcolor: '#F0F7FF',
      bordercolor: BLUE,
      borderwidth: 1,
      borderpad: 5
    }});
  }}

  const layout = Object.assign({{}}, layoutBase, {{
    margin: {{ t: 20, b: 60, l: 60, r: 20 }},
    xaxis: Object.assign({{}}, layoutBase.xaxis, {{
      showgrid: false,
      zeroline: false,
      tickangle: -45,
      tickfont: {{ size: 11, color: '#6E6E73' }}
    }}),
    yaxis: Object.assign({{}}, layoutBase.yaxis, {{
      ticksuffix: 'B',
      showgrid: true,
      zeroline: false
    }}),
    annotations: annotations
  }});

  Plotly.newPlot('trend-chart-2', data, layout, plotlyConfig);
}})();

// CHANNEL SCATTER
// x = open-market competitiveness, y = number of wins, bubble size = contract value
(function() {{
  // Key partners — dynamic from data
  const cvRaw = {json.dumps(d['channel_vendors'])};
  const colorMap = {{ 'Recommended': BLUE, 'Enterprise compute': '#003D82',
                      'Co-sell': GRAY, 'Potential': BLUE }};
  const shortMap = {{ 'Sun Viet': 'Sun Viet ★' }};
  const vendors = cvRaw.map(v => ({{
    name:  v.name,
    short: shortMap[v.name] || v.name,
    x:     v.open_pct,
    wins:  v.wins,
    value: v.value_b,
    color: colorMap[v.role] || LIGHT_GRAY,
    label: v.role
  }}));

  const traces = [];
  const annotations = [];

  const offsets = {{
    'FPT IS':       {{ ax: 70,  ay: -30 }},
    'Sun Viet':     {{ ax: 70,  ay: -25 }},
    'Viettel':      {{ ax: -75, ay: -25 }},
    'VNPT':         {{ ax: -65, ay: 30  }},
    'ƯKTS':         {{ ax: 65,  ay: 30  }},
    'Tek-Solution': {{ ax: 70,  ay: 25  }},
    'NLT':          {{ ax: -55, ay: -25 }},
    'Trọng Tín':    {{ ax: 65,  ay: -28 }}
  }};

  vendors.forEach(v => {{
    const off = offsets[v.short] || {{ ax: 50, ay: -25 }};
    annotations.push({{
      x: v.x, y: v.wins,
      xref: 'x', yref: 'y',
      text: '<b>' + v.short + '</b>' + (v.label ? '<br><span style="font-size:10px;color:#86868B">' + v.label + '</span>' : ''),
      showarrow: true,
      arrowhead: 0, arrowwidth: 1,
      arrowcolor: v.color === BLUE ? BLUE : '#D2D2D7',
      ax: off.ax, ay: off.ay,
      font: {{ size: 11, color: v.color === BLUE ? BLUE : '#1D1D1F' }},
      bgcolor: 'rgba(255,255,255,0.9)',
      borderpad: 4, borderwidth: 0,
      align: 'center'
    }});

    traces.push({{
      type: 'scatter', mode: 'markers',
      x: [v.x], y: [v.wins],
      marker: {{
        color: v.color,
        size: Math.sqrt(v.value) * 1.8 + 12,
        opacity: 0.82,
        line: {{ color: 'white', width: 2 }}
      }},
      hovertemplate: '<b>' + v.name + '</b><br>' +
        'Open bidding (DTRR): ' + v.x + '%<br>' +
        'Wins: ' + v.wins + ' contracts<br>' +
        'Value: VND ' + v.value + 'B' +
        '<extra></extra>',
      showlegend: false
    }});
  }});

  const layout = Object.assign({{}}, layoutBase, {{
    margin: {{ t: 20, b: 70, l: 70, r: 40 }},
    xaxis: Object.assign({{}}, layoutBase.xaxis, {{
      title: {{ text: '% wins via Đấu thầu rộng rãi (DTRR — open competitive bidding)', font: {{ size: 12, color: GRAY }} }},
      range: [20, 110], showgrid: true, zeroline: false,
      ticksuffix: '%'
    }}),
    yaxis: Object.assign({{}}, layoutBase.yaxis, {{
      title: {{ text: 'Number of Contract Wins', font: {{ size: 12, color: GRAY }} }},
      range: [0, 155], showgrid: true, zeroline: false
    }}),
    annotations: [
      ...annotations,
      {{
        x: 95, y: 145,
        xref: 'x', yref: 'y',
        text: '★ Recommended<br>Partner Zone',
        showarrow: false,
        font: {{ color: BLUE, size: 11 }},
        bgcolor: '#F0F7FF',
        bordercolor: BLUE, borderwidth: 1, borderpad: 6
      }}
    ],
    shapes: [{{
      type: 'rect', x0: 80, x1: 110, y0: 15, y1: 155,
      fillcolor: 'rgba(0,113,227,0.04)',
      line: {{ color: BLUE, width: 1, dash: 'dot' }}
    }}]
  }});

  Plotly.newPlot('channel-chart', traces, layout, plotlyConfig);
}})();

// VENDOR BY DEVICE CATEGORY STACKED BAR
(function() {{
  const vendors  = {json.dumps(d['vendor_chart']['vendors'])};
  const cats     = {json.dumps(d['vendor_chart']['cats'])};
  const catColors = {{
    'Desktop / iMac': '#003D82',
    'Laptop':         '#0071E3',
    'Tablet / iPad':  '#5AC8FA'
  }};

  const traces = Object.entries(cats).map(([cat, vals]) => ({{
    type: 'bar',
    name: cat,
    x: vendors,
    y: vals,
    marker: {{ color: catColors[cat] }},
    hovertemplate: '<b>%{{x}}</b><br>' + cat + ': VND %{{y}}B<extra></extra>'
  }}));

  const layout = Object.assign({{}}, layoutBase, {{
    barmode: 'stack',
    hovermode: 'x unified',
    margin: {{ t: 60, b: 120, l: 60, r: 20 }},
    xaxis: Object.assign({{}}, layoutBase.xaxis, {{
      tickangle: -35,
      tickfont: {{ size: 11, color: '#1D1D1F' }},
      showgrid: false,
      zeroline: false,
      automargin: true
    }}),
    yaxis: Object.assign({{}}, layoutBase.yaxis, {{
      ticksuffix: 'B',
      showgrid: true,
      zeroline: false,
      title: {{ text: 'Awarded Contract Value (VND B)', font: {{ size: 12, color: GRAY }} }}
    }}),
    legend: {{ orientation: 'h', y: 1.08, x: 0, xanchor: 'left', font: {{ size: 11 }} }},
    showlegend: true
  }});

  Plotly.newPlot('vendor-cat-chart', traces, layout, plotlyConfig);
}})();

// MARGIN CHART
(function() {{
  const rows    = {json.dumps(d['margin_table'])};
  const vendors = rows.map(r => r.vendor);
  const discs   = rows.map(r => r.overall_disc);
  const vals    = rows.map(r => r.value_b);

  Plotly.newPlot('margin-chart', [{{
    type: 'bar', orientation: 'h',
    y: vendors, x: discs,
    customdata: vals,
    marker: {{ color: discs.map(v => v > 15 ? '#FF3B30' : v > 8 ? '#F5A623' : '#0071E3') }},
    text: discs.map((v, i) => v + '%  · VND ' + vals[i] + 'B'),
    textposition: 'outside', textfont: {{ size: 11 }},
    hovertemplate: '<b>%{{y}}</b><br>Discount: %{{x}}%<br>Awarded: VND %{{customdata}}B<extra></extra>'
  }}], Object.assign({{}}, layoutBase, {{
    margin: {{ t:10, b:40, l:230, r:130 }},
    xaxis: Object.assign({{}}, layoutBase.xaxis, {{
      title: {{ text: 'Overall Price Discount vs. Budget (%)', font: {{ size:12, color:GRAY }} }},
      showgrid: true, zeroline: false, ticksuffix: '%', range: [0, 35]
    }}),
    yaxis: Object.assign({{}}, layoutBase.yaxis, {{
      autorange: 'reversed', showgrid: false, automargin: true,
      tickfont: {{ size: 11, color: '#1D1D1F' }}
    }})
  }}), plotlyConfig);
}})();

// GAP CHART — FPT IS presence vs device gap at key accounts
(function() {{
  const data   = {json.dumps(d['gap_chart'])};
  const labels = data.map(r => r.account);
  const fptis  = data.map(r => r.fptis_b);
  const device = data.map(r => r.device_b);

  // Annotation: flag Tổng cục Thuế as the strongest entry point
  const taxIdx = labels.indexOf('Tổng cục Thuế');
  const annotations = taxIdx >= 0 ? [{{
    x: 'Tổng cục Thuế', y: fptis[taxIdx] + 20,
    xref:'x', yref:'y',
    text: '★ FPT IS dominant here<br>Best entry point',
    showarrow: true, arrowhead: 2, arrowcolor: BLUE,
    ax: 0, ay: -45,
    font: {{ size:11, color:BLUE }},
    bgcolor:'#F0F7FF', bordercolor:BLUE, borderwidth:1, borderpad:5
  }}] : [];

  Plotly.newPlot('gap-chart', [
    {{
      type: 'bar', name: 'FPT IS — total contracts at this account',
      x: labels, y: fptis,
      marker: {{ color: '#0071E3', opacity: 0.9 }},
      hovertemplate: '<b>%{{x}}</b><br>FPT IS presence: VND %{{y}}B<extra></extra>'
    }},
    {{
      type: 'bar', name: 'Device bids — won by other vendors',
      x: labels, y: device,
      marker: {{ color: '#D2D2D7' }},
      hovertemplate: '<b>%{{x}}</b><br>Device (others): VND %{{y}}B<br>← Apple opportunity<extra></extra>'
    }}
  ], Object.assign({{}}, layoutBase, {{
    barmode: 'group',
    annotations: annotations,
    margin: {{ t:50, b:60, l:60, r:20 }},
    xaxis: Object.assign({{}}, layoutBase.xaxis, {{ showgrid:false, zeroline:false }}),
    yaxis: Object.assign({{}}, layoutBase.yaxis, {{
      title: {{ text:'VND B', font:{{size:12, color:GRAY}} }},
      showgrid:true, zeroline:false, ticksuffix:'B'
    }}),
    legend: {{ orientation:'h', y:1.12, x:0, font:{{size:11}} }},
    showlegend: true
  }}), plotlyConfig);
}})();

// ACCOUNT CAPTURE CHART — who owns each priority account's device bids
(function() {{
  const capture = {json.dumps(d['acct_capture'])};
  const accounts = Object.keys(capture);
  const COLORS = ['#003D82','#0071E3','#5AC8FA','#ADADB8'];
  const maxVendors = 4;

  // Build one trace per vendor rank (rank 0 = top vendor per account)
  const traces = [];
  for (let rank = 0; rank < maxVendors; rank++) {{
    const x = accounts;
    const y = accounts.map(a => (capture[a][rank] || {{}}).value_b || 0);
    const text = accounts.map(a => (capture[a][rank] || {{}}).vendor || '');
    const customdata = text;
    traces.push({{
      type: 'bar', name: 'Rank ' + (rank+1),
      x: x, y: y,
      text: text,
      textposition: 'inside',
      textfont: {{ size: 10, color: 'white' }},
      marker: {{ color: COLORS[rank] }},
      hovertemplate: '<b>%{{x}}</b><br>%{{customdata}}<br>VND %{{y}}B<extra></extra>',
      customdata: customdata,
      showlegend: false
    }});
  }}

  Plotly.newPlot('acct-capture-chart', traces, Object.assign({{}}, layoutBase, {{
    barmode: 'stack',
    margin: {{ t:10, b:50, l:60, r:20 }},
    xaxis: Object.assign({{}}, layoutBase.xaxis, {{ showgrid:false, zeroline:false }}),
    yaxis: Object.assign({{}}, layoutBase.yaxis, {{
      title: {{ text:'Awarded (VND B)', font:{{size:12,color:GRAY}} }},
      showgrid:true, zeroline:false, ticksuffix:'B'
    }})
  }}), plotlyConfig);
}})();

// H1 vs H2 PATTERN + FORECAST
(function() {{
  const yrData = {json.dumps(d['year_trend'])};

  // Separate H1 and H2 per year
  const years = ['2022','2023','2024','2025','2026'];
  const h1vals = {{}}, h2vals = {{}};
  Object.entries(yrData).forEach(([k,v]) => {{
    const yr = k.split(' ')[0], half = k.split(' ')[1];
    if (half === 'H1') h1vals[yr] = v.value_b;
    else               h2vals[yr] = v.value_b;
  }});

  // Compute H2/H1 multipliers for years with both values
  const mults = [];
  ['2023','2024','2025'].forEach(yr => {{
    if (h1vals[yr] && h2vals[yr] && h1vals[yr] > 0)
      mults.push(h2vals[yr] / h1vals[yr]);
  }});
  const avgMult = mults.length ? mults.reduce((a,b)=>a+b,0)/mults.length : 1.7;

  // Forecast H2 2026 from H2 YoY growth (more reliable than H1×mult due to sparse 2026 H1 data)
  const h2growth = h2vals['2025'] && h2vals['2024'] ? h2vals['2025']/h2vals['2024'] : 1.16;
  const forecast26H2 = Math.round((h2vals['2025'] || 0) * h2growth);

  const xLabels = ['2022 H2','2023 H1','2023 H2','2024 H1','2024 H2','2025 H1','2025 H2','2026 H1','2026 H2 (forecast)'];
  const yActual = [
    h2vals['2022']||0, h1vals['2023']||0, h2vals['2023']||0,
    h1vals['2024']||0, h2vals['2024']||0,
    h1vals['2025']||0, h2vals['2025']||0,
    h1vals['2026']||0
  ];
  const isH1 = [false,true,false,true,false,true,false,true];

  const annotations = [];

  // H1 < H2 pattern callout on 2025
  if (h1vals['2025'] && h2vals['2025']) {{
    annotations.push({{
      x: '2025 H1', y: h1vals['2025'],
      xref:'x', yref:'y',
      text: 'H1 is always<br>smaller than H2',
      showarrow: true, arrowhead:2, arrowcolor:GRAY,
      ax:0, ay:-50, font:{{size:10,color:GRAY}},
      bgcolor:'white', borderpad:3
    }});
  }}

  // Forecast annotation
  annotations.push({{
    x: '2026 H2 (forecast)', y: forecast26H2,
    xref:'x', yref:'y',
    text: '<b>VND ' + forecast26H2.toLocaleString() + 'B</b><br>Forecast — largest H2 yet',
    showarrow: true, arrowhead:2, arrowcolor:'#003D82',
    ax:0, ay:-55, font:{{size:11,color:'#003D82'}},
    bgcolor:'#E8F4FF', bordercolor:'#003D82', borderwidth:1, borderpad:5
  }});

  Plotly.newPlot('growth-compare-chart', [
    {{
      type: 'bar',
      x: xLabels.slice(0,-1),
      y: yActual,
      marker: {{ color: isH1.map(h => h ? '#5AC8FA' : '#003D82') }},
      name: 'Actual',
      hovertemplate: '%{{x}}: VND %{{y:,}}B<extra></extra>'
    }},
    {{
      type: 'bar',
      x: ['2026 H2 (forecast)'],
      y: [forecast26H2],
      marker: {{ color: '#0071E3', opacity:0.45,
                 line:{{color:'#0071E3', width:2, dash:'dot'}} }},
      name: 'Forecast',
      hovertemplate: 'Forecast 2026 H2: VND %{{y:,}}B<br>(based on H2 YoY avg growth)<extra></extra>'
    }}
  ], Object.assign({{}}, layoutBase, {{
    barmode: 'group',
    bargroupgap: 0.05,
    annotations: annotations,
    margin: {{ t:60, b:60, l:70, r:20 }},
    xaxis: Object.assign({{}}, layoutBase.xaxis, {{
      showgrid:false, zeroline:false,
      tickfont:{{size:11}}, tickangle:-30
    }}),
    yaxis: Object.assign({{}}, layoutBase.yaxis, {{
      showgrid:true, zeroline:false, ticksuffix:'B',
      title:{{ text:'VND B', font:{{size:12,color:GRAY}} }}
    }}),
    showlegend: false
  }}), plotlyConfig);
}})();

// VENDOR RISK SCATTER — discount % vs contract value
(function() {{
  const rows = {json.dumps(d['margin_table'])};

  // Only label top 6: highest value + highest discount
  const byValue = [...rows].sort((a,b) => b.value_b - a.value_b).slice(0,3).map(r => r.vendor);
  const byDisc  = [...rows].sort((a,b) => b.overall_disc - a.overall_disc).slice(0,3).map(r => r.vendor);
  const labeled = new Set([...byValue, ...byDisc]);
  const names   = rows.map(r => r.vendor.length > 20 ? r.vendor.substring(0,18)+'…' : r.vendor);

  Plotly.newPlot('vendor-risk-chart', [{{
    type: 'scatter', mode: 'markers+text',
    x: rows.map(r => r.overall_disc),
    y: rows.map(r => r.value_b),
    text: rows.map((r,i) => labeled.has(r.vendor) ? names[i] : ''),
    textposition: 'top center',
    textfont: {{ size:11, color:'#1D1D1F' }},
    customdata: rows.map(r => [r.wins, r.vendor]),
    marker: {{
      size: rows.map(r => Math.sqrt(r.wins)*6+8),
      color: rows.map(r => r.overall_disc > 15 ? '#FF3B30' : r.overall_disc > 8 ? '#F5A623' : '#0071E3'),
      opacity: 0.85,
      line: {{ color:'white', width:1.5 }}
    }},
    hovertemplate: '<b>%{{customdata[1]}}</b><br>Discount: %{{x}}%<br>Awarded: VND %{{y}}B<br>Wins: %{{customdata[0]}}<extra></extra>'
  }}], Object.assign({{}}, layoutBase, {{
    margin: {{ t:20, b:60, l:70, r:20 }},
    xaxis: Object.assign({{}}, layoutBase.xaxis, {{
      title: {{ text:'Overall Price Discount (%)', font:{{size:12,color:GRAY}} }},
      showgrid:true, zeroline:false, ticksuffix:'%', range:[-1,35]
    }}),
    yaxis: Object.assign({{}}, layoutBase.yaxis, {{
      title: {{ text:'Total Awarded (VND B)', font:{{size:12,color:GRAY}} }},
      showgrid:true, zeroline:false, ticksuffix:'B'
    }}),
    shapes:[
      {{ type:'rect', x0:15, x1:35, y0:0, y1:300,
         fillcolor:'rgba(255,59,48,0.05)', line:{{color:'rgba(255,59,48,0.3)',width:1,dash:'dot'}} }},
      {{ type:'rect', x0:-1, x1:8, y0:0, y1:300,
         fillcolor:'rgba(0,113,227,0.04)', line:{{color:'rgba(0,113,227,0.2)',width:1,dash:'dot'}} }}
    ],
    annotations:[
      {{ x:25, y:280, text:'High discount zone<br>(unsustainable)', showarrow:false,
         font:{{size:10,color:'#FF3B30'}}, bgcolor:'rgba(255,255,255,0.8)' }},
      {{ x:3, y:280, text:"Apple's zone<br>(quality wins)", showarrow:false,
         font:{{size:10,color:BLUE}}, bgcolor:'rgba(255,255,255,0.8)' }}
    ]
  }}), plotlyConfig);
}})();

// SUN VIET FOOTPRINT CHART
(function() {{
  const data    = {json.dumps(d['sunviet_chart'])};
  const labels  = data.map(r => r.account);
  const sv      = data.map(r => r.sv_b);
  const others  = data.map(r => r.other_b);

  // Annotation on Viettel bar
  const vIdx = labels.indexOf('Viettel');
  const annotations = vIdx >= 0 ? [{{
    x: 'Viettel', y: sv[vIdx] + 40,
    xref:'x', yref:'y',
    text: '★ VND 1,346B<br>Mac Studio / Pro entry point',
    showarrow: true, arrowhead: 2, arrowcolor: '#003D82',
    ax: 0, ay: -48,
    font: {{ size:11, color:'#003D82' }},
    bgcolor:'#E8F4FF', bordercolor:'#003D82', borderwidth:1, borderpad:5
  }}] : [];

  Plotly.newPlot('sunviet-chart', [
    {{
      type: 'bar', name: 'Sun Viet — won at this account',
      x: labels, y: sv,
      marker: {{ color: '#003D82', opacity: 0.9 }},
      hovertemplate: '<b>%{{x}}</b><br>Sun Viet: VND %{{y}}B<extra></extra>'
    }},
    {{
      type: 'bar', name: 'Other vendors at same account',
      x: labels, y: others,
      marker: {{ color: '#D2D2D7' }},
      hovertemplate: '<b>%{{x}}</b><br>Others: VND %{{y}}B<extra></extra>'
    }}
  ], Object.assign({{}}, layoutBase, {{
    barmode: 'group',
    annotations: annotations,
    margin: {{ t:60, b:50, l:60, r:20 }},
    xaxis: Object.assign({{}}, layoutBase.xaxis, {{ showgrid:false, zeroline:false }}),
    yaxis: Object.assign({{}}, layoutBase.yaxis, {{
      title: {{ text:'VND B', font:{{size:12,color:GRAY}} }},
      showgrid:true, zeroline:false, ticksuffix:'B'
    }}),
    legend: {{ orientation:'h', y:1.12, x:0, font:{{size:11}} }},
    showlegend: true
  }}), plotlyConfig);
}})();
</script>
</body>
</html>"""

    # Expand abbreviations in HTML text nodes only (not in <script> blocks)
    parts = html.split("<script")
    fixed_parts = []
    for i, part in enumerate(parts):
        if i == 0:
            part = part.replace("FPT IS", "FPT Information Systems")
            part = part.replace("ƯKTS", "Ứng dụng Kỹ thuật và Sản xuất")
            part = part.replace("Apple Government VAR", "Apple Authorized Government Reseller")
            part = part.replace("government VAR", "authorized government reseller")
            part = part.replace(" VAR", " authorized reseller")
            part = part.replace("TCO Study", "TCO Analysis")
            part = part.replace("NHNN", "Ngân hàng Nhà nước")
        fixed_parts.append(part)
    html = "<script".join(fixed_parts)
    return html


# ── Upload to GCS ─────────────────────────────────────────────────
def upload_gcs(html: str, month_str: str, draft: bool = False) -> str:
    # draft=True writes to a separate object and does NOT touch latest.html,
    # so the client-facing production URL is never overwritten during review.
    fname  = f"report-{month_str}-draft.html" if draft else f"report-{month_str}.html"
    local  = Path(f"/tmp/{fname}")
    local.write_text(html, encoding="utf-8")
    gcs_path = f"gs://{GCS_BUCKET}/{fname}"
    headers = ["-h", "Content-Type:text/html;charset=utf-8",
               "-h", "Cache-Control:no-cache,no-store,must-revalidate"]
    subprocess.run(["gsutil"] + headers + ["cp", str(local), gcs_path], check=True)
    if not draft:
        subprocess.run(["gsutil"] + headers + ["cp", str(local),
                        f"gs://{GCS_BUCKET}/latest.html"], check=True)
    return f"https://storage.googleapis.com/{GCS_BUCKET}/{fname}"


# ── Send email ────────────────────────────────────────────────────
def _email_body(url: str, d: dict, is_draft: bool = False) -> str:
    month = d["generated_at"]
    draft_banner = """
  <div style="background:#FFF3CD;border:1px solid #F5A623;border-radius:10px;padding:14px 20px;margin-bottom:20px">
    <p style="font-size:13px;color:#856404;margin:0;font-weight:600">⚠ DRAFT — Internal Review Only</p>
    <p style="font-size:12px;color:#856404;margin:6px 0 0">This report is a work in progress shared for early feedback. Data is based on a keyword-filtered dataset covering an estimated 60–75% of the addressable market. Absolute VND values are floor estimates. Do not distribute externally.</p>
  </div>""" if is_draft else ""

    return f"""
<div style="font-family:-apple-system,SF Pro Text,system-ui,sans-serif;max-width:600px;margin:0 auto;background:#fff;border-radius:18px;overflow:hidden;border:1px solid #D2D2D7">
  <div style="background:#000;padding:24px 32px;text-align:center">
    <svg viewBox="0 0 814 1000" style="width:24px;height:24px;fill:white"><path d="M788.1 340.9c-5.8 4.5-108.2 62.2-108.2 190.5 0 148.4 130.3 200.9 134.2 202.2-.6 3.2-20.7 71.9-68.7 141.9-42.8 61.6-87.5 123.1-155.5 123.1s-85.5-39.5-164-39.5c-76 0-103.7 40.8-165.9 40.8s-105-34.4-147.2-99.8C67.3 812.7 0 688.2 0 569.4c0-194.3 125.4-297.5 248.2-297.5 66.1 0 121 43.4 162.7 43.4 39.5 0 101.1-46 176.3-46 28.5 0 130.9 2.6 198.3 99.2zm-234-181.5c31.1-36.9 53.1-88.1 53.1-139.3 0-7.1-.6-14.3-1.9-20.1-50.6 1.9-110.8 33.7-147.1 75.8-28.5 32.4-55.1 83.6-55.1 135.5 0 7.8 1.3 15.6 1.9 18.1 3.2.6 8.4 1.3 13.6 1.3 45.4 0 102.5-30.4 135.5-71.3z"/></svg>
  </div>
  <div style="padding:32px">
    <p style="font-size:13px;color:#86868B;margin-bottom:8px;text-transform:uppercase;letter-spacing:.05em">Sales Intelligence</p>
    <h1 style="font-size:24px;font-weight:700;color:#1D1D1F;margin:0 0 8px;letter-spacing:-.02em">Vietnam Government IT Procurement</h1>
    <p style="font-size:15px;color:#6E6E73;margin:0 0 28px">{month} · {d['total_bids']:,} bids analyzed</p>
    {draft_banner}
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:28px">
      <div style="background:#F5F5F7;border-radius:12px;padding:16px">
        <div style="font-size:22px;font-weight:700;color:#0071E3">VND {int(d['apple_possible_b']):,}B</div>
        <div style="font-size:12px;color:#6E6E73;margin-top:4px">Apple-Addressable Market</div>
      </div>
      <div style="background:#F5F5F7;border-radius:12px;padding:16px">
        <div style="font-size:22px;font-weight:700;color:#1D1D1F">{round(d['apple_possible_b'] / max(d['apple_current_b'],0.1))}×</div>
        <div style="font-size:12px;color:#6E6E73;margin-top:4px">Potential vs. Current</div>
      </div>
    </div>
    <a href="{url}" style="display:block;background:#0071E3;color:white;text-align:center;padding:16px;border-radius:12px;font-size:15px;font-weight:600;text-decoration:none;margin-bottom:20px">View Full Report →</a>
    <p style="font-size:12px;color:#86868B;text-align:center">
      Direct link: <a href="{url}" style="color:#0071E3">{url}</a>
    </p>
  </div>
  <div style="background:#F5F5F7;padding:16px 32px;text-align:center;border-top:1px solid #D2D2D7">
    <p style="font-size:11px;color:#86868B">Confidential · Apple Vietnam Sales · Auto-generated monthly report</p>
  </div>
</div>"""


def send_email(url: str, d: dict):
    month = d["generated_at"]

    # Standard send to main recipients
    subject  = f"Apple Vietnam Procurement Intelligence — {month}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Sales Intelligence <{GMAIL_USER}>"
    msg["To"]      = ", ".join(RECIPIENTS)
    msg.attach(MIMEText(_email_body(url, d, is_draft=False), "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_PASS)
        s.sendmail(GMAIL_USER, RECIPIENTS, msg.as_string())
    print(f"Email sent to: {', '.join(RECIPIENTS)}")

    # Draft send to extended recipients
    if DRAFT_RECIPIENTS:
        draft_subject = f"[DRAFT] Apple Vietnam Procurement Intelligence — {month}"
        dmsg = MIMEMultipart("alternative")
        dmsg["Subject"] = draft_subject
        dmsg["From"]    = f"Sales Intelligence <{GMAIL_USER}>"
        dmsg["To"]      = ", ".join(DRAFT_RECIPIENTS)
        dmsg.attach(MIMEText(_email_body(url, d, is_draft=True), "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.sendmail(GMAIL_USER, DRAFT_RECIPIENTS, dmsg.as_string())
        print(f"Draft email sent to: {', '.join(DRAFT_RECIPIENTS)}")


# ── Main ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Pulling data...")
    d = analyse()
    print(f"Total bids: {d['total_bids']}, Apple-possible: VND {d['apple_possible_b']}B")

    print("Generating HTML...")
    html = generate_html(d)

    month_str = datetime.now().strftime("%Y-%m")
    print(f"Uploading to GCS ({month_str})...")
    url = upload_gcs(html, month_str)
    print(f"URL: {url}")

    print("Sending email...")
    send_email(url, d)
    print("Done.")
