"""국내주식 수급 추천 실행 → 텔레그램.

    python -m kstock.main --session morning      # 장 전 (전일 확정 수급 + 밤사이 뉴스 + 미국 시장)  ※ pre 도 가능
    python -m kstock.main --session close        # 장 마감 후
    python -m kstock.main --session close --dry-run   # 전송 없이 출력만
    python -m kstock.main --failure https://github.com/.../actions/runs/123

환경변수: KIS_APP_KEY, KIS_APP_SECRET (필수) · TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
          (국장 전용 KSTOCK_TELEGRAM_BOT_TOKEN, KSTOCK_TELEGRAM_CHAT_ID 우선) · DART_API_KEY (선택)
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import extras, kis as kis_mod, score as sc

log = logging.getLogger("kstock")
KST = timezone(timedelta(hours=9))
TOP = 5                        # 시장별 추천 수
SHORTLIST = 10                 # 시장별 뉴스·공시 확인 대상 (예비 점수 상위)
MARKETS = ("KOSPI", "KOSDAQ")
INDEX_CODE = {"KOSPI": "0001", "KOSDAQ": "1001"}
MIN_PREV_VALUE = 10 * sc.EOK   # 전일 거래대금 사전 필터 (API 호출 수 절감)
WORKERS = 8                    # 동시 조회 수 (초당 한도는 KIS 클래스가 지킴)
SESSION_LABEL = {"morning": "장 전 브리핑", "close": "장 마감"}
# 해외 지수·환율 코드 (2026-10 실조회 확인). 조회 실패 항목은 메시지에서 빠짐
MACRO = (("나스닥", "N", "COMP"), ("S&P500", "N", "SPX"), ("필라델피아반도체", "N", "SOX"), ("원/달러", "X", "FX@KRW"))
WORK = Path("work")            # 후보 기록 CSV (Actions 아티팩트로 보관, 레포 커밋 안 함)


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
        today: str | None = None) -> tuple[dict[str, list[sc.Pick]], list[sc.Pick], dict]:
    """({시장: 추천}, 조건 통과 전체, {scanned, errors, stale, dq, passed: {시장: 수}})"""
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
    return picks, passed, stats


def save_csv(path: Path, session: str, passed: list[sc.Pick]) -> None:
    """백테스트용: 조건 통과 전체 후보와 지표·점수를 날짜별로 남긴다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["date", "session", "market", "code", "name", "score", "close", "chg", "mcap", "frgn5", "orgn5",
            "quality5", "flow20", "streak", "vol_ratio", "clv", "upper_wick", "disparity", "ext_atr", "rs20",
            "news_flags", *sorted({k for p in passed for k in p.parts})]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        for p in passed:
            w.writerow({"session": session, "market": p.stock.market, "code": p.stock.code, "name": p.stock.name,
                        "mcap": p.stock.mcap, "news_flags": len(p.flags), **p.parts,
                        **{k: getattr(p, k) for k in cols if hasattr(p, k) and k != "stock"}})


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


def message(session: str, market: str, picks: list[sc.Pick], stats: dict, ctx: dict, dart_on: bool,
            today: str, macro: str = "") -> str:
    c = ctx[market]
    ref = c["ref"]
    lines = [f"📈 {market} 수급 TOP {TOP} — {SESSION_LABEL[session]} {ref[4:6]}/{ref[6:]} 기준"]
    if macro:
        lines.append(macro)
    lines.append(f"{c['light']} {market} {c['close']:,.2f} ({c['chg']:+.2f}%) · 20일선 {'위' if c['above_ma20'] else '아래'}"
                 + (" → 시장 약세, 신규 진입은 관찰 위주" if c["light"] == "🔴" else ""))
    if session == "close" and ref != today:
        lines.append(f"⚠️ 당일 데이터 미갱신 — {ref[4:6]}/{ref[6:]} 데이터로 분석")
    status = f"스캔 {stats['scanned']} · 통과 {stats['passed'][market]}"
    for key, label in (("errors", "조회 실패"), ("stale", "기준일 불일치"), ("dq", "공시 악재 제외")):
        if stats[key]:
            status += f" · {label} {stats[key]}"
    lines.append(status + ("" if dart_on else " · ⚠️ DART 미연결(공시 미검증)"))
    if session == "morning":
        lines.append("※ 전일 종가 기준 신호 — 시초가 갭상승 시 추격 주의")
    if len(picks) < TOP:
        lines.append(f"\n조건 충족 {len(picks)}종목 (기준 미달 종목으로 채우지 않음)")

    for i, p in enumerate(picks, 1):
        s = p.stock
        candle = "고가 마감" if p.clv >= 0.7 else "윗꼬리 매물" if p.upper_wick >= 0.5 else "중간 마감"
        trend = "·".join(t for t, ok in (("20일선↑", p.above_ma20), ("정배열", p.ma_aligned),
                                          ("고점근접", p.near_high)) if ok) or "추세 약함"
        rs = "상대강도 미검증" if p.rs20 is None else f"지수대비 {p.rs20:+.1f}%p(20일)"
        flow = (f"쌍끌이 {p.streak}일" if p.streak else "당일 쌍끌이 아님") + f" · 외인 {p.frgn_streak}일·연기금 {p.fund_streak}일 연속"
        lines += [
            "",
            f"{i}) {s.name} ({s.code}) {p.score}점",
            f"   종가 {p.close:,.0f} ({p.chg:+.1f}%) · 시총 {s.mcap:,.0f}억",
            f"   5일 외인 {_eok(p.frgn5)} · 기관(금투제외) {_eok(p.orgn5)}",
            f"   {flow}",
            f"   거래량 {p.vol_ratio:.1f}배 · {candle} · {trend}",
            f"   {rs} · 20일선 이격 {p.disparity:+.1f}% (ATR {p.ext_atr:.1f}배)",
            f"   영업이익 {'흑자' if s.op_profit > 0 else '적자'} · ROE {s.roe:g}",
            "   " + " · ".join(f"{k} {v}" for k, v in p.parts.items()),
        ]
        if p.ext_atr > 3.5:
            lines.append("   ⚠️ 단기 과열 — 눌림 확인 후 접근")
        lines += [f"   {'⚠️' if t in p.flags else '📰'} {t}" for t, _ in p.news[:2]]
        # PC용 finance.naver.com 주소는 모바일에서 증권 홈으로 튕겨서 모바일 종목 페이지로 연결
        lines.append(f"   🔗 https://m.stock.naver.com/domestic/stock/{s.code}/total")
    top_sector = Counter(p.stock.sector for p in picks if p.stock.sector).most_common(1)
    if top_sector and top_sector[0][1] >= 3:
        lines.append(f"\n⚠️ 같은 업종 {top_sector[0][1]}종목 집중 — 분산 유의")
    lines.append("\n※ 관찰 후보 (매수 신호 아님) · 배점은 검증 전 가설")
    return "\n".join(lines)[:4000]


def send_telegram(text: str) -> bool:
    # 국장 전용 봇/방(KSTOCK_*)이 있으면 그쪽으로, 없으면 COT와 같은 봇/방으로
    token = os.environ.get("KSTOCK_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("KSTOCK_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        log.warning("텔레그램 미설정 — 전송 생략")
        return False
    resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20, json={
        "chat_id": chat, "text": text, "disable_web_page_preview": True})
    resp.raise_for_status()
    return True


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", choices=[*SESSION_LABEL, "pre"], default="close")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="휴장일에도 실행")
    ap.add_argument("--failure", metavar="RUN_URL")
    a = ap.parse_args(argv)
    session = "morning" if a.session == "pre" else a.session

    if a.failure:
        send_telegram(f"⚠️ 국내주식 추천 실패\n실행 로그를 확인해 주세요.\n🔗 {a.failure}")
        return 0

    client = kis_mod.KIS()
    today = datetime.now(KST).strftime("%Y%m%d")
    if not a.force and not client.is_open(today):
        log.info("휴장일 %s — 건너뜀", today)
        return 0
    ctx = market_context(client, session, today)
    dart = extras.Dart.from_env()
    picks, passed, stats = run(session, client, kis_mod.load_master(), ctx, dart=dart, today=today)
    save_csv(WORK / f"kstock-{today}-{session}.csv", session, passed)
    macro = macro_line(client, today) if session == "morning" else ""
    for m in MARKETS:  # 시장별로 한 통씩
        text = message(session, m, picks[m], stats, ctx, dart is not None, today, macro)
        print(text, "\n")
        if not a.dry_run:
            send_telegram(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
