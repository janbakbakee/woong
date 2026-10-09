"""국내주식 수급 추천 실행 → 텔레그램.

    python -m kstock.main --session morning      # 08시 장 전 (전일 확정 수급 + 밤사이 뉴스)
    python -m kstock.main --session close        # 장 마감 후
    python -m kstock.main --session close --dry-run   # 전송 없이 출력만
    python -m kstock.main --failure https://github.com/.../actions/runs/123

환경변수: KIS_APP_KEY, KIS_APP_SECRET (필수) · TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID · DART_API_KEY (선택)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

from . import extras, kis as kis_mod, score as sc

log = logging.getLogger("kstock")
KST = timezone(timedelta(hours=9))
TOP = 5
SHORTLIST = 12                 # 뉴스·공시 확인 대상 (예비 점수 상위)
MIN_PREV_VALUE = 10 * sc.EOK   # 전일 거래대금 사전 필터 (API 호출 수 절감)
SESSION_LABEL = {"morning": "장 전 브리핑", "close": "장 마감"}


def candidates(stocks: list[kis_mod.Stock]) -> list[kis_mod.Stock]:
    return [s for s in stocks if s.ok and s.mcap >= sc.MIN_MCAP[s.market] and s.prev_value >= MIN_PREV_VALUE]


def run(session: str, client: kis_mod.KIS, stocks: list[kis_mod.Stock], dart=None, news=extras.news,
        today: str | None = None) -> tuple[list[sc.Pick], int, int]:
    """(추천, 스캔 종목 수, 조건 통과 수)"""
    today = today or datetime.now(KST).strftime("%Y%m%d")
    pool = candidates(stocks)
    passed, errors = [], 0
    for s in pool:
        try:
            rows = client.investor_daily(s.code, today)
        except Exception as e:
            errors += 1
            log.warning("%s %s: %s", s.code, s.name, e)
            continue
        if session == "morning":  # 장 전에는 오늘 행(미확정)을 쓰지 않는다
            rows = [r for r in rows if r["stck_bsop_date"] < today]
        p = sc.analyze(s, rows)
        if p:
            passed.append(sc.score(p))
    if pool and errors > len(pool) / 2:
        raise RuntimeError(f"KIS 조회 실패 {errors}/{len(pool)}")

    passed.sort(key=lambda p: p.score, reverse=True)
    picks = []
    for p in passed[:SHORTLIST]:
        if dart and any(extras.is_bad(t) for t in dart.recent(p.stock.code)):
            log.info("공시 악재로 제외: %s", p.stock.name)
            continue
        p.news = news(p.stock.name)
        p.flags = [t for t, _ in p.news if extras.is_bad(t)]
        picks.append(sc.score(p))
    picks.sort(key=lambda p: p.score, reverse=True)
    return picks[:TOP], len(pool), len(passed)


def _eok(x: float) -> str:
    return f"{x / sc.EOK:+,.0f}억"


def message(session: str, picks: list[sc.Pick], scanned: int, passed: int, dart_on: bool) -> str:
    d = picks[0].date if picks else ""
    when = f"{d[4:6]}/{d[6:]} 기준" if d else ""
    lines = [f"📈 국내주식 수급 추천 — {SESSION_LABEL[session]} {when}".rstrip(),
             f"스캔 {scanned}종목 · 조건 통과 {passed}종목" + ("" if dart_on else " · DART 미연결")]
    if not picks:
        lines.append("\n오늘은 조건(외국인·기관 동반 순매수 + 유동성 + 과열 제외)을 통과한 종목이 없습니다.")
    for i, p in enumerate(picks, 1):
        s = p.stock
        trend = " · ".join(t for t, ok in (("20일선 위", p.above_ma20), ("정배열", p.ma_aligned),
                                           ("고점 근접", p.near_high)) if ok) or "추세 약함"
        lines += [
            "",
            f"{i}) {s.name} ({s.code}·{s.market}) {p.score}점",
            f"   종가 {p.close:,.0f} ({p.chg:+.1f}%) · 시총 {s.mcap:,.0f}억",
            f"   5일 외국인 {_eok(p.frgn5)} · 기관 {_eok(p.orgn5)} · 쌍끌이 {p.streak}일 연속",
            f"   거래량 {p.vol_ratio:.1f}배 · {trend}",
            f"   영업이익 {'흑자' if s.op_profit > 0 else '적자'} · ROE {s.roe:g}",
            "   " + " · ".join(f"{k} {v}" for k, v in p.parts.items()),
        ]
        lines += [f"   {'⚠️' if t in p.flags else '📰'} {t}" for t, _ in p.news[:2]]
        lines.append(f"   🔗 https://finance.naver.com/item/main.naver?code={s.code}")
    lines.append("\n※ 수급 기반 후보 목록이며 투자 권유가 아닙니다.")
    return "\n".join(lines)[:4000]


def send_telegram(text: str) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
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
    ap.add_argument("--session", choices=SESSION_LABEL, default="close")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="휴장일에도 실행")
    ap.add_argument("--failure", metavar="RUN_URL")
    a = ap.parse_args(argv)

    if a.failure:
        send_telegram(f"⚠️ 국내주식 추천 실패\n실행 로그를 확인해 주세요.\n🔗 {a.failure}")
        return 0

    client = kis_mod.KIS()
    today = datetime.now(KST).strftime("%Y%m%d")
    if not a.force and not client.is_open(today):
        log.info("휴장일 %s — 건너뜀", today)
        return 0
    dart = extras.Dart.from_env()
    picks, scanned, passed = run(a.session, client, kis_mod.load_master(), dart=dart, today=today)
    text = message(a.session, picks, scanned, passed, dart is not None)
    print(text)
    if not a.dry_run:
        send_telegram(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
