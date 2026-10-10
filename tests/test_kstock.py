"""kstock 합성 데이터 테스트 (네트워크·API 키 불필요)."""
from __future__ import annotations

from datetime import date, timedelta

from kstock import extras, kis, main, score as sc, signals as sg, site


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


def _rows(n=60, frgn=20000, orgn=10000, scrt=0, vol_today=3_000_000, chg=2.0, price=50000, wick=100,
          start=date(2026, 10, 9)):
    """최신일 먼저. 종가가 매일 50원씩 오르는 상승 추세, 오늘은 고가 근처 마감(윗꼬리 wick원)."""
    rows = []
    for i in range(n):
        c = price - i * 50
        rows.append({
            "stck_bsop_date": (start - timedelta(days=i)).strftime("%Y%m%d"),
            "stck_clpr": str(c), "stck_oprc": str(c - 300), "stck_hgpr": str(c + (wick if i == 0 else 100)),
            "stck_lwpr": str(c - 400),
            "acml_vol": str(vol_today if i == 0 else 1_500_000), "prdy_ctrt": str(chg if i == 0 else 0.1),
            "frgn_ntby_qty": str(frgn if i < 4 else -100), "orgn_ntby_qty": str(orgn if i < 4 else 50),
            "scrt_ntby_qty": str(scrt if i < 4 else 0),
            "fund_ntby_qty": str(orgn // 2), "ivtr_ntby_qty": "1000", "pe_fund_ntby_vol": "0",
        })
    return rows


def _index(start=date(2026, 10, 9), n=60):
    return {(start - timedelta(days=i)).strftime("%Y%m%d"): 1000 - i * 0.5 for i in range(n)}


STOCK = kis.Stock("005930", "테스트전자", "KOSPI", mcap=20000, op_profit=100, roe=12, ok=True, prev_value=1e11)


def test_analyze_and_score():
    p = sc.score(sc.analyze(STOCK, _rows(), _index()))
    assert (p.streak, p.frgn_streak, round(p.vol_ratio, 1)) == (4, 4, 2.0)
    assert round(p.clv, 2) == 0.8 and round(p.upper_wick, 2) == 0.2   # 고가 근처 마감
    assert p.above_ma20 and p.ma_aligned and p.near_high and 0 < p.ext_atr < 2
    assert p.rs20 is not None and 0 < p.rs20 < 3                       # 지수보다 약간 강함
    assert p.parts["거래량·캔들"] == 25 and p.parts["재무"] == 15
    assert p.parts["추세·위치"] == 12 + 5 + 2 and "뉴스" not in p.parts  # 뉴스 없음 = 가점 없음
    assert p.score == sum(p.parts.values()) <= 100
    assert sc.score(sc.analyze(STOCK, _rows())).rs20 is None             # 지수 없으면 상대강도 미검증(0점)


def test_score_bounds():
    p = sc.analyze(STOCK, _rows(frgn=2_000_000, orgn=2_000_000), _index())
    p.rs20, p.quality5 = 20, p.orgn5
    assert sc.score(p).score == 100
    p.flags = ["악재"] * 5
    assert sc.score(p).parts["뉴스"] == -15                              # 감점 상한
    weak = sc.analyze(kis.Stock("1", "약", "KOSPI", 20000, -1, -5, True, 1e11), _rows(wick=3000))
    weak.flags = ["악재"] * 5
    assert sc.score(weak).score >= 0


def test_volume_spike_with_upper_wick_is_penalized():
    good = sc.score(sc.analyze(STOCK, _rows(), _index()))
    wick = sc.score(sc.analyze(STOCK, _rows(wick=3000), _index()))     # 같은 거래량, 긴 윗꼬리
    assert wick.upper_wick > 0.5 and wick.clv < 0.4
    assert wick.parts["거래량·캔들"] == 10 and good.parts["거래량·캔들"] - wick.parts["거래량·캔들"] == 15


def test_filters_reject():
    assert sc.analyze(STOCK, _rows(orgn=-5000)) is None              # 기관 순매도
    assert sc.analyze(STOCK, _rows(orgn=10000, scrt=20000)) is None  # 기관 매수가 전부 금융투자 → 제외
    assert sc.analyze(STOCK, _rows(chg=16)) is None                   # 당일 과열
    assert sc.analyze(STOCK, _rows(frgn=10, orgn=10)) is None         # 잡음 수준 금액
    assert sc.analyze(STOCK, _rows(n=15)) is None                     # 데이터 부족
    zero = _rows()
    zero[5]["stck_clpr"] = "0"
    assert sc.analyze(STOCK, zero) is None                            # 잘못된 가격 → 0으로 나누지 않고 제외
    small = kis.Stock("1", "소형", "KOSDAQ", mcap=1000, op_profit=1, roe=1, ok=True, prev_value=1e11)
    assert sc.analyze(small, _rows()) is None


def test_large_caps_get_lower_cap_ratio_bar():
    big = kis.Stock("005930", "대형", "KOSPI", mcap=200_000, op_profit=1, roe=12, ok=True, prev_value=1e12)
    pb = sc.analyze(big, _rows(frgn=400_000, orgn=200_000))
    pb.flow20 = 0.2 / 100 * 200_000 * sc.EOK
    s_big = sc.score(pb).parts["수급"]
    pb.flow20 = 0.35 / 100 * 200_000 * sc.EOK
    assert sc.score(pb).parts["수급"] - s_big == 4  # 0.2% → 6점, 0.35% → 10점 (10조↑ 만점 0.3%)


class FakeKIS:
    def __init__(self, data, fail=()):
        self.data, self.fail = data, fail

    def investor_daily(self, code, ymd):
        if code in self.fail:
            raise RuntimeError("boom")
        return self.data[code]

    def index_daily(self, code, start, end):
        return _index()


def test_market_context_light_and_morning_ref():
    ctx = main.market_context(FakeKIS({}), "close", "20261009")
    assert ctx["KOSPI"]["ref"] == "20261009" and ctx["KOSPI"]["light"] == "🟢"
    assert main.market_context(FakeKIS({}), "morning", "20261009")["KOSDAQ"]["ref"] == "20261008"


def test_run_ranks_by_market_excludes_stale_errors_and_bad_news(tmp_path):
    mk = lambda code, name, market="KOSPI", op=100, roe=15: kis.Stock(code, name, market, 20000, op, roe, True, 1e11)
    good, weak, bad, kq = mk("000001", "좋은기업"), mk("000002", "보통기업", op=-5, roe=1), mk("000003", "악재기업"), \
        mk("000005", "코닥기업", "KOSDAQ")
    stale, err = mk("000006", "정지기업"), mk("000007", "에러기업")
    skip = kis.Stock("000004", "관리", "KOSPI", 20000, 100, 15, False, 1e11)
    data = {s.code: _rows() for s in (good, weak, bad, kq)}
    data["000006"] = _rows(start=date(2026, 10, 8))  # 기준일보다 하루 늦은 데이터
    fake = FakeKIS(data, fail={"000007"})
    news = lambda name: [(f"{name} 유상증자 결정", "u")] if name == "악재기업" else [(f"{name} 수주", "u")]
    ctx = main.market_context(fake, "close", "20261009")

    by, passed, stats, sigs = main.run("close", fake, [good, weak, bad, kq, stale, err, skip], ctx, news=news,
                                 today="20261009")
    assert stats == {"scanned": 6, "errors": 1, "stale": 1, "dq": 0, "passed": {"KOSPI": 3, "KOSDAQ": 1}}
    assert [p.stock.name for p in by["KOSPI"]] == ["좋은기업", "악재기업", "보통기업"]
    assert by["KOSPI"][1].parts["뉴스"] == -5 and [p.stock.name for p in by["KOSDAQ"]] == ["코닥기업"]

    hist = {"seen": {"000001": {"count": 3}}, "window": 10, "exits": ["빠진<기업>"]}
    text = main.message("close", "KOSPI", by["KOSPI"], stats, ctx, False, "20261009", hist=hist,
                        url="https://x.github.io/woong/kstock/")
    for s in ("<b>📈 KOSPI TOP 5</b>", "🟢", "조회 실패 1", "기준일 불일치 1", "DART 미연결", "<code>000001</code>",
              "고가 마감", "🔁 3/10일", "🆕", "⚠️악재뉴스", "🚪 이탈: 빠진&lt;기업&gt;", "조건 충족 3종목",
              'href="https://x.github.io/woong/kstock/#kospi"'):
        assert s in text, s
    assert "당일 데이터 미갱신" in main.message("close", "KOSPI", [], stats, ctx, False, "20261010")

    main.save_csv(tmp_path / "c.csv", "close", passed, by)
    lines = (tmp_path / "c.csv").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 + 4 and lines[0].startswith("date,session,market,top_rank,code")
    assert sorted(l.split(",")[3] for l in lines[1:]) == ["1", "1", "2", "3"]  # 코스피 1~3위 + 코스닥 1위

    # 웹페이지: 이틀치 저장 → 이탈·연속 등장·차트·목표가가 페이지에 나온다
    root = tmp_path / "site"
    by["KOSPI"][0].opinions = [{"stck_bsop_date": "20261001", "hts_goal_prc": "60000", "invt_opnn": "매수",
                                "mbcr_name": "OO증권"}]
    ctx_prev = {m: {**c, "ref": "20261008"} for m, c in ctx.items()}
    site.save_day(root, "20261008", {"KOSPI": by["KOSPI"] + [by["KOSDAQ"][0]], "KOSDAQ": []}, ctx_prev, stats, False)
    site.save_day(root, "20261009", by, ctx, stats, False)
    site.build(root, "https://x/")
    days = site.load_days(root)
    h = site.history(days, "KOSPI")
    assert h["seen"]["000001"]["count"] == 2 and h["exits"] == ["코닥기업"] and h["window"] == 2
    page = (root / "index.html").read_text(encoding="utf-8")
    for s in ("10/09(금)", 'href="20261008.html"', "<svg", "OO증권 매수 60,000원", "+20.0%", "이탈<sup>ⓘ</sup></button>: 코닥기업",
              'popovertarget="tip-ext"', 'id="tip-ext"', 'href="guide.html#ext"',
              "m.stock.naver.com/domestic/stock/000001/total", "finance.naver.com/item/main.naver?code=000001",
              "2일 중 2일"):
        assert s in page, s
    assert (root / "20261008.html").exists()
    guide = (root / "guide.html").read_text(encoding="utf-8")
    assert 'href="20261009.html"' in guide and "ATR 배수" in guide and "종가 위치" in guide
    assert 'href="guide.html"' in page
    import re
    targets = set(re.findall(r'popovertarget="(tip-[a-z0-9]+)"', page))
    assert targets <= set(re.findall(r'id="(tip-[a-z0-9]+)"', page))          # 누르는 곳마다 설명 상자가 있음
    assert set(re.findall(r'guide\.html#([a-z0-9]+)', page)) <= set(re.findall(r'id="([a-z0-9]+)"', guide))


def test_site_keeps_only_recent_days(tmp_path):
    for i in range(site.KEEP + 3):
        d = f"202610{i + 1:02d}"
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "data" / f"{d}.json").write_text('{"date": "%s", "markets": {}}' % d)
        (tmp_path / f"{d}.html").write_text("x")
    days = site.load_days(tmp_path)
    assert len(days) == site.KEEP and days[0]["date"] == "20261013"
    assert not (tmp_path / "data" / "20261001.json").exists() and not (tmp_path / "20261001.html").exists()


def test_morning_drops_today_row():
    good = kis.Stock("000001", "좋은기업", "KOSPI", 20000, 100, 15, True, 1e11)
    fake = FakeKIS({"000001": _rows(frgn=40000)})
    ctx = main.market_context(fake, "morning", "20261009")
    by, _, _, _ = main.run("morning", fake, [good], ctx, news=lambda n: [], today="20261009")
    assert by["KOSPI"][0].date == "20261008"
    text = main.message("morning", "KOSPI", by["KOSPI"], {"scanned": 1, "errors": 0, "stale": 0, "dq": 0,
                        "passed": {"KOSPI": 1}}, ctx, True, "20261009", "🌎 나스닥 +0.50%")
    assert "🌎 나스닥 +0.50%" in text and "갭상승" in text


def test_bad_words_and_relief():
    assert extras.is_bad("OO전자, 300억 규모 전환사채 발행") and not extras.is_bad("OO전자 신규 수주")
    assert not extras.is_bad("OO전자 유상증자 철회") and not extras.is_bad("OO바이오 특허 소송 승소")


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


def test_news_keeps_only_titles_with_name(monkeypatch):
    xml = ("<rss><channel><item><title>미코 주가 상승</title><link>a</link></item>"
           "<item><title>무료 슬롯 머신 가이드</title><link>b</link></item></channel></rss>").encode()

    class R:
        content = xml

        def raise_for_status(self):
            pass

    monkeypatch.setattr(extras.requests, "get", lambda *a, **k: R())
    assert extras.news("미코") == [("미코 주가 상승", "a")]
    xml = ("<rss><channel><item><title>개그맨, '미코' 출신 아내와 이혼</title><link>c</link></item>"
           "</channel></rss>").encode()
    R.content = xml
    assert extras.news("미코") == []  # 짧은 이름은 증권 단어 없으면 제외


def test_cron_is_kst_weekday_0747_and_1807_and_no_cot_dependency():
    from pathlib import Path

    import pytest

    yaml = pytest.importorskip("yaml")

    root = Path(__file__).resolve().parents[1]
    crons = [c["cron"] for c in yaml.safe_load((root / ".github/workflows/kstock-daily.yml").read_text())[True]["schedule"]]
    # UTC 일~목 21:50 = KST 월~금 06:50, UTC 월~금 05:35 = KST 14:35, UTC 월~금 09:07 = KST 18:07
    assert crons == ["50 21 * * 0-4", "35 5 * * 1-5", "7 9 * * 1-5"]
    wf = (root / ".github/workflows/kstock-daily.yml").read_text()
    assert all(f"github.event.schedule == '{c}'" in wf for c in crons[:2])  # 예약별 세션 판별이 cron과 일치
    for f in (root / "kstock").glob("*.py"):
        assert "from cot" not in f.read_text() and "import cot" not in f.read_text(), f



def _bars(closes, vols, highs=None, lows=None, opens=None, flow=1000, start=date(2026, 10, 9)):
    """오래된 날 먼저 받은 값으로 최신일 먼저인 일봉 행을 만든다."""
    n, rows = len(closes), []
    for k, c in enumerate(closes):
        rows.append({"stck_bsop_date": (start - timedelta(days=n - 1 - k)).strftime("%Y%m%d"), "stck_clpr": str(c),
                     "stck_oprc": str(opens[k] if opens else c - 50), "stck_hgpr": str(highs[k] if highs else c + 100),
                     "stck_lwpr": str(lows[k] if lows else c - 100), "acml_vol": str(vols[k]), "prdy_ctrt": "1",
                     "frgn_ntby_qty": str(flow), "orgn_ntby_qty": str(flow), "scrt_ntby_qty": "0"})
    return rows[::-1]


def _pullback(**kw):
    """50일 상승 → 3일 거래량 줄며 20일선 근처까지 눌림 → 오늘 고가권 반등."""
    cl = [10000 + 100 * i for i in range(50)] + [14700, 14400, 14150, 14450]
    hi, lo, op = [c + 100 for c in cl], [c - 100 for c in cl], [c - 50 for c in cl]
    hi[-1], lo[-1], op[-1] = 14480, 14100, 14150
    return _bars(cl, [1_000_000] * 50 + [600_000] * 3 + [1_100_000], hi, lo, op, **kw)


def _breakout(vol_today=2_500_000, **kw):
    """좁은 박스(약 6%) 55일 → 오늘 거래량 실어 고가권 돌파."""
    cl = [10000 + (i % 5) * 100 for i in range(55)] + [11000]
    hi, lo, op = [c + 100 for c in cl], [c - 100 for c in cl], [c - 50 for c in cl]
    hi[-1], lo[-1], op[-1] = 11050, 10350, 10400
    return _bars(cl, [1_000_000] * 55 + [vol_today], hi, lo, op, **kw)


def test_setup_a_pullback_and_sizing():
    x = sg.evaluate(STOCK, _pullback(), "🟢", 5, [])
    assert (x.setup, x.grade, x.entry) == ("A", "A", 14450)
    assert x.stop < 14100 and 0 < x.stop_pct < sg.MAX_STOP_PCT
    assert abs(x.weight - sg.RISK_PCT / x.stop_pct * 100) < 1e-9 and x.weight <= sg.MAX_WEIGHT
    assert x.r1 - x.entry == x.entry - x.stop and x.r2 - x.entry == 2 * (x.entry - x.stop)
    assert x.shares(10_000_000) == int(10_000_000 * x.weight / 100 // x.entry)
    assert sg.evaluate(STOCK, _pullback(flow=-5000), "🟢", 5, []) is None   # 눌림 중 큰손 순매도 → 무효


def test_setup_b_breakout_and_gates():
    x = sg.evaluate(STOCK, _breakout(), "🟢", 5, [])
    assert (x.setup, x.grade) == ("B", "A") and x.stop == 10350
    assert sg.evaluate(STOCK, _breakout(vol_today=1_200_000), "🟢", 5, []) is None   # 거래량 부족
    assert "이벤트" in " ".join(sg.evaluate(STOCK, _breakout(vol_today=6_000_000), "🟢", 5, []).notes)
    assert sg.evaluate(STOCK, _breakout(), "🔴", 5, []) is None                       # 🔴 신규 금지
    assert sg.evaluate(STOCK, _breakout(), "🟢", 5, ["악재"]) is None                 # 악재 뉴스
    assert sg.evaluate(STOCK, _breakout(), "🟡", 1, []) is None                       # 🟡는 A급만
    y = sg.evaluate(STOCK, _breakout(), "🟡", 5, [])
    assert y and y.risk_pct == sg.RISK_PCT / 2
    assert sg.evaluate(STOCK, _breakout(), "🟢", 1, []).grade == "B"


class IntradayKIS(FakeKIS):
    def price(self, code):
        return {"stck_prpr": "11000", "stck_oprc": "10400", "stck_hgpr": "11050", "stck_lwpr": "10350",
                "acml_vol": "2000000", "prdy_ctrt": "10"}

    def investor_estimate(self, code):
        return {"bsop_hour_gb": "4", "frgn_fake_ntby_qty": "5000", "orgn_fake_ntby_qty": "3000"}


def test_intraday_signals_message_and_site_box(tmp_path):
    from datetime import datetime
    hist = _breakout()[1:]  # 오늘(장중) 행은 현재가로 대신한다
    fake = IntradayKIS({})   # 장중에는 일별 투자자 API를 부르지 않는다 (KIS 00:00~15:40 조회 불가)
    ctx = main.market_context(fake, "morning", "20261009")
    for c in ctx.values():
        c["light"] = "🟢"
    watch = [{"code": "000001", "name": "돌파기업", "market": "KOSPI", "sector": ""}]
    now = datetime(2026, 10, 9, 14, 35, tzinfo=main.KST)
    assert main.run_intraday(fake, ctx, watch, {}, "20261009", now) == {"KOSPI": [], "KOSDAQ": []}  # 캐시 없음
    sigs = main.run_intraday(fake, ctx, watch, {"000001": hist}, "20261009", now)
    x = sigs["KOSPI"][0]
    assert x.setup == "B" and x.entry == 11000 and x.vol_ratio > 2   # 2,000,000주를 하루치로 환산
    assert "잠정 외인" in x.notes[-1]
    text = main.intraday_message(sigs, now, "https://x/", 10_000_000)
    assert "⏱ 장중 예비 신호 14:35" in text and "<code>000001</code>" in text and "주" in text

    root = tmp_path / "site"
    site.save_day(root, "20261008", {"KOSPI": [], "KOSDAQ": []}, {m: {**c, "close": 1.0, "chg": 0.0,
                  "above_ma20": True} for m, c in ctx.items()}, {"scanned": 0, "errors": 0, "stale": 0, "dq": 0,
                  "passed": {"KOSPI": 0, "KOSDAQ": 0}}, False, {"KOSPI": [x], "KOSDAQ": []}, [])
    site.save_intraday(root, "20261009", "14:35", sigs)
    site.build(root)
    page = (root / "index.html").read_text(encoding="utf-8")
    assert "⏱ 장중 예비 신호" in page and "돌파기업" in page and "다음 거래일 진입 후보" in page
    morning = main.morning_message("KOSPI", site.load_days(root)[0], "🌎 나스닥 +1.00%", "https://x/", 0)
    assert "🌅 KOSPI 장 전 브리핑" in morning and "오늘 진입 계획" in morning and "돌파기업" in morning
    assert "나스닥" in morning and "시초가" in morning
    site.save_day(root, "20261009", {"KOSPI": [], "KOSDAQ": []}, {m: {**c, "close": 1.0, "chg": 0.0,
                  "above_ma20": True} for m, c in ctx.items()}, {"scanned": 0, "errors": 0, "stale": 0, "dq": 0,
                  "passed": {"KOSPI": 0, "KOSDAQ": 0}}, False)
    site.build(root)
    assert not (root / "intraday.json").exists()                   # 장 마감 기록이 생기면 장중 상자 제거
    assert "⏱ 장중 예비 신호" not in (root / "index.html").read_text(encoding="utf-8")


def test_save_rows_keeps_only_needed_fields(tmp_path):
    p = sc.analyze(STOCK, _rows(), _index())
    main.save_rows(tmp_path / "rows.json", [p])
    import json
    rows = json.loads((tmp_path / "rows.json").read_text())["005930"]
    assert len(rows) == 40 and set(rows[0]) == set(main.ROW_KEYS)
    assert sg.evaluate(STOCK, _pullback(), "🟢", 5, []) is not None


def test_signal_lines_in_close_message():
    x = sg.evaluate(STOCK, _pullback(), "🟢", 5, [])
    text = "\n".join(main.signal_lines([x], "close", 10_000_000))
    assert "🎯 내일 진입 후보" in text and "매수 14,450~" in text and "시초가" in text and "자동감시주문" in text
    assert "없음" in "\n".join(main.signal_lines([], "close")) and main.signal_lines([], "intraday") == []



def test_demo_builds_three_messages(tmp_path, monkeypatch):
    from kstock import demo
    texts = demo.build(main, "https://x/", 10_000_000)
    assert len(texts) == 3 and all(t.startswith(demo.TAG) for t in texts)
    assert "① 장 전" in texts[0] and "오늘 진입 계획" in texts[0] and "🌎" in texts[0]
    assert "② 장중" in texts[1] and "⏱ 장중 예비 신호" in texts[1]
    assert "③ 장 마감" in texts[2] and "내일 진입 후보" in texts[2] and "🅐눌림" in texts[2] and "🅑돌파" in texts[2]
    assert all(len(t) <= 4096 for t in texts)
    monkeypatch.chdir(tmp_path)
    assert main.main(["--demo", "--dry-run"]) == 0 and (tmp_path / "work/kstock-demo-demo-msg.json").exists()


def test_stock_light_and_flow_line():
    rec = {"rank": 1, "code": "000001", "name": "가<나>", "score": 80, "frgn5": 87.4, "orgn5": -3.0,
           "streak": 0, "vol_ratio": 1.8, "clv": 0.8, "upper_wick": 0.1, "ext_atr": 1.0, "flags": []}
    head, tags = main.pick_lines(rec, signaled=True)
    assert head.startswith("🟢 1) <b>가&lt;나&gt;</b>") and "외인 +87억 · 기관 -3억 · 거래량 1.8배 · 고가 마감" in tags
    assert site.stock_light(rec, False) == "🟡"
    assert site.stock_light({**rec, "ext_atr": 4.0}, True) == "🔴"
    assert site.stock_light({**rec, "flags": ["횡령"]}, True) == "🔴"
