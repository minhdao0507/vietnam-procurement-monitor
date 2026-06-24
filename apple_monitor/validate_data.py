"""
validate_data.py — Data validation script for Apple Monitor.
Run before any analysis or monthly report generation.

Usage:
    python validate_data.py
    python validate_data.py --export-samples  # also write CSV for manual review
"""

import argparse
import csv
import json
import sys
import io
from collections import Counter, defaultdict
from datetime import datetime, date
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from google.cloud import secretmanager
from google.oauth2.service_account import Credentials
import gspread

# ── Config ────────────────────────────────────────────────────────
SHEET_ID   = "1gtsSjOkj0_tiP0g1Y4N_ruxd0iDnKkSiowcffVln3Fo"
PROJECT_ID = "apple-monitor"

EXPECTED_COLUMNS = [
    "notifyId", "keyword", "notifyNo", "bid_name", "investorName",
    "investorCode", "prov_name", "publicDate", "bidCloseDate", "priceInit",
    "bidForm", "bidMode", "status", "analysis", "crawled_at",
    "source_url", "winner", "winner_price",
]
# Present only when the Goods List feature is enabled — absence is not an error.
OPTIONAL_COLUMNS = ["goods_url"]

# Known bid form codes on muasamcong.mpi.gov.vn (from portal + data observations)
# DTRR=Đấu thầu rộng rãi, HCQT=Hạn chế, CHCT=Chào hàng cạnh tranh
# CHCTRG=Chào hàng cạnh tranh rút gọn, CDNT/CDNTRG/MSDD/TDKQ=Direct (not on portal)
# CGTT/CGTTRG/DTHC/TCTVCN/CQS=edge cases found in data (likely portal variants)
KNOWN_BID_FORMS = {
    "DTRR", "HCQT", "CHCT", "CHCTRG",  # competitive (appear on portal)
    "CDNT", "CDNTRG", "MSDD", "TDKQ",  # direct appointment (not on portal)
    "CGTT", "CGTTRG", "DTHC", "TCTVCN", "CQS",  # edge cases (classified as competitive variants)
}

ENTITY_KEYS = {
    "SVTech / Sun Viet": ["SUN VIỆT", "SUN VIET", "VIỄN THÔNG TIN HỌC SUN",
                          "SVTECH", "SV TECH", "SV-TECH"],
    "FPT IS":            ["HỆ THỐNG THÔNG TIN FPT", "FPT IS", "FPT INFORMATION"],
    "Viettel":           ["VIỄN THÔNG QUÂN ĐỘI", "VIETTEL"],
}

PRICE_MIN = 5_000_000      # 5 triệu VND — dưới mức này suspicious
PRICE_MAX = 50_000_000_000_000  # 50 nghìn tỷ — trên mức này suspicious

SAMPLES_PER_CATEGORY = 10


# ── Helpers ───────────────────────────────────────────────────────
def _secret(name):
    c = secretmanager.SecretManagerServiceClient()
    return c.access_secret_version(
        name=f"projects/{PROJECT_ID}/secrets/{name}/versions/latest"
    ).payload.data.decode("utf-8")


def _dump_csv(path, header, rows):
    """Write offending records to a CSV for manual root-cause inspection."""
    import csv, os
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


class ValidationReport:
    def __init__(self):
        self.results = []
        self.warnings = []
        self.failures = []
        self.manual_items = []
        self.samples = []
        self.stats = {}

    def ok(self, layer, msg):
        self.results.append(("PASS", layer, msg))

    def warn(self, layer, msg):
        self.warnings.append(("WARN", layer, msg))
        self.results.append(("WARN", layer, msg))

    def fail(self, layer, msg):
        self.failures.append(("FAIL", layer, msg))
        self.results.append(("FAIL", layer, msg))

    def manual(self, msg):
        self.manual_items.append(msg)

    def add_sample(self, category, row):
        self.samples.append({"category": category, **row})

    def verdict(self):
        if self.failures:
            return "FAIL"
        if self.warnings:
            return "CONDITIONAL PASS"
        return "PASS"

    def print_report(self):
        width = 68
        print("=" * width)
        print(f"VALIDATION REPORT — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print("=" * width)

        current_layer = None
        for status, layer, msg in self.results:
            if layer != current_layer:
                print(f"\n[{layer}]")
                current_layer = layer
            symbol = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}.get(status, "?")
            print(f"  {symbol} {msg}")

        if self.stats:
            print("\n[STATISTICS]")
            for k, v in self.stats.items():
                print(f"  {k}: {v}")

        if self.manual_items:
            print("\n[MANUAL REVIEW REQUIRED]")
            for item in self.manual_items:
                print(f"  → {item}")

        print("\n" + "=" * width)
        v = self.verdict()
        symbol = {"PASS": "✓", "CONDITIONAL PASS": "⚠", "FAIL": "✗"}[v]
        print(f"VERDICT: {symbol} {v}")
        print("=" * width)

        if v == "FAIL":
            print("\nDo NOT generate report until all FAILs are resolved.")
        elif v == "CONDITIONAL PASS":
            print("\nReport may be generated but must include limitations note.")


# ── Validation layers ─────────────────────────────────────────────
def layer1_schema(rows, header, rpt):
    """Layer 1: Schema — all expected columns present."""
    missing = [c for c in EXPECTED_COLUMNS if c not in header]
    extra   = [c for c in header
               if c not in EXPECTED_COLUMNS and c not in OPTIONAL_COLUMNS]

    if missing:
        rpt.fail("SCHEMA", f"Missing columns: {missing}")
    else:
        rpt.ok("SCHEMA", f"All {len(EXPECTED_COLUMNS)} required columns present")

    absent_opt = [c for c in OPTIONAL_COLUMNS if c not in header]
    if absent_opt:
        rpt.ok("SCHEMA", f"Optional columns not present (feature disabled): {absent_opt}")

    if extra:
        rpt.warn("SCHEMA", f"Extra columns (OK to ignore): {extra}")

    n = len(rows)
    if n < 5000:
        rpt.fail("SCHEMA", f"Row count = {n} — suspiciously low (expected ≥ 5,000)")
    else:
        rpt.ok("SCHEMA", f"Row count: {n:,}")

    rpt.stats["total_rows"] = f"{n:,}"


def layer2_field_format(rows, HDR, rpt):
    """Layer 2: Field formats — bidForm values, price units, dates, URLs."""
    bid_forms = Counter()
    prices = []
    date_errors = []
    url_samples = []
    price_zero_with_winner = 0
    negative_prices = 0

    for r in rows:
        if len(r) <= max(HDR.values()):
            continue

        bf = r[HDR["bidForm"]].strip()
        if bf:
            bid_forms[bf] += 1

        pi_str = r[HDR["priceInit"]].strip()
        winner = r[HDR["winner"]].strip()
        wp_str = r[HDR["winner_price"]].strip() if len(r) > HDR.get("winner_price", 999) else ""

        try:
            pi = float(pi_str) if pi_str else 0
            if pi < 0:
                negative_prices += 1
            elif pi > 0:
                prices.append(pi)
        except ValueError:
            pass

        try:
            wp = float(wp_str) if wp_str else 0
            if winner and wp == 0:
                price_zero_with_winner += 1
        except ValueError:
            pass

        pub = r[HDR["publicDate"]].strip()
        if pub:
            try:
                datetime.fromisoformat(pub[:10])
            except ValueError:
                date_errors.append(pub)

        url = r[HDR["source_url"]].strip() if len(r) > HDR.get("source_url", 999) else ""
        if url and len(url_samples) < 10:
            url_samples.append(url)

    # bidForm
    rpt.stats["bidForm_values"] = dict(bid_forms.most_common())
    unknown_forms = {f for f in bid_forms if f not in KNOWN_BID_FORMS}
    if unknown_forms:
        rpt.warn("FIELD FORMAT", f"Unknown bidForm values (need to classify): {unknown_forms}")
    else:
        rpt.ok("FIELD FORMAT", f"bidForm values all recognized: {set(bid_forms.keys())}")

    missing_forms = KNOWN_BID_FORMS - set(bid_forms.keys())
    if missing_forms:
        rpt.warn("FIELD FORMAT", f"bidForm values NOT seen in data: {missing_forms}")

    # Price units
    if prices:
        median_price = sorted(prices)[len(prices)//2]
        rpt.stats["price_median_VND"] = f"{median_price:,.0f}"
        rpt.stats["price_range_VND"] = f"{min(prices):,.0f} – {max(prices):,.0f}"
        if median_price < PRICE_MIN:
            rpt.fail("FIELD FORMAT", f"Median price {median_price:,.0f} suspicious — unit may not be VND")
        elif median_price > PRICE_MAX:
            rpt.fail("FIELD FORMAT", f"Median price {median_price:,.0f} suspicious — may be in wrong unit")
        else:
            rpt.ok("FIELD FORMAT", f"Price unit looks correct — median: {median_price/1e9:.1f}B VND")

    if negative_prices:
        rpt.fail("FIELD FORMAT", f"{negative_prices} rows have negative priceInit")
    if price_zero_with_winner:
        rpt.warn("FIELD FORMAT", f"{price_zero_with_winner} rows have winner but winner_price = 0")

    # Dates
    if date_errors:
        rpt.warn("FIELD FORMAT", f"{len(date_errors)} unparseable dates: {date_errors[:3]}")
    else:
        rpt.ok("FIELD FORMAT", "All dates parseable")

    # URLs
    rpt.manual(f"Source URL spot-check — verify these {len(url_samples)} URLs open correctly on portal:")
    for url in url_samples[:5]:
        rpt.manual(f"  {url}")


def layer3_business_logic(rows, HDR, rpt):
    """Layer 3: Business logic — consistency, duplicates, discount range."""
    from collections import defaultdict

    notify_ids = []
    nid_detail = defaultdict(list)   # notifyId -> [ {keyword, crawled_at, bid} ]
    price_exceed_rows = []           # rows where winner_price > priceInit
    discount_anomalies = []
    winner_no_price = 0
    price_exceeds_budget = 0

    kw_idx = HDR.get("keyword")
    ca_idx = HDR.get("crawled_at")

    for r in rows:
        if len(r) <= max(HDR.values()):
            continue

        nid    = r[HDR["notifyId"]].strip()
        winner = r[HDR["winner"]].strip()
        pi_str = r[HDR["priceInit"]].strip()
        wp_str = r[HDR["winner_price"]].strip() if len(r) > HDR.get("winner_price", 999) else ""
        bid    = r[HDR["bid_name"]][:60]

        if nid:
            notify_ids.append(nid)
            nid_detail[nid].append({
                "keyword":    r[kw_idx] if kw_idx is not None and len(r) > kw_idx else "",
                "crawled_at": r[ca_idx] if ca_idx is not None and len(r) > ca_idx else "",
                "bid":        bid,
            })

        try:
            pi = float(pi_str) if pi_str else 0
            wp = float(wp_str) if wp_str else 0
        except ValueError:
            pi, wp = 0, 0

        if winner and wp == 0:
            winner_no_price += 1

        if pi > 0 and wp > 0:
            # Only ratio > 1.5x is a genuine data error (corrupt budget, e.g.
            # priceInit=1 placeholder). Awards 1.01–1.5x over dự toán are a normal
            # procurement outcome in VN, not "impossible" — don't flag them.
            if wp > pi * 1.5:
                price_exceeds_budget += 1
                price_exceed_rows.append({
                    "notifyId": nid, "bid": bid,
                    "priceInit": pi, "winner_price": wp,
                    "ratio": round(wp / pi, 3),
                })
            disc = (1 - wp / pi) * 100
            if disc > 50:
                discount_anomalies.append({
                    "bid": r[HDR["bid_name"]][:50],
                    "discount_pct": round(disc, 1),
                    "pi_b": round(pi/1e9, 2),
                    "wp_b": round(wp/1e9, 2),
                })

    # Duplicates — WARN not FAIL: monthly_report.analyse() dedups by notifyId
    # (keeps latest crawled_at), so VND aggregations are not double-counted. Raw
    # Sheet dups come from occasional concurrent/double crawl runs (same keyword,
    # same day, minutes apart). Still surfaced for hygiene.
    dup_count = len(notify_ids) - len(set(notify_ids))
    if dup_count > 0:
        dup_ids = {n: v for n, v in nid_detail.items() if len(v) > 1}
        multi_kw = sum(1 for v in dup_ids.values()
                       if len({d["keyword"] for d in v}) > 1)
        same_day = sum(1 for v in dup_ids.values()
                       if len({d["crawled_at"][:10] for d in v}) == 1)
        rpt.warn("BUSINESS LOGIC",
                 f"{dup_count} duplicate notifyIds ({len(dup_ids)} ids) — handled by report dedup; "
                 f"{multi_kw} span multiple keywords, {same_day} same-day re-crawl")
        _dump_csv("/tmp/dup_notifyids.csv",
                  ["notifyId", "keyword", "crawled_at", "bid"],
                  [[n, d["keyword"], d["crawled_at"], d["bid"]]
                   for n, v in sorted(dup_ids.items()) for d in v])
        rpt.manual("Spot-check /tmp/dup_notifyids.csv — confirm dedup keeps latest crawled_at")
    else:
        rpt.ok("BUSINESS LOGIC", "No duplicate notifyIds")

    if price_exceeds_budget > 0:
        # WARN not FAIL: these are corrupt budgets (priceInit placeholder like 1 VND),
        # not unit errors. Report drops the bogus budget (pi=0) but keeps the real
        # award value. Mild 1.01–1.5x overruns are excluded — they're legitimate.
        rpt.warn("BUSINESS LOGIC",
                 f"{price_exceeds_budget} bids: winner_price > 1.5× priceInit — corrupt budget, "
                 f"handled by report (pi dropped, award kept)")
        _dump_csv("/tmp/price_exceed.csv",
                  ["notifyId", "bid", "priceInit", "winner_price", "ratio"],
                  [[x["notifyId"], x["bid"], x["priceInit"], x["winner_price"], x["ratio"]]
                   for x in sorted(price_exceed_rows, key=lambda x: -x["ratio"])])
        worst = sorted(price_exceed_rows, key=lambda x: -x["ratio"])[:3]
        for x in worst:
            rpt.warn("BUSINESS LOGIC",
                     f"  ratio {x['ratio']}x — {x['bid']} (pi={x['priceInit']:.0f} wp={x['winner_price']:.0f})")
        rpt.manual("Spot-check /tmp/price_exceed.csv — corrupt priceInit (budget not published)")
    else:
        rpt.ok("BUSINESS LOGIC", "All winner_price ≤ 1.5× priceInit")

    if discount_anomalies:
        rpt.warn("BUSINESS LOGIC",
                 f"{len(discount_anomalies)} bids with discount > 50% — review for data error")
        for a in discount_anomalies[:3]:
            rpt.warn("BUSINESS LOGIC", f"  {a['bid']} — {a['discount_pct']}% off")

    rpt.stats["winner_no_price"] = f"{winner_no_price} bids have winner name but no price"
    rpt.ok("BUSINESS LOGIC", f"Winner without price: {winner_no_price} (normal for incomplete records)")


def layer4_classification_samples(rows, HDR, rpt, export=False):
    """Layer 4: Classification spot-check — output samples for manual review."""
    from collections import defaultdict
    import random

    TECH_KW = [
        "máy tính", "laptop", "máy chủ", "server", "thiết bị công nghệ",
        "thiết bị it", "phần mềm", "máy in", "máy tính bảng", "ipad",
        "iphone", "macbook", "apple", "màn hình", "máy scan", "scanner",
        "switch", "router", "mạng", "thiết bị điện tử", "thiết bị văn phòng",
        "camera", "ups", "lưu trữ", "storage", "workstation", "tablet",
        "điện thoại", "thiết bị thông tin",
    ]
    APPLE_EXPLICIT = ["apple", "iphone", "ipad", "macbook", "imac",
                      "mac pro", "mac mini", "mac studio", "airpods"]
    APPLE_DEV_KW   = ["máy tính xách tay", "laptop", "máy tính bảng", "tablet",
                      "máy tính để bàn", "desktop", "all-in-one",
                      "điện thoại thông minh", "smartphone"]
    WIN_KW         = ["windows", "microsoft office", "ms office", "office 365",
                      "exchange server", "windows server", "active directory"]

    buckets = defaultdict(list)
    for r in rows:
        if len(r) < 14:
            continue
        bid_name = r[HDR["bid_name"]].strip()
        analysis = r[HDR["analysis"]].strip() if len(r) > HDR.get("analysis", 999) else ""
        name = bid_name.lower()
        text = (bid_name + " " + analysis).lower()

        if any(k in name for k in APPLE_EXPLICIT):
            cat = "apple_explicit"
        elif any(k in text for k in WIN_KW):
            cat = "windows_locked"
        elif any(k in name for k in APPLE_DEV_KW):
            cat = "apple_possible"
        elif any(k in name for k in TECH_KW):
            cat = "other_tech"
        else:
            continue
        buckets[cat].append({"bid_name": bid_name, "analysis": analysis[:80],
                              "bidForm": r[HDR["bidForm"]].strip(),
                              "priceInit": r[HDR["priceInit"]].strip(),
                              "investor": r[HDR["investorName"]].strip()[:40]})

    rpt.stats["classification"] = {cat: len(v) for cat, v in buckets.items()}
    rpt.manual("Classification spot-check — manually verify sample bids below are in correct category:")

    sample_rows = []
    for cat, bids in buckets.items():
        sample = random.sample(bids, min(SAMPLES_PER_CATEGORY, len(bids)))
        for b in sample:
            sample_rows.append({"category": cat, **b})
            rpt.add_sample(cat, b)

    # Warn about windows_locked using analysis column
    for r in rows:
        if len(r) < 14:
            continue
        bid_name = r[HDR["bid_name"]].strip()
        analysis = r[HDR["analysis"]].strip() if len(r) > HDR.get("analysis", 999) else ""
        name = bid_name.lower()
        if any(k in analysis.lower() for k in WIN_KW) and not any(k in name for k in WIN_KW):
            rpt.warn("CLASSIFICATION",
                     "Some bids classified windows_locked based on Gemini analysis column, not bid_name — risk of false positive")
            break

    rpt.ok("CLASSIFICATION", f"Buckets: " + ", ".join(f"{k}={len(v)}" for k, v in buckets.items()))

    if export:
        out = Path("/tmp/validation_samples.csv")
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["category", "bid_name", "analysis",
                                               "bidForm", "priceInit", "investor"])
            w.writeheader()
            w.writerows(sample_rows)
        rpt.manual(f"Classification samples exported to: {out}")

    return sample_rows


def layer5_entity_matching(rows, HDR, rpt):
    """Layer 5: Verify entity key matching (SVTech, FPT IS, Viettel)."""
    def is_match(winner_raw, keys):
        lead = winner_raw.split("|")[0].upper().strip()
        return any(k in lead for k in keys)

    entity_counts = {name: 0 for name in ENTITY_KEYS}
    entity_samples = {name: [] for name in ENTITY_KEYS}
    winner_samples_all = Counter()

    for r in rows:
        winner = r[HDR["winner"]].strip() if len(r) > HDR.get("winner", 999) else ""
        if not winner:
            continue
        lead = winner.split("|")[0].strip().upper()
        if len(lead) > 3:
            winner_samples_all[lead[:40]] += 1
        for name, keys in ENTITY_KEYS.items():
            if is_match(winner, keys):
                entity_counts[name] += 1
                if len(entity_samples[name]) < 3:
                    entity_samples[name].append(winner[:60])

    for name, count in entity_counts.items():
        if count == 0:
            rpt.fail("ENTITY MATCHING", f"{name}: 0 matches — keys may be wrong or entity not in data")
        elif count < 5:
            rpt.warn("ENTITY MATCHING", f"{name}: only {count} matches — verify keys are complete")
        else:
            rpt.ok("ENTITY MATCHING", f"{name}: {count} bids matched")
            for s in entity_samples[name]:
                rpt.ok("ENTITY MATCHING", f"    sample: {s}")

    # Top 20 winner names for manual review
    rpt.stats["top_winner_names"] = dict(winner_samples_all.most_common(20))
    rpt.manual("Review top winner names in stats — check for SVTech/FPT IS variants we may be missing")


def layer6_keyword_health(rows, HDR, rpt):
    """Layer 6: Keyword health — bid count per keyword, dead keywords."""
    kw_counts = Counter()
    kw_by_year = defaultdict(lambda: Counter())

    for r in rows:
        kw  = r[HDR["keyword"]].strip().lower() if len(r) > HDR.get("keyword", 999) else ""
        pub = r[HDR["publicDate"]].strip()
        yr  = pub[:4] if pub and len(pub) >= 4 else "unknown"
        if kw:
            kw_counts[kw] += 1
            kw_by_year[yr][kw] += 1

    rpt.stats["keyword_counts"] = dict(kw_counts.most_common())

    dead = [kw for kw, cnt in kw_counts.items() if cnt < 5]
    if dead:
        rpt.warn("KEYWORD HEALTH", f"Keywords with < 5 bids total: {dead}")
    else:
        rpt.ok("KEYWORD HEALTH", f"{len(kw_counts)} keywords active, all have ≥ 5 bids")

    zero_recent = []
    current_year = str(datetime.now().year)
    for kw in kw_counts:
        if kw_by_year.get(current_year, {}).get(kw, 0) == 0:
            zero_recent.append(kw)
    if zero_recent:
        rpt.warn("KEYWORD HEALTH", f"No bids in {current_year} for: {zero_recent}")


# ── Main ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--export-samples", action="store_true",
                        help="Export classification samples to /tmp/validation_samples.csv")
    args = parser.parse_args()

    rpt = ValidationReport()

    print("Loading data from Google Sheet...")
    sa_json = _secret("GOOGLE_SERVICE_ACCOUNT_JSON")
    info    = json.loads(sa_json)
    creds   = Credentials.from_service_account_info(
        info, scopes=["https://spreadsheets.google.com/feeds",
                      "https://www.googleapis.com/auth/drive"]
    )
    ws   = gspread.authorize(creds).open_by_key(SHEET_ID).worksheets()[0]
    all_rows = ws.get_all_values()
    header   = all_rows[0]
    rows     = all_rows[1:]
    HDR      = {h: i for i, h in enumerate(header)}
    print(f"Loaded {len(rows):,} rows.\n")

    print("Running validation layers...")
    layer1_schema(rows, header, rpt)
    layer2_field_format(rows, HDR, rpt)
    layer3_business_logic(rows, HDR, rpt)
    layer4_classification_samples(rows, HDR, rpt, export=args.export_samples)
    layer5_entity_matching(rows, HDR, rpt)
    layer6_keyword_health(rows, HDR, rpt)

    rpt.print_report()

    # Exit code for CI/scripting
    sys.exit(0 if rpt.verdict() != "FAIL" else 1)


if __name__ == "__main__":
    main()
