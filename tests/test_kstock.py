"""kstock 합성 데이터 테스트 (네트워크·API 키 불필요)."""
from __future__ import annotations

from datetime import date, timedelta

from kstock import extras, kis, main, score as sc


def _master_line(market, code, name, *, grp="ST", mang="N", warn="00", mcap=5000, op=100, roe=12.5):
    n, h = kis._TAIL[market], kis._HEAD[market]
    tail = [" "] * n

    def put(a, b, v):
        tail[a:b] = list(str(v).rjust(b - a)[: b - a])

    put(*h["grp"], grp)
    put(*h["mang"], mang)
    put(*h["warn"], warn)
    put(*h["pref"], "0")
    put(*h["price"], 50000)
    put(*h["prev_vol"], 1_000_000)
    put(n - 15, n - 6, mcap)
    put(n - 55, n - 46, op)
    put(n - 32, n - 23, roe)
    return f"{code:<9}{'KR7' + code + '000':<12}{name}" + "".join(tail)


def test_parse_master_fields_and_filters():
    text = "\n".join([
        _master_line("KOSPI", "005930", "삼성전자", mcap=4000000),
        _master_line("KOSPI", "069500", "KODEX 200", grp="EF"),
        _master_line("KOSDAQ", "123456", "관리기업", mang="Y"),
        _master_line("KOSDAQ", "654321", "경고기업", warn="02"),
    ])
    st = kis.parse_master(text.split("\n")[0] + "\n" + text.split("\n")[1], "KOSPI") \
        + kis.parse_master("\n".join(text.split("\n")[2:]), "KOSDAQ")
    by = {s.code: s for s in st}
    s = by["005930"]
    assert (s.name, s.market, s.mcap, s.op_profit, s.roe, s.ok) == ("삼성전자", "KOSPI", 4000000, 100, 12.5, True)
    assert s.prev_value == 50000 * 1_000_000
    assert not by["069500"].ok and not by["123456"].ok and not by["654321"].ok


def _rows(n=60, frgn=20000, orgn=10000, vol_today=3_000_000, chg=2.0, price=50000):
    d = date(2026, 10, 9)
    rows = []
    for i in range(n):
        rows.append({
            "stck_bsop_date": (d - timedelta(days=i)).strftime("%Y%m%d"),
            "stck_clpr": str(price - i * 50), "stck_hgpr": str(price - i * 50 + 100),
            "acml_vol": str(vol_today if i == 0 else 1_500_000), "prdy_ctrt": str(chg if i == 0 else 0.1),
            "frgn_ntby_qty": str(frgn if i < 4 else -100), "orgn_ntby_qty": str(orgn if i < 4 else 50),
            "fund_ntby_qty": str(orgn // 2), "ivtr_ntby_qty": "1000", "pe_fund_ntby_vol": "0",
        })
    return rows


STOCK = kis.Stock("005930", "테스트전자", "KOSPI", mcap=20000, op_profit=100, roe=12, ok=True, prev_value=1e11)


def test_analyze_and_score():
    p = sc.score(sc.analyze(STOCK, _rows()))
    assert p.streak == 4
    assert round(p.vol_ratio, 1) == 2.0
    assert p.frgn5 > 0 and p.orgn5 > 0 and p.above_ma20 and p.ma_aligned and p.near_high
    assert p.parts == {"수급": p.parts["수급"], "거래량·추세": 25, "재무": 20, "뉴스": 15}
    assert 0 < p.parts["수급"] <= 40 and p.score == sum(p.parts.values())


def test_filters_reject():
    assert sc.analyze(STOCK, _rows(orgn=-5000)) is None          # 기관 순매도 → 쌍끌이 아님
    assert sc.analyze(STOCK, _rows(chg=16)) is None               # 당일 과열
    assert sc.analyze(STOCK, _rows(frgn=10, orgn=10)) is None     # 잡음 수준 금액
    small = kis.Stock("1", "소형", "KOSDAQ", mcap=1000, op_profit=1, roe=1, ok=True, prev_value=1e11)
    assert sc.analyze(small, _rows()) is None


class FakeKIS:
    def __init__(self, data):
        self.data = data

    def investor_daily(self, code, ymd):
        return self.data[code]


def test_run_ranks_excludes_bad_news_and_skips_today_in_morning():
    good = kis.Stock("000001", "좋은기업", "KOSPI", 20000, 100, 15, True, 1e11)
    weak = kis.Stock("000002", "보통기업", "KOSPI", 20000, -5, 1, True, 1e11)
    bad = kis.Stock("000003", "악재기업", "KOSPI", 20000, 100, 15, True, 1e11)
    skip = kis.Stock("000004", "관리", "KOSPI", 20000, 100, 15, False, 1e11)
    data = {"000001": _rows(), "000002": _rows(), "000003": _rows()}
    fake_news = lambda name: [(f"{name} 유상증자 결정", "u")] if name == "악재기업" else [(f"{name} 수주", "u")]

    kq = kis.Stock("000005", "코닥기업", "KOSDAQ", 20000, 100, 15, True, 1e11)
    data["000005"] = _rows()
    by, stats = main.run("close", FakeKIS(data), [good, weak, bad, skip, kq], news=fake_news,
                                      today="20261009")
    picks = by["KOSPI"]
    assert stats == {"scanned": 4, "errors": 0, "passed": {"KOSPI": 3, "KOSDAQ": 1}}
    assert [p.stock.name for p in picks] == ["좋은기업", "악재기업", "보통기업"]  # 시장별로 따로 순위
    assert [p.stock.name for p in by["KOSDAQ"]] == ["코닥기업"]
    assert picks[1].flags and picks[1].parts["뉴스"] == 10

    morning, _ = main.run("morning", FakeKIS({"000001": _rows(frgn=40000)}), [good], news=lambda n: [], today="20261009")
    assert morning["KOSPI"][0].date == "20261008"

    text = main.message("close", "KOSPI", picks, {**stats, "errors": 2}, dart_on=False)
    assert "KOSPI 수급 TOP 5" in text and "조회 실패 2종목" in text
    assert "m.stock.naver.com/domestic/stock/000001/total" in text
    assert "좋은기업 (000001·KOSPI)" in text and "⚠️ 악재기업 유상증자 결정" in text and "DART 미연결" in text
    assert "통과한 종목이 없습니다" in main.message("morning", "KOSDAQ", [], {"scanned": 10, "errors": 0, "passed": {"KOSDAQ": 0}}, True)


def test_bad_words():
    assert extras.is_bad("OO전자, 300억 규모 전환사채 발행") and not extras.is_bad("OO전자 신규 수주")


def test_telegram_prefers_kstock_chat(monkeypatch):
    sent = {}

    class R:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(main.requests, "post", lambda url, **kw: sent.update(url=url, chat=kw["json"]["chat_id"]) or R())
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "cot-bot")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "cot-chat")
    main.send_telegram("x")
    assert sent == {"url": "https://api.telegram.org/botcot-bot/sendMessage", "chat": "cot-chat"}
    monkeypatch.setenv("KSTOCK_TELEGRAM_BOT_TOKEN", "k-bot")
    monkeypatch.setenv("KSTOCK_TELEGRAM_CHAT_ID", "k-chat")
    main.send_telegram("x")
    assert sent == {"url": "https://api.telegram.org/botk-bot/sendMessage", "chat": "k-chat"}


def test_zero_close_rows_and_per_stock_errors_dont_crash():
    rows = _rows()
    rows[5]["stck_clpr"] = "0"
    assert sc.analyze(STOCK, rows) is None  # 0으로 나누기 없이 제외

    class Flaky(FakeKIS):
        def investor_daily(self, code, ymd):
            if code == "000002":
                raise RuntimeError("boom")
            return super().investor_daily(code, ymd)

    a = kis.Stock("000001", "정상", "KOSPI", 20000, 100, 15, True, 1e11)
    b = kis.Stock("000002", "에러", "KOSPI", 20000, 100, 15, True, 1e11)
    by, stats = main.run("close", Flaky({"000001": _rows()}), [a, b], news=lambda n: [],
                                      today="20261009")
    assert stats["scanned"] == 2 and stats["errors"] == 1 and by["KOSPI"][0].stock.name == "정상"


def test_large_caps_get_lower_cap_ratio_bar():
    big = kis.Stock("005930", "대형", "KOSPI", mcap=200_000, op_profit=1, roe=12, ok=True, prev_value=1e12)
    rows = _rows(frgn=400_000, orgn=200_000)  # 20일 누적 약 0.6조원
    pb = sc.analyze(big, rows)
    pb.flow20 = 0.2 / 100 * 200_000 * sc.EOK
    s_big = sc.score(pb).parts["수급"]
    pb.flow20 = 0.35 / 100 * 200_000 * sc.EOK
    assert sc.score(pb).parts["수급"] - s_big == 4  # 0.2% → 6점, 0.35% → 10점


def test_news_keeps_only_titles_with_name(monkeypatch):
    xml = ("<rss><channel><item><title>미코 주가 상승</title><link>a</link></item>"
           "<item><title>무료 슬롯 머신 가이드</title><link>b</link></item></channel></rss>").encode()

    class R:
        content = xml

        def raise_for_status(self):
            pass

    monkeypatch.setattr(extras.requests, "get", lambda *a, **k: R())
    assert extras.news("미코") == [("미코 주가 상승", "a")]
