"""합성 데이터로 전체 파이프라인을 검증 (네트워크·API 불필요)."""
from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from cot import analyze, config, fetch, main, metrics as mx, render, rss

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
    monkeypatch.delenv("COT_AI_EXPECTED", raising=False)
    monkeypatch.setattr(rss, "collect", lambda *a: SAMPLE_RSS)


SAMPLE_RSS = [{"group": "Fed 보도자료", "title": "FOMC statement", "url": "https://www.federalreserve.gov/x",
               "published": "2026-07-29"}]


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
    with pytest.raises(SystemExit) as e:
        main.main(["--date", future.isoformat(), "--out", str(tmp_path), "--strict"])
    assert e.value.code == 3


def test_partial_data_marks_waiting(tmp_path, monkeypatch):
    def only_tff(ds, codes, since):
        if ds == config.DISAGG_DATASET:
            raise fetch.FetchError("down")
        return _synthetic_socrata("tff", since)
    monkeypatch.setattr(fetch, "_fetch_socrata", only_tff)
    monkeypatch.setattr(fetch, "_fetch_history_zip", lambda *a: [])
    monkeypatch.setattr(rss, "collect", lambda *a: [])
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


def _fake_claude_analysis(keys):
    return {
        "markets": [{"market": k, "rating": "Bearish", "rating_label": "약세", "stars": 2,
                     "card_note": "n", "change_note": "c", "smart_money": "s", "smart_money_icon": "🔴",
                     "extreme_note": "e", "contrarian": "반전 시나리오", "contrarian_tone": "bear",
                     "score": 0, "score_reason": "r", "prob_up": 20, "prob_side": 30, "prob_down": 50,
                     "verdict": "Reduce", "trend_short": "a", "trend_mid": "b", "trend_long": "c"}
                    for k in keys],
        "smart_money_highlight": "h", "sentiment": [{"label": "주식", "text": "뉴스 근거 추론"}],
        "top5": [{"title": "T", "body": "B"}], "exec_theme": "주간 테마",
        "exec_paragraphs": [{"label": "개요", "text": "p"}], "exec_recommendation": "권고",
        "monitoring": ["FOMC"], "trend_overview": "o", "sheet_opinion": "EUR Reduce", "sheet_memo": "m",
        "sources": [{"title": "Reuters", "url": "https://www.reuters.com/a"}, {"title": "bad", "url": "x"}],
    }


def test_prepare_then_render_with_claude_output(tmp_path, fake_fetch, monkeypatch):
    out, work = tmp_path / "docs", tmp_path / "work"
    gh_out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    base = ["--date", TARGET.isoformat(), "--out", str(out), "--work", str(work)]
    assert main.main(["prepare", *base]) == 0
    assert "run=true" in gh_out.read_text()
    prompt_text = (work / "prompt.md").read_text(encoding="utf-8")
    assert str((work / "analysis.json").resolve()) in prompt_text and "reuters.com" in prompt_text
    assert json.loads((work / "rss.json").read_text(encoding="utf-8")) == SAMPLE_RSS

    # Claude Code가 작성하는 파일 흉내
    keys = [m["key"] for m in json.loads((work / "metrics.json").read_text(encoding="utf-8"))]
    (work / "analysis.json").write_text(json.dumps(_fake_claude_analysis(keys), ensure_ascii=False),
                                        encoding="utf-8")
    (work / "news.md").write_text("① Fed — 동결 (Reuters)", encoding="utf-8")
    assert main.main(["render", *base]) == 0

    html = (out / "reports" / f"{TARGET}.html").read_text(encoding="utf-8")
    assert "주간 테마" in html and "뉴스 근거 추론" in html and "FOMC statement" in html
    assert "reuters.com/a" in html and 'href="x"' not in html
    meta = json.loads((out / "data" / "reports" / f"{TARGET}.json").read_text(encoding="utf-8"))["meta"]
    assert meta["ai"] is True
    for k, s in meta["scores"].items():  # score 0 → 퀀트 −15 이내로 보정
        assert s >= 0

    # AI 결과가 있는 완성 주차는 prepare에서 건너뜀
    gh_out.write_text("")
    monkeypatch.setenv("COT_AI_EXPECTED", "true")
    assert main.main(["prepare", *base]) == 0
    assert "run=false" in gh_out.read_text()


def test_render_with_broken_claude_output_falls_back(tmp_path, fake_fetch):
    out, work = tmp_path / "docs", tmp_path / "work"
    base = ["--date", TARGET.isoformat(), "--out", str(out), "--work", str(work)]
    main.main(["prepare", *base])
    (work / "analysis.json").write_text("{not json", encoding="utf-8")
    assert main.main(["render", *base]) == 0
    html = (out / "reports" / f"{TARGET}.html").read_text(encoding="utf-8")
    assert "형식 오류" in html


def test_postprocess_fills_missing_fields(fake_fetch):
    since = TARGET - timedelta(weeks=52 * 6)
    rows, _ = fetch.fetch_report("tff", since)
    metrics = {k: mx.market_metrics(config.MARKET_BY_KEY[k], s, TARGET)
               for k, s in fetch.split_by_market(rows, "tff").items()}
    res = analyze.postprocess({"markets": [{"market": "ES", "rating": "???", "score": "55"}]}, metrics)
    es = res["markets"]["ES"]
    assert es["rating"] in {"Bullish", "Neutral", "Bearish"} and es["prob_up"] is None
    assert set(res["markets"]) == set(metrics) and res["top5"] == []


def test_parse_feed_rss_and_atom():
    rss_xml = """<rss><channel><item><title>A  title</title><link>https://a</link>
    <pubDate>Wed, 29 Jul 2026 14:00:00 GMT</pubDate></item></channel></rss>"""
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>B</title>
    <link href="https://b"/><updated>2026-07-30T10:00:00Z</updated></entry></feed>"""
    a = rss.parse_feed(rss_xml)[0]
    b = rss.parse_feed(atom)[0]
    assert a["title"] == "A title" and a["url"] == "https://a" and a["published"].day == 29
    assert b["url"] == "https://b" and b["published"].day == 30


@pytest.mark.parametrize("text,expected", [
    ("2026년 9월 2주차", date(2026, 9, 8)),
    ("26년 9월 4주", date(2026, 9, 22)),
    ("2026-09-2주", date(2026, 9, 8)),
    ("2026-09-W5", date(2026, 9, 29)),
    ("2026년 7월 4주차", TARGET),
    ("2026-09-22", date(2026, 9, 22)),
    ("2026-09-25", date(2026, 9, 22)),   # 금요일 → 그 주 화요일
    ("2026.09.28", date(2026, 9, 22)),   # 월요일 → 직전 화요일
])
def test_parse_week(text, expected):
    d = fetch.parse_week(text)
    assert d == expected and d.weekday() == 1


@pytest.mark.parametrize("bad", ["2026년 2월 5주차", "구월 둘째주", "2026년 9월"])
def test_parse_week_rejects(bad):
    with pytest.raises(ValueError):
        fetch.parse_week(bad)


def test_week_label_and_cli(tmp_path, fake_fetch):
    assert fetch.week_label(date(2026, 9, 8)) == "2026년 9월 2주차"
    assert main.main(["--date", "2026년 7월 4주차", "--out", str(tmp_path)]) == 0
    html = (tmp_path / "reports" / f"{TARGET}.html").read_text(encoding="utf-8")
    assert "2026년 7월 4주차" in html
    assert "2026년 7월 4주차" in (tmp_path / "index.html").read_text(encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        main.main(["--date", "아무거나", "--out", str(tmp_path)])
    assert e.value.code == 2
