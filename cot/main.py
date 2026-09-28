"""주간 COT 리포트 생성 진입점.

    python -m cot.main                  # 이번 주 최신 데이터로 생성 (이미 완료된 주차면 건너뜀)
    python -m cot.main --date 2026-07-28  # 특정 주차 재생성
    python -m cot.main --no-ai          # Claude API 없이 수치만
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import analyze, config, fetch, metrics as mx, render

log = logging.getLogger("cot")
KST = timezone(timedelta(hours=9))


def manual_paste_message(market_names: list[str], data_date: str | None) -> str:
    head = (f"현재 조회한 데이터는 {data_date} 기준입니다. 요청하신 주차와 다를 수 있습니다.\n"
            if data_date else "")
    return (f"{head}[{', '.join(market_names)}] 데이터를 확인할 수 없습니다.\n"
            f"아래 URL에서 직접 확인해 주세요.\n"
            f"① 금융선물 (NQ/ES/JPY/EUR/USD/BTC): {config.SOURCE_PAGES['tff']}\n"
            f"② WTI 원유: {config.SOURCE_PAGES['disagg']}\n"
            f"→ 추정치나 이전 데이터로 대체하지 않았습니다.")


def _gh_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def load_records(out: Path) -> list[dict]:
    recs = []
    for p in sorted((out / "data" / "reports").glob("*.json"), reverse=True):
        try:
            recs.append(json.loads(p.read_text(encoding="utf-8"))["meta"])
        except (json.JSONDecodeError, KeyError):
            log.warning("메타 파일 손상: %s", p)
    return recs


def write_history_csv(out: Path, records: list[dict]) -> None:
    keys = [m.key for m in config.MARKETS]
    header = ["날짜"] + [f"{k} Net" for k in keys] + [f"{k}점수" for k in keys] + ["투자의견", "핵심메모"]
    lines = [",".join(header)]
    for r in sorted(records, key=lambda r: r["report_date"]):
        vals = [r["report_date"]]
        vals += [str(r.get("nets", {}).get(k, "")) for k in keys]
        vals += [str(r.get("scores", {}).get(k, "")) for k in keys]
        vals += [r.get("sheet_opinion", ""), r.get("sheet_memo", "")]
        lines.append(",".join('"' + v.replace('"', '""') + '"' if ("," in v or '"' in v) else v for v in vals))
    (out / "data" / "history.csv").write_text("﻿" + "\n".join(lines) + "\n", encoding="utf-8")


def run(args) -> int:
    now = datetime.now(timezone.utc)
    target = date.fromisoformat(args.date) if args.date else fetch.expected_report_date(now)
    release = fetch.release_date(target)
    out = Path(args.out)
    meta_path = out / "data" / "reports" / f"{target.isoformat()}.json"

    if meta_path.exists() and not args.force:
        prev = json.loads(meta_path.read_text(encoding="utf-8"))["meta"]
        if prev.get("complete") and (prev.get("ai") or not os.environ.get("ANTHROPIC_API_KEY")):
            log.info("%s 리포트가 이미 완성되어 있어 건너뜁니다. (--force 로 재생성)", target)
            return 0

    # ---- Step 1: 데이터 조회 --------------------------------------------------
    since = target - timedelta(weeks=52 * config.HISTORY_YEARS)
    all_series, sources, latest_seen = {}, [], {}
    for report in ("tff", "disagg"):
        try:
            rows, src = fetch.fetch_report(report, since)
        except fetch.FetchError as e:
            log.error("%s", e)
            continue
        sources.append(src)
        split = fetch.split_by_market(rows, report)
        all_series.update(split)
        if rows:
            latest_seen[report] = max(r["date"] for r in rows).isoformat()

    # ---- Step 2: 날짜 검증 ----------------------------------------------------
    metrics = {}
    for m in config.MARKETS:
        s = all_series.get(m.key)
        if not s:
            continue
        mt = mx.market_metrics(m, s, target)
        if mt:
            metrics[m.key] = mt
    missing = [m.name for m in config.MARKETS if m.key not in metrics]

    if not metrics:
        msg = manual_paste_message([m.name for m in config.MARKETS],
                                   ", ".join(f"{k}={v}" for k, v in latest_seen.items()) or None)
        log.warning("기준일 %s 데이터 없음.\n%s", target, msg)
        _gh_summary(f"### ⏳ {target} 기준 COT 데이터 미확인\n\n```\n{msg}\n```")
        return 3 if args.strict else 0

    log.info("%s 기준 데이터 확인됐습니다. 분석 시작합니다. (확인 %d개 / 대기 %d개)",
             f"{target.month}월 {target.day}일", len(metrics), len(missing))
    if missing:
        log.warning(manual_paste_message(missing, None))

    # ---- Step 2.5 + 3: 뉴스 서치 & AI 분석 ----------------------------------
    news = None
    if args.no_ai or not os.environ.get("ANTHROPIC_API_KEY"):
        reason = "--no-ai 옵션" if args.no_ai else "ANTHROPIC_API_KEY 미설정"
        analysis = analyze.fallback_analysis(metrics, reason)
    else:
        try:
            news = analyze.news_brief(target, release, now.astimezone(KST).date())
            analysis = analyze.run_analysis(metrics, news, target)
        except Exception as e:  # noqa: BLE001 — 어떤 실패든 수치 리포트는 발행
            log.exception("AI 분석 실패")
            analysis = analyze.fallback_analysis(metrics, f"AI 분석 오류 ({type(e).__name__})")

    # ---- 출력 ----------------------------------------------------------------
    html = render.render_report(
        report_date=target, release=release, run_kst=now.astimezone(KST),
        metrics=metrics, analysis=analysis, news=news, data_sources=sources, model=analyze.MODEL,
    )
    (out / "reports").mkdir(parents=True, exist_ok=True)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    (out / "reports" / f"{target.isoformat()}.html").write_text(html, encoding="utf-8")

    meta = {
        "report_date": target.isoformat(), "release_date": release.isoformat(),
        "generated_at": now.isoformat(), "complete": not missing, "ai": analysis.get("ai", False),
        "missing": missing,
        "nets": {k: m["net"] for k, m in metrics.items()},
        "scores": {k: analysis["markets"][k]["score"] for k in metrics},
        "verdicts": {k: analysis["markets"][k]["verdict"] for k in metrics},
        "sheet_opinion": analysis.get("sheet_opinion", ""), "sheet_memo": analysis.get("sheet_memo", ""),
    }
    slim_metrics = {k: {kk: vv for kk, vv in m.items() if kk != "chart"} for k, m in metrics.items()}
    meta_path.write_text(json.dumps({"meta": meta, "metrics": slim_metrics, "analysis": analysis,
                                     "news": news}, ensure_ascii=False, indent=1, default=str),
                         encoding="utf-8")

    records = load_records(out)
    (out / "index.html").write_text(render.render_index(records), encoding="utf-8")
    write_history_csv(out, records)
    (out / ".nojekyll").touch()

    header, row = render.sheet_row(target, metrics, analysis)
    log.info("⑫ 구글 시트 행:\n%s\n%s", header, row)
    _gh_summary(f"### ✅ COT 리포트 {target} 생성\n\n- AI 분석: {'예' if meta['ai'] else '아니오'}\n"
                f"- 대기 시장: {', '.join(missing) or '없음'}\n\n```\n{header}\n{row}\n```")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="CFTC COT 주간 리포트 생성")
    p.add_argument("--date", help="CFTC 기준일(화요일) YYYY-MM-DD. 생략 시 최신 발표 주차")
    p.add_argument("--out", default="docs", help="출력 폴더 (GitHub Pages 루트)")
    p.add_argument("--no-ai", action="store_true", help="Claude 분석 없이 수치만 생성")
    p.add_argument("--force", action="store_true", help="이미 생성된 주차도 다시 생성")
    p.add_argument("--strict", action="store_true", help="데이터 미발표 시 실패 코드로 종료")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
