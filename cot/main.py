"""주간 COT 리포트 생성 진입점.

GitHub Actions (Claude 구독 토큰 방식)
    python -m cot.main prepare     # CFTC 데이터·지표·RSS 수집 → work/ 에 작업 지시서 생성
    (claude-code-action 이 work/prompt.md 를 읽고 news.md / analysis.json 작성)
    python -m cot.main render      # work/ 결과를 검증해 HTML 리포트 게시

로컬 한 번에 실행
    python -m cot.main                    # ANTHROPIC_API_KEY 있으면 API로 분석, 없으면 수치만
    python -m cot.main --date 2026-07-28 --force
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import analyze, config, fetch, metrics as mx, prompt, record, render, rss, site

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


def _gh_output(**kv) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


def _ai_expected() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) or os.environ.get("COT_AI_EXPECTED") == "true"


# --------------------------------------------------------------------------
# 1) prepare: Step 1(조회) + Step 2(날짜 검증) + 지표 + RSS
# --------------------------------------------------------------------------

def prepare(args) -> dict | None:
    """성공 시 state dict, 건너뛰거나 데이터가 없으면 None (state['rc']에 종료코드)."""
    now = datetime.now(timezone.utc)
    if args.date:
        try:
            target = fetch.parse_week(args.date)
        except ValueError as e:
            log.error("%s", e)
            _gh_summary(f"### ❌ 입력 오류\n\n{e}")
            raise SystemExit(2)
        log.info("입력 '%s' → CFTC 기준일 %s (%s)", args.date, target, fetch.week_label(target))
    else:
        target = fetch.expected_report_date(now)
    out = Path(args.out)
    meta_path = out / "data" / "reports" / f"{target.isoformat()}.json"

    if meta_path.exists() and not args.force:
        prev = json.loads(meta_path.read_text(encoding="utf-8"))["meta"]
        if prev.get("complete") and (prev.get("ai") or not _ai_expected()):
            log.info("%s 리포트가 이미 완성되어 있어 건너뜁니다. (--force 로 재생성)", target)
            return None

    since = target - timedelta(weeks=52 * config.HISTORY_YEARS)
    all_series, sources, latest_seen = {}, [], {}
    for report in ("tff", "disagg"):
        try:
            rows, src = fetch.fetch_report(report, since)
        except fetch.FetchError as e:
            log.error("%s", e)
            continue
        sources.append(src)
        all_series.update(fetch.split_by_market(rows, report))
        if rows:
            latest_seen[report] = max(r["date"] for r in rows).isoformat()

    metrics = {}
    for m in config.MARKETS:
        s = all_series.get(m.key)
        mt = mx.market_metrics(m, s, target) if s else None
        if mt:
            metrics[m.key] = mt
    missing = [m.name for m in config.MARKETS if m.key not in metrics]

    if not metrics:
        msg = manual_paste_message([m.name for m in config.MARKETS],
                                   ", ".join(f"{k}={v}" for k, v in latest_seen.items()) or None)
        log.warning("기준일 %s 데이터 없음.\n%s", target, msg)
        _gh_summary(f"### ⏳ {target} 기준 COT 데이터 미확인\n\n```\n{msg}\n```")
        _gh_output(waiting=target.isoformat())  # 정시 실행이면 '발표 지연' 알림용
        if args.strict:
            raise SystemExit(3)
        return None

    log.info("%s 기준 데이터 확인됐습니다. 분석 시작합니다. (확인 %d개 / 대기 %d개)",
             f"{target.month}월 {target.day}일", len(metrics), len(missing))
    if missing:
        log.warning(manual_paste_message(missing, None))

    run_date = now.astimezone(KST).date()
    try:
        headlines = rss.collect(target, run_date)
    except Exception:  # noqa: BLE001 — RSS는 보조 자료, 실패해도 진행
        log.exception("RSS 수집 실패")
        headlines = []
    log.info("RSS 헤드라인 %d건", len(headlines))

    return {
        "target": target.isoformat(), "generated_at": now.isoformat(),
        "metrics": metrics, "missing": missing, "sources": sources, "rss": headlines,
    }


def write_work(state: dict, work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    for stale in ("news.md", "analysis.json"):
        (work / stale).unlink(missing_ok=True)
    (work / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    slim = [{k: v for k, v in m.items() if k != "chart"} for m in state["metrics"].values()]
    (work / "metrics.json").write_text(json.dumps(slim, ensure_ascii=False, indent=1), encoding="utf-8")
    (work / "rss.json").write_text(json.dumps(state["rss"], ensure_ascii=False, indent=1), encoding="utf-8")
    prompt.build(work, date.fromisoformat(state["target"]),
                 datetime.fromisoformat(state["generated_at"]).astimezone(KST).date(),
                 list(state["metrics"]))


# --------------------------------------------------------------------------
# 2) AI 결과 읽기 (Claude Code가 작성한 파일)
# --------------------------------------------------------------------------

def load_claude_output(work: Path, metrics: dict) -> tuple[dict, dict | None]:
    apath, npath = work / "analysis.json", work / "news.md"
    if not apath.exists():
        return analyze.fallback_analysis(metrics, "Claude 분석 결과 파일 없음"), None
    try:
        text = apath.read_text(encoding="utf-8").strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[1].rsplit("```", 1)[0]
        raw = json.loads(text)
        sources = [s for s in raw.pop("sources", []) or []
                   if isinstance(s, dict) and str(s.get("url", "")).startswith("http")]
        analysis = analyze.postprocess(raw, metrics)
    except (json.JSONDecodeError, analyze.AnalysisError, IndexError) as e:
        log.error("analysis.json 검증 실패: %s", e)
        return analyze.fallback_analysis(metrics, "Claude 분석 결과 형식 오류"), None
    news_text = npath.read_text(encoding="utf-8").strip() if npath.exists() else ""
    news = {"text": news_text, "sources": sources} if (news_text or sources) else None
    return analysis, news


# --------------------------------------------------------------------------
# 3) publish: HTML + 메타 + 인덱스
# --------------------------------------------------------------------------

def publish(state: dict, analysis: dict, news: dict | None, out: Path, model: str) -> None:
    """주차 데이터 저장 → 수치 기록 누적 → 사이트 전체 재구성."""
    target = date.fromisoformat(state["target"])
    release = fetch.release_date(target)
    metrics, missing = state["metrics"], state["missing"]

    meta = {
        "report_date": target.isoformat(), "release_date": release.isoformat(),
        "week_label": fetch.week_label(target), "generated_at": state["generated_at"],
        "complete": not missing, "ai": analysis.get("ai", False), "missing": missing, "model": model,
        "nets": {k: m["net"] for k, m in metrics.items()},
        "scores": {k: analysis["markets"][k]["score"] for k in metrics},
        "verdicts": {k: analysis["markets"][k]["verdict"] for k in metrics},
        "sheet_opinion": analysis.get("sheet_opinion", ""), "sheet_memo": analysis.get("sheet_memo", ""),
        "comment": analysis.get("one_liner", ""),
    }
    data_path = out / "data" / "reports" / f"{target.isoformat()}.json"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    # 차트 데이터까지 저장 → 사이트 재구성 시 이 파일만으로 리포트를 다시 그릴 수 있음
    data_path.write_text(json.dumps({"meta": meta, "metrics": metrics, "analysis": analysis, "news": news,
                                     "rss": state.get("rss", []), "sources": state["sources"]},
                                    ensure_ascii=False, default=str), encoding="utf-8")

    # 주차별 수치 기록 누적 (구글 시트와 같은 열 구성, 영구 보관)
    record.upsert(out / "data" / "cot_record.csv", record.make_row(
        target.isoformat(), meta["nets"], meta["scores"], meta["sheet_opinion"], meta["sheet_memo"]))
    (out / "data" / "history.csv").unlink(missing_ok=True)  # 구 형식 파일 정리

    site.rebuild(out)

    header, row = render.sheet_row(target, metrics, analysis)
    log.info("⑫ 구글 시트 행:\n%s\n%s", header, row)
    _gh_summary(f"### ✅ COT 리포트 {fetch.week_label(target)} ({target}) 생성\n\n- AI 분석: {'예' if meta['ai'] else '아니오'}\n"
                f"- 대기 시장: {', '.join(missing) or '없음'}\n\n```\n{header}\n{row}\n```")


# --------------------------------------------------------------------------

def cmd_prepare(args) -> int:
    state = prepare(args)
    if state is None:
        _gh_output(run="false")
        return 0
    write_work(state, Path(args.work))
    _gh_output(run="true", date=state["target"])
    return 0


def cmd_render(args) -> int:
    work = Path(args.work)
    state_path = work / "state.json"
    if not state_path.exists():
        log.error("%s 없음 — 먼저 prepare 단계를 실행하세요.", state_path)
        return 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if args.no_ai:
        analysis, news = analyze.fallback_analysis(state["metrics"], "--no-ai 옵션"), None
    else:
        analysis, news = load_claude_output(work, state["metrics"])
    publish(state, analysis, news, Path(args.out), model=os.environ.get("COT_MODEL") or "Claude Code")
    return 0


def cmd_rebuild(args) -> int:
    """저장된 주차 데이터로 사이트만 다시 만든다 (분석·데이터 수집 없음)."""
    site.rebuild(Path(args.out))
    return 0


def cmd_run(args) -> int:
    state = prepare(args)
    if state is None:
        return 0
    metrics = state["metrics"]
    news = None
    if args.no_ai or not os.environ.get("ANTHROPIC_API_KEY"):
        reason = "--no-ai 옵션" if args.no_ai else "ANTHROPIC_API_KEY 미설정"
        analysis = analyze.fallback_analysis(metrics, reason)
    else:
        target = date.fromisoformat(state["target"])
        try:
            news = analyze.news_brief(target, fetch.release_date(target),
                                      datetime.now(timezone.utc).astimezone(KST).date())
            analysis = analyze.run_analysis(metrics, news, target)
        except Exception as e:  # noqa: BLE001 — 어떤 실패든 수치 리포트는 발행
            log.exception("AI 분석 실패")
            analysis = analyze.fallback_analysis(metrics, f"AI 분석 오류 ({type(e).__name__})")
    publish(state, analysis, news, Path(args.out), model=analyze.MODEL)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="CFTC COT 주간 리포트 생성")
    p.add_argument("stage", nargs="?", default="run", choices=["run", "prepare", "render", "rebuild"],
                   help="run=한 번에 실행 / prepare·render=GitHub Actions 단계 / rebuild=사이트만 재구성")
    p.add_argument("--date", help="기준 주차: 2026-09-22 또는 '2026년 9월 2주차'. 생략 시 최신 발표 주차")
    p.add_argument("--out", default="docs", help="출력 폴더 (GitHub Pages 루트)")
    p.add_argument("--work", default="work", help="Claude 작업 폴더 (prepare/render)")
    p.add_argument("--no-ai", action="store_true", help="AI 분석 없이 수치만 생성")
    p.add_argument("--force", action="store_true", help="이미 생성된 주차도 다시 생성")
    p.add_argument("--strict", action="store_true", help="데이터 미발표 시 실패 코드로 종료")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return {"run": cmd_run, "prepare": cmd_prepare, "render": cmd_render, "rebuild": cmd_rebuild}[args.stage](args)


if __name__ == "__main__":
    sys.exit(main())
