"""국내주식 수급 추천 실행 → 텔레그램.

    python -m kstock.main --session morning      # 장 전 (전일 확정 수급 + 밤사이 뉴스 + 미국 시장)  ※ pre 도 가능
    python -m kstock.main --session intraday     # 장중 14:35 (전일 후보만 잠정 수급으로 재판정, 신호 있을 때만 전송)
    python -m kstock.main --session close        # 장 마감 후
    python -m kstock.main --session close --dry-run   # 전송 없이 출력만
    python -m kstock.main --send work/kstock-20261012-close-msg.json   # 저장된 메시지만 전송
    python -m kstock.main --rebuild-site       # 분석 없이 웹페이지만 다시 생성 (양식 변경 반영)
    python -m kstock.main --demo               # 합성 데이터로 메시지 양식 미리보기 (Actions: demo)
    python -m kstock.main --failure https://github.com/.../actions/runs/123

환경변수: KIS_APP_KEY, KIS_APP_SECRET (필수) · TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
          (국장 전용 KSTOCK_TELEGRAM_BOT_TOKEN, KSTOCK_TELEGRAM_CHAT_ID 우선) · DART_API_KEY (선택)
          KSTOCK_CAPITAL (선택, 계좌 자본 — 있으면 진입 신호에 매수 수량까지 표시)
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import os
import sys
from collections import Counter
from dataclasses import fields
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import extras, kis as kis_mod, score as sc, signals as sg, site

log = logging.getLogger("kstock")
KST = timezone(timedelta(hours=9))
TOP = 5                        # 시장별 추천 수
SHORTLIST = 10                 # 시장별 뉴스·공시 확인 대상 (예비 점수 상위)
MARKETS = ("KOSPI", "KOSDAQ")
INDEX_CODE = {"KOSPI": "0001", "KOSDAQ": "1001"}
MIN_PREV_VALUE = 10 * sc.EOK   # 전일 거래대금 사전 필터 (API 호출 수 절감)
WORKERS = 8                    # 동시 조회 수 (초당 한도는 KIS 클래스가 지킴)
SESSION_LABEL = {"morning": "장 전 브리핑", "intraday": "장중 예비", "close": "장 마감"}
MAX_SIGNALS = 3                # 보유 한도(2~3종목)에 맞춰 신호도 시장별 최대 3개
# 해외 지수·환율 코드 (2026-10 실조회 확인). 조회 실패 항목은 메시지에서 빠짐
MACRO = (("나스닥", "N", "COMP"), ("S&P500", "N", "SPX"), ("필라델피아반도체", "N", "SOX"), ("원/달러", "X", "FX@KRW"))
WORK = Path("work")            # 후보 기록 CSV·메시지 원문 (Actions 아티팩트로 보관, 레포 커밋 안 함)
ROWS = WORK / "rows.json"      # 장 마감 때 조건 통과 종목 일봉 → Actions 캐시 → 다음 날 장중에 사용
ROW_KEYS = ("stck_bsop_date", "stck_clpr", "stck_oprc", "stck_hgpr", "stck_lwpr", "acml_vol", "prdy_ctrt",
            "frgn_ntby_qty", "orgn_ntby_qty", "scrt_ntby_qty")
SITE = Path("docs/kstock")     # 웹페이지 (장 마감 + 장중 예비 상자만 갱신·커밋)


def candidates(stocks: list[kis_mod.Stock]) -> list[kis_mod.Stock]:
    return [s for s in stocks if s.ok and s.mcap >= sc.MIN_MCAP[s.market] and s.prev_value >= MIN_PREV_VALUE]


def market_context(client, session: str, today: str) -> dict[str, dict]:
    """시장별 지수 {날짜: 종가}, 기준일(지수 최신 거래일), 신호등."""
    start = (datetime.strptime(today, "%Y%m%d") - timedelta(days=130)).strftime("%Y%m%d")
    ctx = {}
    for m in MARKETS:
        idx = client.index_daily(INDEX_CODE[m], start, today)
        if session == "morning":
            idx = {d: v for d, v in idx.items() if d < today}
        if len(idx) < 21:
            raise RuntimeError(f"{m} 지수 데이터 부족 ({len(idx)}일)")
        dates = sorted(idx, reverse=True)
        c = [idx[d] for d in dates]
        ma20, ma60 = sum(c[:20]) / 20, sum(c[:60]) / len(c[:60])
        above = (c[0] > ma20) + (c[0] > ma60)
        ctx[m] = {"index": idx, "ref": dates[0], "close": c[0], "chg": (c[0] / c[1] - 1) * 100,
                  "above_ma20": c[0] > ma20, "light": "🟢🟡🔴"[2 - above]}
    return ctx


def run(session: str, client, stocks: list[kis_mod.Stock], ctx: dict, dart=None, news=extras.news,
        today: str | None = None) -> tuple[dict, list[sc.Pick], dict, dict]:
    """({시장: 추천}, 조건 통과 전체, {scanned, errors, stale, dq, passed: {시장: 수}}, {시장: 진입 신호})"""
    today = today or datetime.now(KST).strftime("%Y%m%d")
    pool = candidates(stocks)

    def one(s: kis_mod.Stock):
        rows = client.investor_daily(s.code, today)
        if session == "morning":  # 장 전에는 오늘 행(미확정)을 쓰지 않는다
            rows = [r for r in rows if r["stck_bsop_date"] < today]
        if not rows or rows[0]["stck_bsop_date"] != ctx[s.market]["ref"]:
            return "stale"  # 지수 기준일과 다른 데이터(정지·미갱신)는 점수 내지 않음
        p = sc.analyze(s, rows, ctx[s.market]["index"])
        return sc.score(p) if p else None

    def safe(s: kis_mod.Stock):
        try:
            return one(s)
        except Exception as e:  # 한 종목 실패가 전체를 멈추지 않게
            log.warning("%s %s: %s", s.code, s.name, e)
            return e

    log.info("후보 %d종목 조회 시작", len(pool))
    with ThreadPoolExecutor(WORKERS) as ex:
        results = list(ex.map(safe, pool))
    errors = sum(isinstance(r, Exception) for r in results)
    stale = results.count("stale")
    passed = [r for r in results if isinstance(r, sc.Pick)]
    log.info("조회 완료: 실패 %d · 기준일 불일치 %d · 조건 통과 %d", errors, stale, len(passed))
    if pool and errors > len(pool) / 2:
        raise RuntimeError(f"KIS 조회 실패 {errors}/{len(pool)}")

    passed.sort(key=lambda p: p.score, reverse=True)
    picks, dq = {}, 0
    for m in MARKETS:
        checked = []
        for p in [p for p in passed if p.stock.market == m][:SHORTLIST]:
            if dart and any(extras.is_bad(t) for t in dart.recent(p.stock.code)):
                log.info("공시 악재로 제외: %s", p.stock.name)
                dq += 1
                continue
            p.news = news(p.stock.name)
            p.flags = [t for t, _ in p.news if extras.is_bad(t)]
            checked.append(sc.score(p))
        picks[m] = sorted(checked, key=lambda p: p.score, reverse=True)[:TOP]
    stats = {"scanned": len(pool), "errors": errors, "stale": stale, "dq": dq,
             "passed": {m: sum(p.stock.market == m for p in passed) for m in MARKETS}}
    return picks, passed, stats, find_signals(passed, ctx, dart, news)


def flow_note(p: sc.Pick) -> str:
    return f"5일 외인 {_eok(p.frgn5)} · 기관 {_eok(p.orgn5)}" + (f" · 쌍끌이 {p.streak}일" if p.streak else "")


def find_signals(passed: list[sc.Pick], ctx: dict, dart=None, news=extras.news) -> dict[str, list[sg.Signal]]:
    """조건 통과 종목 중 셋업 A/B 충족 종목. 뉴스·공시를 아직 안 본 종목은 확인 후 다시 판정."""
    out = {m: [] for m in MARKETS}
    for p in passed:
        light = ctx[p.stock.market]["light"]
        if not sg.evaluate(p.stock, p.rows, light, p.rs20, p.flags):
            continue
        if not p.news:
            if dart and any(extras.is_bad(t) for t in dart.recent(p.stock.code)):
                continue
            p.news = news(p.stock.name)
            p.flags = [t for t, _ in p.news if extras.is_bad(t)]
        s = sg.evaluate(p.stock, p.rows, light, p.rs20, p.flags)
        if s:
            s.notes.insert(0, flow_note(p))
            out[p.stock.market].append((p.score, s))
    return {m: [s for _, s in sorted(v, key=lambda x: (x[1].grade, -x[0]))][:MAX_SIGNALS] for m, v in out.items()}


def save_csv(path: Path, session: str, passed: list[sc.Pick], picks: dict[str, list[sc.Pick]]) -> None:
    """백테스트용: 조건 통과 전체 후보와 지표·점수를 날짜별로 남긴다. top_rank = 텔레그램 TOP 순위(아니면 빈칸)."""
    rank = {id(p): i for ps in picks.values() for i, p in enumerate(ps, 1)}
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["date", "session", "market", "top_rank", "code", "name", "score", "close", "chg", "mcap", "frgn5", "orgn5",
            "quality5", "flow20", "streak", "vol_ratio", "clv", "upper_wick", "disparity", "ext_atr", "rs20",
            "news_flags", *sorted({k for p in passed for k in p.parts})]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        for p in passed:
            w.writerow({"session": session, "market": p.stock.market, "top_rank": rank.get(id(p), ""), "code": p.stock.code, "name": p.stock.name,
                        "mcap": p.stock.mcap, "news_flags": len(p.flags), **p.parts,
                        **{k: getattr(p, k) for k in cols if hasattr(p, k) and k != "stock"}})


def attach_opinions(client, picks: dict[str, list[sc.Pick]], today: str) -> None:
    """TOP 종목에 최근 3개월 증권사 투자의견·목표가를 붙인다 (웹페이지용, 실패해도 진행)."""
    start = (datetime.strptime(today, "%Y%m%d") - timedelta(days=90)).strftime("%Y%m%d")
    for p in (p for ps in picks.values() for p in ps):
        try:
            p.opinions = client.invest_opinion(p.stock.code, start, today)
        except Exception as e:
            log.warning("투자의견 실패 %s: %s", p.stock.name, e)
    sample = next((p.opinions[0] for ps in picks.values() for p in ps if p.opinions), None)
    log.info("투자의견 응답 필드: %s", sorted(sample) if sample else "없음")  # 증권사명 필드 확인용


def macro_line(client, today: str) -> str:
    start = (datetime.strptime(today, "%Y%m%d") - timedelta(days=10)).strftime("%Y%m%d")
    out = []
    for name, mkt, code in MACRO:
        try:
            s = client.overseas_daily(mkt, code, start, today)
            d = sorted(s, reverse=True)
            fmt = f"{s[d[0]]:,.1f}" if mkt == "X" else f"{(s[d[0]] / s[d[1]] - 1) * 100:+.2f}%"
            out.append(f"{name} {fmt}")
        except Exception as e:
            log.warning("해외 시세 실패 %s(%s): %s", name, code, e)
    return "🌎 " + " | ".join(out) if out else ""


def _eok(x: float) -> str:
    return f"{x / sc.EOK:+,.0f}억"


def capital() -> float:
    """계좌 자본 (GitHub Secret KSTOCK_CAPITAL, 숫자만). 없으면 비중(%)만 안내."""
    digits = "".join(ch for ch in os.environ.get("KSTOCK_CAPITAL", "") if ch.isdigit())
    return float(digits) if digits else 0.0


def signal_lines(sigs: list[sg.Signal], session: str, cap: float = 0.0) -> list[str]:
    """진입 신호 블록. 신호가 없으면 장 마감만 '없음'을 알린다 (장중은 빈 목록)."""
    e = html.escape
    title = {"close": "🎯 내일 진입 후보", "morning": "🎯 오늘 진입 계획 (어제 종가 기준)",
             "intraday": "🛒 15:20 종가 동시호가 매수 검토 — 놓치면 15:40 시간외 종가"}[session]
    if not sigs:
        return [f"<b>{title.split(' —')[0]}</b>: 없음 — 쉬는 것도 매매", ""] if session != "intraday" else []
    out = [f"<b>{title}</b>"]
    for x in sigs:
        qty = f" · 약 {x.shares(cap):,}주" if cap and x.shares(cap) else ""
        out.append(f"{x.grade}급 {'🅐눌림' if x.setup == 'A' else '🅑돌파'} <b>{e(x.name)}</b> <code>{x.code}</code>")
        price = (f"현재가 {x.entry:,.0f}" if session == "intraday"
                 else f"매수 {x.entry:,.0f}~{x.buy_max:,.0f}")
        out.append(f"    {price} · 손절 {x.stop:,.0f} (−{x.stop_pct:.1f}%) · 1R {x.r1:,.0f} / 2R {x.r2:,.0f}")
        out.append(f"    비중 계좌의 {x.weight:.0f}% (위험 {x.risk_pct:g}%){qty}"
                   + (f" · 시초가 {x.gap_skip:,.0f}↑면 패스" if session != "intraday" else ""))
        out += [f"    {e(n)}" for n in x.notes]
    out.append("※ 손절가는 키움 자동감시주문으로 미리 걸어두기")
    return out + [""]


def _rs20(rows: list[dict], index: dict[str, float]) -> float | None:
    if len(rows) <= 20 or rows[0]["stck_bsop_date"] not in index or rows[20]["stck_bsop_date"] not in index:
        return None
    c0, c20 = sc._f(rows[0], "stck_clpr"), sc._f(rows[20], "stck_clpr")
    i0, i20 = index[rows[0]["stck_bsop_date"]], index[rows[20]["stck_bsop_date"]]
    return ((c0 / c20) - (i0 / i20)) * 100 if c20 and i20 else None


def save_rows(path: Path, passed: list[sc.Pick]) -> None:
    """장 마감 일봉 (장중에 다시 받을 수 없어서 — KIS 종목별 투자자 일별 API는 00:00~15:40 조회 불가)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({p.stock.code: [{k: r.get(k) for k in ROW_KEYS} for r in p.rows[:40]]
                                for p in passed}), encoding="utf-8")


def run_intraday(client, ctx: dict, watch: list[dict], rows: dict[str, list], today: str,
                 now: datetime) -> dict[str, list[sg.Signal]]:
    """전일 장 마감 조건 통과 종목만: 캐시된 일봉 + 현재가 + 외인·기관 잠정 가집계로 '오늘 종가 기준' 셋업을 미리 판정.
    당일 거래량은 경과 시간으로 하루치 환산(예상)."""
    frac = min(1.0, max(0.1, (now.hour * 60 + now.minute - 540) / 390))  # 09:00~15:30 = 390분

    def one(w: dict):
        hist = [r for r in rows.get(w["code"], []) if r["stck_bsop_date"] < today]
        if not hist:
            return None
        q, est = client.price(w["code"]), client.investor_estimate(w["code"])
        if w is watch[0]:
            log.info("장중 응답 확인 — 현재가 %s · 가집계 %s", q.get("stck_prpr"), est)
        if not hist or not sc._f(q, "stck_prpr"):
            return None
        row = {"stck_bsop_date": today, "stck_clpr": q["stck_prpr"], "stck_oprc": q.get("stck_oprc"),
               "stck_hgpr": q.get("stck_hgpr"), "stck_lwpr": q.get("stck_lwpr"), "acml_vol": q.get("acml_vol"),
               "prdy_ctrt": q.get("prdy_ctrt"), "frgn_ntby_qty": est.get("frgn_fake_ntby_qty", 0),
               "orgn_ntby_qty": est.get("orgn_fake_ntby_qty", 0), "scrt_ntby_qty": 0}  # 잠정 기관은 금투 포함
        stock = kis_mod.Stock(w["code"], w["name"], w["market"], 0, 0, 0, True, 0, w.get("sector", ""))
        x = sg.evaluate(stock, [row] + hist, ctx[w["market"]]["light"], _rs20(hist, ctx[w["market"]]["index"]),
                        [], vol_scale=1 / frac)
        if x:
            price = sc._f(q, "stck_prpr")
            x.notes.append(f"잠정 외인 {sc._f(row, 'frgn_ntby_qty') * price / sc.EOK:+,.0f}억 · "
                           f"기관(금투 포함) {sc._f(row, 'orgn_ntby_qty') * price / sc.EOK:+,.0f}억 · "
                           f"거래량은 하루치 환산 예상")
        return x

    def safe(w):
        try:
            return one(w)
        except Exception as e:
            log.warning("장중 %s: %s", w["code"], e)
            return None

    with ThreadPoolExecutor(WORKERS) as ex:
        found = [x for x in ex.map(safe, watch) if x]
    return {m: sorted([x for x in found if x.market == m], key=lambda x: x.grade)[:MAX_SIGNALS] for m in MARKETS}


def message(session: str, market: str, picks: list[sc.Pick], stats: dict, ctx: dict, dart_on: bool,
            today: str, macro: str = "", hist: dict | None = None, url: str = "",
            sigs: list | None = None, cap: float = 0.0) -> str:
    """텔레그램 HTML 메시지: 순위와 한 줄 코멘트만. 근거(차트·수급표·목표가)는 웹페이지."""
    e = html.escape
    c = ctx[market]
    ref = c["ref"]
    hist = hist or {"seen": {}, "window": 0, "exits": []}
    lines = [f"<b>📈 {market} TOP {TOP}</b> — {ref[4:6]}/{ref[6:]} {SESSION_LABEL[session]} "
             f"{c['light']} {c['close']:,.2f} ({c['chg']:+.2f}%)"]
    if macro:
        lines.append(e(macro))
    if c["light"] == "🔴":
        lines.append("🔴 시장 약세(20·60일선 아래) — 신규 진입은 관찰 위주")
    if session == "close" and ref != today:
        lines.append(f"⚠️ 당일 데이터 미갱신 — {ref[4:6]}/{ref[6:]} 데이터로 분석")
    if session == "morning":
        lines.append("※ 전일 종가 기준 — 시초가 갭상승 시 추격 주의")
    lines.append("")
    if sigs is not None:
        lines += signal_lines(sigs, session, cap)
    if not picks:
        lines.append("조건(외인·기관 동반 순매수 + 유동성 + 과열 제외)을 충족한 종목이 없습니다.")
    if picks:
        lines.append(LIGHT_LEGEND)
    codes = {x.code for x in sigs or []}
    for i, p in enumerate(picks, 1):
        seen = hist["seen"].get(p.stock.code)
        lines += pick_lines(site.record(p, i), p.stock.code in codes,
                            f"🔁 {seen['count']}/{hist['window']}일" if seen and seen["count"] > 1 else "🆕")
    if 0 < len(picks) < TOP:
        lines.append(f"(조건 충족 {len(picks)}종목 — 기준 미달로 채우지 않음)")
    top_sector = Counter(p.stock.sector for p in picks if p.stock.sector).most_common(1)
    if top_sector and top_sector[0][1] >= 3:
        lines.append(f"⚠️ 같은 업종 {top_sector[0][1]}종목 집중 — 분산 유의")
    if hist["exits"]:
        lines.append("🚪 이탈: " + e(", ".join(hist["exits"])))
    if url:
        lines.append(f'\n<a href="{e(url)}#{market.lower()}">📊 상세 보기 — 차트·수급표·목표가</a>')
    status = f"스캔 {stats['scanned']} · 통과 {stats['passed'][market]}"
    for key, label in (("errors", "조회 실패"), ("stale", "기준일 불일치"), ("dq", "공시 악재 제외")):
        if stats[key]:
            status += f" · {label} {stats[key]}"
    lines.append(status + ("" if dart_on else " · DART 미연결") + "\n※ 관찰 후보 · 투자 권유 아님 · 코드를 누르면 복사")
    return "\n".join(lines)[:4000]


LIGHT_LEGEND = "<i>종목 신호등 🟢 진입 자리 · 🟡 관찰 · 🔴 주의(악재·과열·윗꼬리)</i>"


def stock_light(rec: dict, signaled: bool) -> str:
    """종목 신호등: 시장 신호등과 별개로 '지금 이 종목을 어떻게 볼지'."""
    if rec["flags"] or rec["ext_atr"] > 3.5 or rec["upper_wick"] >= 0.5:
        return "🔴"
    return "🟢" if signaled else "🟡"


def pick_lines(rec: dict, signaled: bool, seen: str = "") -> list[str]:
    """TOP 종목 2줄 (rec = site.record 형식, 금액은 억 단위)."""
    tags = [f"외인 {rec['frgn5']:+,.0f}억", f"기관 {rec['orgn5']:+,.0f}억"]
    if rec["streak"]:
        tags.append(f"쌍끌이 {rec['streak']}일")
    tags.append(f"거래량 {rec['vol_ratio']:.1f}배")
    if rec["clv"] >= 0.7:
        tags.append("고가 마감")
    elif rec["upper_wick"] >= 0.5:
        tags.append("윗꼬리")
    if seen:
        tags.append(seen)
    if rec["ext_atr"] > 3.5:
        tags.append("⚠️과열")
    if rec["flags"]:
        tags.append("⚠️악재뉴스")
    return [f"{stock_light(rec, signaled)} {rec['rank']}) <b>{html.escape(rec['name'])}</b> "
            f"<code>{rec['code']}</code> {rec['score']}점",
            "    " + " · ".join(tags)]


def as_signal(rec: dict) -> sg.Signal:
    return sg.Signal(**{f.name: rec[f.name] for f in fields(sg.Signal)})


def morning_message(market: str, day: dict, macro: str, url: str, cap: float) -> str:
    """장 전: 전일 장 마감 기록(진입 후보·TOP)을 다시 보여주고 미국 시장·갭 주의만 더한다.
    (장 전에는 새 수급 데이터가 없고, KIS 종목별 투자자 일별 API도 00:00~15:40 조회 불가)"""
    e = html.escape
    mk, ref = day["markets"][market], day["date"]
    lines = [f"<b>🌅 {market} 장 전 브리핑</b> — {ref[4:6]}/{ref[6:]} 장 마감 기준 "
             f"{mk['light']} {mk['close']:,.2f} ({mk['chg']:+.2f}%)"]
    if macro:
        lines.append(e(macro))
    if mk["light"] == "🔴":
        lines.append("🔴 시장 약세(20·60일선 아래) — 신규 진입은 관찰 위주")
    lines += ["※ 전일 종가 기준 — 시초가 갭상승 시 추격 주의", ""]
    lines += signal_lines([as_signal(r) for r in mk.get("signals", [])], "morning", cap)
    if mk["picks"]:
        codes = {r["code"] for r in mk.get("signals", [])}
        lines += [f"<b>어제 TOP {len(mk['picks'])}</b>", LIGHT_LEGEND]
        for r in mk["picks"]:
            lines += pick_lines(r, r["code"] in codes)
        lines.append("")
    if url:
        lines.append(f'<a href="{e(url)}#{market.lower()}">📊 상세 보기 — 차트·수급표·목표가</a>')
    lines.append("※ 관찰 후보 · 투자 권유 아님 · 코드를 누르면 복사")
    return "\n".join(lines)[:4000]


def save_signals(path: Path, sigs: dict[str, list[sg.Signal]]) -> None:
    """신호 기록 (사후 검증용 아티팩트): 신호가·손절·셋업·등급."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([site.signal_record(x) for m in MARKETS for x in sigs[m]], ensure_ascii=False),
                    encoding="utf-8")


def intraday_message(sigs: dict[str, list[sg.Signal]], now: datetime, url: str, cap: float) -> str:
    lines = [f"<b>⏱ 장중 예비 신호 {now:%H:%M}</b> — 잠정 수급·예상 거래량 기준, 장 마감 때 바뀔 수 있음", ""]
    for m in MARKETS:
        if sigs[m]:
            lines += [f"<b>{m}</b>"] + signal_lines(sigs[m], "intraday", cap)
    if url:
        lines.append(f'<a href="{html.escape(url)}">📊 웹페이지 장중 상자</a>')
    lines.append("※ 관찰 후보 · 투자 권유 아님 · 코드를 누르면 복사")
    return "\n".join(lines)[:4000]


def site_url() -> str:
    """GitHub Pages 주소 (Actions 의 GITHUB_REPOSITORY = owner/repo)."""
    owner, _, repo = os.environ.get("GITHUB_REPOSITORY", "/").partition("/")
    return f"https://{owner.lower()}.github.io/{repo}/kstock/" if owner and repo else ""


def send_telegram(text: str) -> bool:
    # 국장 전용 봇/방(KSTOCK_*)이 있으면 그쪽으로, 없으면 COT와 같은 봇/방으로
    token = os.environ.get("KSTOCK_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("KSTOCK_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        log.warning("텔레그램 미설정 — 전송 생략")
        return False
    resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20, json={
        "chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
    resp.raise_for_status()
    return True


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", choices=[*SESSION_LABEL, "pre"], default="close")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="휴장일에도 실행")
    ap.add_argument("--failure", metavar="RUN_URL")
    ap.add_argument("--send", metavar="MSG_JSON", help="저장된 메시지만 전송 (웹페이지 배포 뒤에 보낼 때)")
    ap.add_argument("--rebuild-site", action="store_true", help="분석 없이 저장된 데이터로 웹페이지만 다시 생성")
    ap.add_argument("--demo", action="store_true", help="합성 데이터로 장 전·장중·장 마감 메시지 양식 미리보기")
    a = ap.parse_args(argv)
    if a.demo:
        from . import demo
        texts = demo.build(sys.modules[__name__], os.environ.get("KSTOCK_SITE_URL") or site_url(),
                           capital() or 10_000_000)
        WORK.mkdir(exist_ok=True)
        (WORK / "kstock-demo-demo-msg.json").write_text(json.dumps(texts, ensure_ascii=False), encoding="utf-8")
        for text in texts:
            print(text, "\n")
            if not a.dry_run:
                send_telegram(text)
        return 0
    if a.rebuild_site:
        site.build(SITE, os.environ.get("KSTOCK_SITE_URL") or site_url())
        return 0
    if a.send:
        for text in json.loads(Path(a.send).read_text(encoding="utf-8")):
            send_telegram(text)
        return 0
    session = "morning" if a.session == "pre" else a.session

    if a.failure:
        send_telegram(f"⚠️ 국내주식 추천 실패\n실행 로그를 확인해 주세요.\n🔗 {html.escape(a.failure)}")
        return 0

    client = kis_mod.KIS()
    now = datetime.now(KST)
    today = now.strftime("%Y%m%d")
    if not a.force and not client.is_open(today):
        log.info("휴장일 %s — 건너뜀", today)
        return 0
    url = os.environ.get("KSTOCK_SITE_URL") or site_url()
    cap = capital()
    WORK.mkdir(exist_ok=True)

    if session == "intraday":  # 전일 장 마감 조건 통과 종목만 장중 재판정 (지수·신호등은 전일 확정 기준)
        ctx = market_context(client, "morning", today)
        days = site.load_days(SITE)
        watch = days[0].get("watch", []) if days else []
        rows = json.loads(ROWS.read_text(encoding="utf-8")) if ROWS.exists() else {}
        if watch and not rows:
            log.warning("장 마감 일봉 캐시(%s)가 없어 장중 판정을 건너뜀", ROWS)
        sigs = run_intraday(client, ctx, watch, rows, today, now)
        site.save_intraday(SITE, today, now.strftime("%H:%M"), sigs)
        site.build(SITE, url)
        save_signals(WORK / f"kstock-{today}-intraday-signals.json", sigs)
        found = [x for m in MARKETS for x in sigs[m]]
        log.info("장중 대상 %d종목(캐시 %d) · 신호 %d", len(watch), len(rows), len(found))
        texts = [intraday_message(sigs, now, url, cap)] if found else []  # 신호 없으면 보내지 않음
    elif session == "morning":  # 전일 장 마감 기록 + 미국 시장 (KIS 일별 투자자 API는 이 시간 조회 불가)
        days = site.load_days(SITE)
        if not days:
            log.warning("장 마감 기록이 없어 장 전 브리핑을 건너뜀")
            return 0
        macro = macro_line(client, today)
        texts = [morning_message(m, days[0], macro, url, cap) for m in MARKETS]
    else:
        ctx = market_context(client, session, today)
        dart = extras.Dart.from_env()
        picks, passed, stats, sigs = run(session, client, kis_mod.load_master(), ctx, dart=dart, today=today)
        save_csv(WORK / f"kstock-{today}-{session}.csv", session, passed, picks)
        save_signals(WORK / f"kstock-{today}-{session}-signals.json", sigs)
        save_rows(ROWS, passed)
        attach_opinions(client, picks, today)
        site.save_day(SITE, ctx[MARKETS[0]]["ref"], picks, ctx, stats, dart is not None, sigs, passed)
        site.build(SITE, url)
        days = site.load_days(SITE)
        texts = [message(session, m, picks[m], stats, ctx, dart is not None, today, "",
                         site.history(days, m) if days else None, url, sigs[m], cap) for m in MARKETS]
    if texts:
        (WORK / f"kstock-{today}-{session}-msg.json").write_text(json.dumps(texts, ensure_ascii=False),
                                                                encoding="utf-8")
    for text in texts:
        print(text, "\n")
        if not a.dry_run:
            send_telegram(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
