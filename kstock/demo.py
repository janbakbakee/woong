"""데모 메시지: 합성 데이터로 장 전·장중·장 마감 텔레그램 양식을 미리 본다 (KIS 호출 없음).

    python -m kstock.main --demo            # 3통 전송 (dry-run이면 출력만)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import kis, score as sc, signals as sg, site

TAG = "🧪 <b>데모 데이터</b> — 실제 종목·시세 아님, 양식 미리보기\n"
REF = date(2026, 10, 12)


def _bars(closes, vols, highs, lows, opens, flow):
    n = len(closes)
    rows = [{"stck_bsop_date": (REF - timedelta(days=n - 1 - k)).strftime("%Y%m%d"), "stck_clpr": str(c),
             "stck_oprc": str(opens[k]), "stck_hgpr": str(highs[k]), "stck_lwpr": str(lows[k]),
             "acml_vol": str(vols[k]), "prdy_ctrt": f"{(c / closes[k - 1] - 1) * 100 if k else 0:.2f}",
             "frgn_ntby_qty": str(flow), "orgn_ntby_qty": str(flow // 2), "scrt_ntby_qty": "0",
             "fund_ntby_qty": str(flow // 3), "ivtr_ntby_qty": str(flow // 6), "pe_fund_ntby_vol": "0"}
            for k, c in enumerate(closes)]
    return rows[::-1]


def _pullback():
    cl = [10000 + 100 * i for i in range(50)] + [14700, 14400, 14150, 14450]
    hi, lo, op = [c + 100 for c in cl], [c - 100 for c in cl], [c - 50 for c in cl]
    hi[-1], lo[-1], op[-1] = 14480, 14100, 14150
    return _bars(cl, [1_000_000] * 50 + [600_000] * 3 + [1_100_000], hi, lo, op, 120_000)


def _breakout():
    cl = [30000 + (i % 5) * 300 for i in range(55)] + [33000]
    hi, lo, op = [c + 300 for c in cl], [c - 300 for c in cl], [c - 150 for c in cl]
    hi[-1], lo[-1], op[-1] = 33150, 31050, 31200
    return _bars(cl, [500_000] * 55 + [1_250_000], hi, lo, op, 60_000)


def _trend():
    cl = [20000 + 60 * i for i in range(60)]
    return _bars(cl, [600_000] * 59 + [900_000], [c + 150 for c in cl], [c - 150 for c in cl],
                 [c - 100 for c in cl], 80_000)


def build(main, url: str, cap: float) -> list[str]:
    """main 모듈의 실제 메시지 함수로 장 전·장중·장 마감 1통씩."""
    ref = REF.strftime("%Y%m%d")
    index = {(REF - timedelta(days=i)).strftime("%Y%m%d"): 2700 - i * 2 for i in range(70)}
    ctx = {m: {"ref": ref, "close": 2650.42 if m == "KOSPI" else 880.15, "chg": 0.42, "light": "🟢",
               "above_ma20": True, "index": index} for m in main.MARKETS}
    stocks = [kis.Stock("900001", "데모전자", "KOSPI", 25000, 100, 13.2, True, 1e11, "13"),
              kis.Stock("900002", "데모중공업", "KOSPI", 60000, 100, 9.1, True, 1e11, "17"),
              kis.Stock("900003", "데모화학", "KOSPI", 18000, 100, 6.4, True, 1e11, "08")]
    picks = []
    for st, rows in zip(stocks, (_pullback(), _breakout(), _trend())):
        p = sc.analyze(st, rows, index)
        if p:
            p.news = [(f"{st.name}, 4분기 실적 개선 기대에 목표가 상향", "")]
            picks.append(sc.score(p))
    picks.sort(key=lambda p: p.score, reverse=True)
    sigs = [x for p in picks if (x := sg.evaluate(p.stock, p.rows, "🟢", p.rs20, p.flags))]
    stats = {"scanned": 573, "errors": 0, "stale": 0, "dq": 0, "passed": {m: len(picks) for m in main.MARKETS}}
    hist = {"seen": {"900001": {"count": 3}}, "window": 10, "exits": ["데모바이오"]}
    macro = "🌎 나스닥 +0.85% | S&P500 +0.41% | 필라델피아반도체 +1.62% | 원/달러 1,338.5"
    day = {"date": ref, "markets": {"KOSPI": {**{k: ctx["KOSPI"][k] for k in ("close", "chg", "light")},
                                              "picks": [site.record(p, i) for i, p in enumerate(picks, 1)],
                                              "signals": [site.signal_record(x) for x in sigs]}}}
    morning = main.morning_message("KOSPI", day, macro, url, cap)
    close = main.message("close", "KOSPI", picks, stats, ctx, False, ref, "", hist, url, sigs, cap)
    brk = next((x for x in sigs if x.setup == "B"), None)
    intra = {m: [] for m in main.MARKETS}
    if brk:
        brk.notes.append("잠정 외인 +18억 · 기관(금투 포함) +9억 · 거래량은 하루치 환산 예상")
        intra["KOSPI"] = [brk]
    intraday = main.intraday_message(intra, datetime(2026, 10, 12, 14, 36), url, cap)
    note = f"\n(데모 자본 {cap:,.0f}원 가정)" if cap else ""
    return [TAG + "<b>① 장 전 07:00</b>\n\n" + morning + note,
            TAG + "<b>② 장중 14:35</b> — 신호 있을 때만 옴\n\n" + intraday + note,
            TAG + "<b>③ 장 마감 18:07</b> — 코스닥도 같은 형식으로 1통 더\n\n" + close + note]
