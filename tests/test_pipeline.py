"""합성 데이터로 전체 파이프라인을 검증 (네트워크·API 불필요)."""
from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from cot import analyze, config, fetch, main, metrics as mx, render

TARGET = date(2026, 7, 28)


def _synthetic_socrata(report: str, since: date) -> list[dict]:
    rnd = random.Random(42)
    rows = []
    for m in config.MARKETS:
        if m.report != report:
            continue
        d = since + timedelta(days=(1 - since.weekday()) % 7)
        t = 0
        while d <= TARGET:
            base = 100_000 + 40_000 * math.sin(t / 15)
            r = {
                # Socrata 형식 컬럼명 (소문자, 일부 _all 없음)
                "report_date_as_yyyy_mm_dd": f"{d.isoformat()}T00:00:00.000",
                "cftc_contract_market_code": m.codes[0],
                "market_and_exchange_names": f"{m.name} - TEST EXCHANGE",
                "open_interest_all": str(int(500_000 + rnd.randint(-20_000, 20_000))),
            }
            if report == "tff":
                r.update({
                    "lev_money_positions_long": str(int(base + rnd.randint(0, 20_000))),
                    "lev_money_positions_short": str(int(150_000 + rnd.randint(0, 20_000))),
                    "asset_mgr_positions_long": str(int(300_000 - base / 2 + rnd.randint(0, 10_000))),
                    "asset_mgr_positions_short": str(int(120_000 + rnd.randint(0, 10_000))),
                    "dealer_positions_long_all": "1000", "dealer_positions_short_all": "2000",
                })
            else:
                r.update({
                    "m_money_positions_long_all": str(int(base + 60_000)),
                    "m_money_positions_short_all": str(int(90_000 + rnd.randint(0, 10_000))),
                })
            rows.append(r)
            d += timedelta(days=7)
            t += 1
    return fetch.normalize_rows(rows)


@pytest.fixture
def fake_fetch(monkeypatch):
    monkeypatch.setattr(fetch, "_fetch_socrata", lambda ds, codes, since:
                        _synthetic_socrata("tff" if ds == config.TFF_DATASET else "disagg", since))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_normalize_history_csv_headers():
    rows = [{"Market_and_Exchange_Names": "X", "Report_Date_as_YYYY-MM-DD": "2026-07-28",
             "CFTC_Contract_Market_Code": " 099741 ", "Open_Interest_All": "1,000",
             "Lev_Money_Positions_Long_All": "10", "Lev_Money_Positions_Short_All": "30",
             "Asset_Mgr_Positions_Long_All": "50", "Asset_Mgr_Positions_Short_All": "5"}]
    rec = fetch.normalize_rows(rows)[0]
    assert rec["code"] == "099741" and rec["date"] == TARGET
    assert rec["oi"] == 1000 and rec["lev_long"] == 10 and rec["am_short"] == 5


def test_expected_report_date():
    # 금요일 발표 전(15:00 UTC) → 전주 화요일, 토요일 → 이번 주 화요일
    assert fetch.expected_report_date(datetime(2026, 7, 31, 15, tzinfo=timezone.utc)) == date(2026, 7, 21)
    assert fetch.expected_report_date(datetime(2026, 8, 1, 1, tzinfo=timezone.utc)) == TARGET
    assert fetch.expected_report_date(datetime(2026, 8, 4, 12, tzinfo=timezone.utc)) == TARGET


@pytest.mark.parametrize("lc,sc,expected", [
    (100, 10, "신규 Long 구축"), (10, -100, "Short Covering"),
    (-10, 100, "신규 Short 구축"), (-100, 10, "Long 청산"), (5, 5, "변화 없음"),
])
def test_diagnose(lc, sc, expected):
    assert mx.diagnose(lc, sc) == expected


def test_percentile_and_quant():
    assert mx.percentile_rank([1, 2, 3, 4], 4) == 100
    assert mx.cot_index([0, 10], 5) == 50


def test_pipeline_no_ai(tmp_path, fake_fetch):
    rc = main.main(["--date", TARGET.isoformat(), "--out", str(tmp_path)])
    assert rc == 0
    html = (tmp_path / "reports" / f"{TARGET}.html").read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html and 'charset="UTF-8"' in html
    assert "var(--" not in html  # CSS 변수 사용 금지
    for sec in ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭"]:
        assert sec in html
    assert "<svg" in html and "AI 분석 미실행" in html
    meta = json.loads((tmp_path / "data" / "reports" / f"{TARGET}.json").read_text(encoding="utf-8"))["meta"]
    assert meta["complete"] and set(meta["scores"]) == {m.key for m in config.MARKETS}
    assert (tmp_path / "index.html").exists()
    assert "ES Net" in (tmp_path / "data" / "history.csv").read_text(encoding="utf-8")

    # 완성된 주차는 재실행 시 건너뜀
    mtime = (tmp_path / "index.html").stat().st_mtime_ns
    assert main.main(["--date", TARGET.isoformat(), "--out", str(tmp_path)]) == 0
    assert (tmp_path / "index.html").stat().st_mtime_ns == mtime


def test_missing_week_publishes_nothing(tmp_path, fake_fetch):
    future = TARGET + timedelta(days=7)
    assert main.main(["--date", future.isoformat(), "--out", str(tmp_path)]) == 0
    assert not (tmp_path / "reports").exists()
    assert main.main(["--date", future.isoformat(), "--out", str(tmp_path), "--strict"]) == 3


def test_partial_data_marks_waiting(tmp_path, monkeypatch):
    def only_tff(ds, codes, since):
        if ds == config.DISAGG_DATASET:
            raise fetch.FetchError("down")
        return _synthetic_socrata("tff", since)
    monkeypatch.setattr(fetch, "_fetch_socrata", only_tff)
    monkeypatch.setattr(fetch, "_fetch_history_zip", lambda *a: [])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main.main(["--date", TARGET.isoformat(), "--out", str(tmp_path)]) == 0
    html = (tmp_path / "reports" / f"{TARGET}.html").read_text(encoding="utf-8")
    assert "데이터 수신 대기" in html
    meta = json.loads((tmp_path / "data" / "reports" / f"{TARGET}.json").read_text(encoding="utf-8"))["meta"]
    assert not meta["complete"] and "WTI 원유" in meta["missing"]


def test_render_with_ai_analysis(fake_fetch):
    since = TARGET - timedelta(weeks=52 * 6)
    metrics = {}
    for report in ("tff", "disagg"):
        rows, _ = fetch.fetch_report(report, since)
        for k, s in fetch.split_by_market(rows, report).items():
            metrics[k] = mx.market_metrics(config.MARKET_BY_KEY[k], s, TARGET)
    fake = {
        "markets": [{
            "market": k, "rating": "Bullish", "rating_label": "Bullish", "stars": 9,
            "card_note": "테스트 <b>노트</b>", "change_note": "c", "smart_money": "s", "smart_money_icon": "✅",
            "extreme_note": "e", "contrarian": "반전", "contrarian_tone": "bull",
            "score": 100, "score_reason": "r", "prob_up": 50, "prob_side": 30, "prob_down": 30,
            "verdict": "Buy", "trend_short": "a", "trend_mid": "b", "trend_long": "c",
        } for k in metrics],
        "smart_money_highlight": "h", "sentiment": [{"label": "주식", "text": "t"}],
        "top5": [{"title": "T", "body": "B"}], "exec_theme": "테마",
        "exec_paragraphs": [{"label": "개요", "text": "p"}], "exec_recommendation": "권고",
        "monitoring": ["FOMC"], "trend_overview": "o", "sheet_opinion": "JPY Buy", "sheet_memo": "memo",
    }
    analysis = analyze.postprocess(fake, metrics)
    for k, m in analysis["markets"].items():
        q = metrics[k]["quant"]["total"]
        assert m["score"] <= q + 15 and m["stars"] == 5
        assert m["prob_up"] + m["prob_side"] + m["prob_down"] == 100
    html = render.render_report(
        report_date=TARGET, release=fetch.release_date(TARGET),
        run_kst=datetime(2026, 8, 1, 9, 30), metrics=metrics, analysis=analysis,
        news={"text": "① Fed ...", "sources": [{"title": "Fed", "url": "https://federalreserve.gov"}]},
        data_sources=["test"], model="test-model",
    )
    assert "&lt;b&gt;노트" in html  # 모델 출력은 이스케이프
    assert "JPY Buy" in html and "federalreserve.gov" in html
    header, row = render.sheet_row(TARGET, metrics, analysis)
    assert header.split("\t")[:3] == ["날짜", "ES Net", "NQ Net"]
    assert row.split("\t")[1].startswith("'")
