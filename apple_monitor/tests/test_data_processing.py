"""
Unit tests for pure data-processing functions in apple_monitor.py.
No network, no Google Sheets, no external APIs required.
"""
import sys
import re
import pytest
from datetime import date, timedelta
from unittest.mock import patch

# Stub out the config import so tests run without real credentials
import types

_fake_config = types.ModuleType("apple_monitor_config")
_fake_config.KEYWORDS             = ["macbook", "ipad"]
_fake_config.GMAIL_SENDER         = "test@example.com"
_fake_config.GMAIL_APP_PASSWORD   = "fake"
_fake_config.EMAIL_RECIPIENTS     = ["r@example.com"]
_fake_config.GOOGLE_SHEET_ID      = "fake_id"
_fake_config.GOOGLE_CREDENTIALS_FILE = "fake.json"
_fake_config.GEMINI_API_KEY       = "fake_key"
_fake_config.API_TOKEN            = "fake_token"
_fake_config.API_COOKIES          = {}
sys.modules["apple_monitor_config"] = _fake_config

# Also stub heavy optional imports that may not be installed in CI
for mod in ["gspread", "google.auth", "google.oauth2", "google.oauth2.service_account",
            "googleapiclient", "googleapiclient.discovery", "googleapiclient.http",
            "google.genai", "playwright", "playwright.async_api"]:
    sys.modules.setdefault(mod, types.ModuleType(mod))

# Patch required names into stubs so `from X import Y` works
_sa = sys.modules["google.oauth2.service_account"]
_sa.Credentials = type("Credentials", (), {})

_disc = sys.modules["googleapiclient.discovery"]
_disc.build = lambda *a, **kw: None

_http = sys.modules["googleapiclient.http"]
_http.MediaFileUpload = type("MediaFileUpload", (), {})
_http.MediaIoBaseUpload = type("MediaIoBaseUpload", (), {})

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
from apple_monitor import (
    _clean, _days_left, _fmt_price, _fmt_date,
    _url_param, _build_source_url, flatten,
    _sort_by_value,
)


# ── _clean ────────────────────────────────────────────────────────

class TestClean:
    def test_strips_illegal_excel_chars(self):
        assert "\x00" not in _clean("hello\x00world")

    def test_passthrough_normal_string(self):
        assert _clean("hello world") == "hello world"

    def test_passthrough_non_string(self):
        assert _clean(42) == 42
        assert _clean(None) is None
        assert _clean(3.14) == 3.14


# ── _days_left ────────────────────────────────────────────────────

class TestDaysLeft:
    def test_future_date(self):
        future = (date.today() + timedelta(days=10)).isoformat()
        assert _days_left(future) == 10

    def test_today(self):
        assert _days_left(date.today().isoformat()) == 0

    def test_past_date(self):
        past = (date.today() - timedelta(days=5)).isoformat()
        assert _days_left(past) == -5

    def test_invalid_returns_none(self):
        assert _days_left("not-a-date") is None
        assert _days_left("") is None
        assert _days_left(None) is None

    def test_datetime_string_truncated(self):
        future = (date.today() + timedelta(days=3)).isoformat() + "T00:00:00"
        assert _days_left(future) == 3


# ── _fmt_price ────────────────────────────────────────────────────

class TestFmtPrice:
    def test_billion(self):
        assert _fmt_price("1000000000") == "1,00B VND"

    def test_half_billion(self):
        assert _fmt_price("500000000") == "0,50B VND"

    def test_invalid_returns_dash(self):
        assert _fmt_price("") == "—"
        assert _fmt_price(None) == "—"

    def test_non_numeric_returns_string(self):
        result = _fmt_price("abc")
        assert result == "abc"


# ── _url_param ────────────────────────────────────────────────────

class TestUrlParam:
    BASE = "https://muasamcong.mpi.gov.vn/web/guest/contractor-selection"

    def test_extracts_real_value(self):
        url = f"{self.BASE}?inputResultId=abc-123&bidOpenId=xyz"
        assert _url_param(url, "inputResultId") == "abc-123"

    def test_returns_empty_for_undefined(self):
        url = f"{self.BASE}?inputResultId=undefined"
        assert _url_param(url, "inputResultId") == ""

    def test_returns_empty_for_missing_param(self):
        url = f"{self.BASE}?foo=bar"
        assert _url_param(url, "inputResultId") == ""

    def test_returns_empty_for_empty_url(self):
        assert _url_param("", "inputResultId") == ""


# ── _build_source_url ─────────────────────────────────────────────

class TestBuildSourceUrl:
    def _make_item(self, **kwargs):
        base = {
            "id": "NOTIFY-001",
            "notifyId": "NOTIFY-001",
            "notifyNo": "IB2600001234",
            "stepCode": "OPEN_BID-tbmt",
            "inputResultId": "abc-123",
            "bidOpenId": "bid-456",
            "processApply": "LDT",
            "bidMode": "Trong nước",
            "planNo": "PLAN-001",
            "isInternet": "1",
            "caseKHKQ": None,
            "bidForm": "Đấu thầu rộng rãi",
        }
        base.update(kwargs)
        return base

    def test_contains_real_input_result_id(self):
        url = _build_source_url(self._make_item())
        assert "inputResultId=abc-123" in url

    def test_undefined_when_missing(self):
        url = _build_source_url(self._make_item(inputResultId=None))
        assert "inputResultId=undefined" in url

    def test_starts_with_base_url(self):
        url = _build_source_url(self._make_item())
        assert url.startswith("https://muasamcong.mpi.gov.vn")

    def test_step_extracted_from_step_code(self):
        url = _build_source_url(self._make_item(stepCode="OPEN_BID-tbmt"))
        assert "step=tbmt" in url


# ── flatten ───────────────────────────────────────────────────────

class TestFlatten:
    def _make_api_item(self):
        return {
            "notifyId":   "NID-001",
            "notifyNo":   "IB2600001234",
            "bidName":    "Mua sắm MacBook Pro",
            "investorName": "Bộ Khoa học",
            "investorCode": "vn001",
            "locations":  [{"provName": "Hà Nội"}],
            "publicDate": "2026-05-01T00:00:00",
            "bidCloseDate": "2026-06-01T00:00:00",
            "priceInit":  "5000000000",
            "bidForm":    "Đấu thầu rộng rãi",
            "bidMode":    "Trong nước",
            "status":     "IS_PUBLISH",
            "stepCode":   "IS_PUBLISH-tbmt",
            "inputResultId": "abc-123",
            "bidOpenId":  "bid-456",
            "processApply": "LDT",
        }

    def test_basic_fields(self):
        record = flatten(self._make_api_item(), "macbook")
        assert record["notifyId"] == "NID-001"
        assert record["keyword"] == "macbook"
        assert record["bid_name"] == "Mua sắm MacBook Pro"
        assert record["prov_name"] == "Hà Nội"

    def test_dates_truncated_to_10_chars(self):
        record = flatten(self._make_api_item(), "macbook")
        assert record["publicDate"] == "2026-05-01"
        assert record["bidCloseDate"] == "2026-06-01"

    def test_source_url_built(self):
        record = flatten(self._make_api_item(), "macbook")
        assert record["source_url"].startswith("https://muasamcong.mpi.gov.vn")

    def test_goods_url_empty(self):
        record = flatten(self._make_api_item(), "macbook")
        assert record["goods_url"] == ""

    def test_list_bid_name_joined(self):
        item = self._make_api_item()
        item["bidName"] = ["MacBook Pro", "M4 Max"]
        record = flatten(item, "macbook")
        assert "MacBook Pro" in record["bid_name"]
        assert "M4 Max" in record["bid_name"]

    def test_winner_list_joined(self):
        item = self._make_api_item()
        item["winningContractorName"] = ["Công ty A", "Công ty B"]
        record = flatten(item, "macbook")
        assert "Công ty A" in record["winner"]
        assert "Công ty B" in record["winner"]

    def test_no_locations_gives_empty_prov(self):
        item = self._make_api_item()
        item["locations"] = []
        record = flatten(item, "macbook")
        assert record["prov_name"] == ""


# ── _sort_by_value ────────────────────────────────────────────────

class TestSortByValue:
    def test_sorts_descending(self):
        records = [
            {"priceInit": "1000000000"},
            {"priceInit": "5000000000"},
            {"priceInit": "2000000000"},
        ]
        sorted_r = _sort_by_value(records)
        prices = [float(r["priceInit"]) for r in sorted_r]
        assert prices == sorted(prices, reverse=True)

    def test_handles_empty_price(self):
        records = [
            {"priceInit": ""},
            {"priceInit": "5000000000"},
        ]
        sorted_r = _sort_by_value(records)
        assert sorted_r[0]["priceInit"] == "5000000000"

    def test_empty_list(self):
        assert _sort_by_value([]) == []
