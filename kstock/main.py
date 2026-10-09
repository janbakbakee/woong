"""국내주식 수급 추천 실행 → 텔레그램.

    python -m kstock.main --session morning      # 장 전 (전일 확정 수급 + 밤사이 뉴스 + 미국 시장)  ※ pre 도 가능
    python -m kstock.main --session close        # 장 마감 후
    python -m kstock.main --session close --dry-run   # 전송 없이 출력만
    python -m kstock.main --send work/kstock-20261012-close-msg.json   # 저장된 메시지만 전송
    python -m kstock.main --failure https://github.com/.../actions/runs/123

환경변수: KIS_APP_KEY, KIS_APP_SECRET (필수) · TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
          (국장 전용 KSTOCK_TELEGRAM_BOT_TOKEN, KSTOCK_TELEGRAM_CHAT_ID 우선) · DART_API_KEY (선택)
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import extras, kis as kis_mod, score as sc, site

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
WORK = Path("work")            # 후보 기록 CSV·메시지 원문 (Actions 아티팩트로 보관, 레포 커밋 안 함)
SITE = Path("docs/kstock")     # 웹페이지 (장 마감 실행만 갱신·커밋)


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


def message(session: str, market: str, picks: list[sc.Pick], stats: dict, ctx: dict, dart_on: bool,
            today: str, macro: str = "", hist: dict | None = None, url: str = "") -> str:
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
    if not picks:
        lines.append("조건(외인·기관 동반 순매수 + 유동성 + 과열 제외)을 충족한 종목이 없습니다.")
    for i, p in enumerate(picks, 1):
        tags = [f"외인 {_eok(p.frgn5)}", f"기관 {_eok(p.orgn5)}",
                "고가 마감" if p.clv >= 0.7 else "윗꼬리" if p.upper_wick >= 0.5 else f"거래량 {p.vol_ratio:.1f}배"]
        seen = hist["seen"].get(p.stock.code)
        tags.append(f"🔁 {seen['count']}/{hist['window']}일" if seen and seen["count"] > 1 else "🆕")
        if p.ext_atr > 3.5:
            tags.append("⚠️과열")
        if p.flags:
            tags.append("⚠️악재뉴스")
        lines += [f"{i}) <b>{e(p.stock.name)}</b> <code>{p.stock.code}</code> {p.score}점",
                  "    " + " · ".join(tags)]
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
    a = ap.parse_args(argv)
    if a.send:
        for text in json.loads(Path(a.send).read_text(encoding="utf-8")):
            send_telegram(text)
        return 0
    session = "morning" if a.session == "pre" else a.session

    if a.failure:
        send_telegram(f"⚠️ 국내주식 추천 실패\n실행 로그를 확인해 주세요.\n🔗 {html.escape(a.failure)}")
        return 0

    client = kis_mod.KIS()
    today = datetime.now(KST).strftime("%Y%m%d")
    if not a.force and not client.is_open(today):
        log.info("휴장일 %s — 건너뜀", today)
        return 0
    ctx = market_context(client, session, today)
    dart = extras.Dart.from_env()
    picks, passed, stats = run(session, client, kis_mod.load_master(), ctx, dart=dart, today=today)
    save_csv(WORK / f"kstock-{today}-{session}.csv", session, passed, picks)
    url = os.environ.get("KSTOCK_SITE_URL") or site_url()
    if session == "close":  # 웹페이지는 장 마감만 갱신 (장 전 데이터는 전일 장 마감과 같음)
        attach_opinions(client, picks, today)
        site.save_day(SITE, ctx[MARKETS[0]]["ref"], picks, ctx, stats, dart is not None)
        site.build(SITE, url)
    days = site.load_days(SITE)
    macro = macro_line(client, today) if session == "morning" else ""
    texts = []
    for m in MARKETS:  # 시장별로 한 통씩
        hist = site.history(days, m) if days else None
        if hist and session == "morning":
            hist["exits"] = []  # 이탈은 장 마감 간 비교에서만 의미
        texts.append(message(session, m, picks[m], stats, ctx, dart is not None, today, macro, hist, url))
    (WORK / f"kstock-{today}-{session}-msg.json").write_text(json.dumps(texts, ensure_ascii=False), encoding="utf-8")
    for text in texts:
        print(text, "\n")
        if not a.dry_run:
            send_telegram(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
