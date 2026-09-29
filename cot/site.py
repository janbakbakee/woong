"""사이트 전체 재구성: 메인(최신 리포트) · 월별 탭 · 기록 탭 · 보관기간 정리.

매 실행마다 docs/data/reports/*.json(주차별 저장 데이터)에서 모든 페이지를 다시 만든다.
- index.html            최신 주차 리포트 (상단 탭 포함)
- months/YYYY-MM.html   그 달 주차 목록
- record.html           주차별 점수·수치 기록 (cot_record.csv 는 영구 보관)
- reports/<date>.html   주차별 리포트, reports/<date>.md 텍스트 요약, latest.md
- 보관기간(RETENTION_DAYS)이 지난 주차의 리포트/요약/데이터는 삭제 (git 이력에는 남음)
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

from . import fetch, markdown, record, render

log = logging.getLogger(__name__)
RETENTION_DAYS = 365
MAX_MONTH_TABS = 12
KST = timezone(timedelta(hours=9))


def month_key(d: str) -> str:
    return d[:7]


def month_label(key: str) -> str:
    y, m = key.split("-")
    return f"{y}년 {int(m)}월"


def nav_html(base: str, months: list[str], active: str) -> str:
    """상단 탭. active: 'latest' | 'YYYY-MM' | 'record'."""
    tabs = [("latest", "최신", f"{base}index.html")]
    tabs += [(k, month_label(k), f"{base}months/{k}.html") for k in months]
    tabs.append(("record", "기록", f"{base}record.html"))
    items = "".join(
        f'<a class="tab{" on" if key == active else ""}" href="{escape(href)}">{escape(label)}</a>'
        for key, label, href in tabs)
    return f'<nav class="tabs">{items}</nav>'


def load_all(out: Path) -> list[dict]:
    """저장된 주차 데이터 (최신순)."""
    items = []
    for p in (out / "data" / "reports").glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            d["meta"]["report_date"]  # noqa: B018 — 형식 확인
            items.append(d)
        except (json.JSONDecodeError, KeyError):
            log.warning("저장 데이터 손상: %s", p)
    return sorted(items, key=lambda d: d["meta"]["report_date"], reverse=True)


def prune(out: Path, latest: date) -> list[str]:
    """보관기간이 지난 주차 파일 삭제. 삭제된 기준일 목록 반환."""
    cutoff = latest - timedelta(days=RETENTION_DAYS)
    removed = []
    for p in (out / "data" / "reports").glob("*.json"):
        try:
            d = date.fromisoformat(p.stem)
        except ValueError:
            continue
        if d < cutoff:
            for f in (p, out / "reports" / f"{p.stem}.html", out / "reports" / f"{p.stem}.md"):
                f.unlink(missing_ok=True)
            removed.append(p.stem)
    # 저장 데이터가 없는 고아 리포트 파일도 정리
    for f in list((out / "reports").glob("*.html")) + list((out / "reports").glob("*.md")):
        try:
            if date.fromisoformat(f.stem) < cutoff:
                f.unlink()
        except ValueError:
            pass
    return sorted(removed)


def _render_week(d: dict, base: str, nav: str) -> str:
    meta = d["meta"]
    generated = datetime.fromisoformat(meta["generated_at"]).astimezone(KST)
    return render.render_report(
        report_date=date.fromisoformat(meta["report_date"]),
        release=date.fromisoformat(meta["release_date"]),
        run_kst=generated, metrics=d["metrics"], analysis=d["analysis"], news=d.get("news"),
        rss=d.get("rss", []), data_sources=d.get("sources") or ["CFTC Commitments of Traders"],
        model=meta.get("model", "Claude"), base=base, nav=nav,
    )


def rebuild(out: Path) -> None:
    items = load_all(out)
    if not items:
        return
    latest = date.fromisoformat(items[0]["meta"]["report_date"])
    removed = prune(out, latest)
    if removed:
        log.info("보관기간(%d일) 경과로 정리: %s", RETENTION_DAYS, ", ".join(removed))
        items = load_all(out)

    months = sorted({month_key(d["meta"]["report_date"]) for d in items}, reverse=True)[:MAX_MONTH_TABS]
    record_rows = sorted(record.load(out / "data" / "cot_record.csv"), key=lambda r: r["날짜"], reverse=True)
    (out / "reports").mkdir(parents=True, exist_ok=True)
    (out / "months").mkdir(parents=True, exist_ok=True)

    # 주차별 리포트 + 텍스트 요약
    for d in items:
        meta = d["meta"]
        rd = meta["report_date"]
        nav = nav_html("../", months, month_key(rd))
        (out / "reports" / f"{rd}.html").write_text(_render_week(d, "../", nav), encoding="utf-8")
        (out / "reports" / f"{rd}.md").write_text(
            markdown.render(meta, d["metrics"], d["analysis"], record_rows), encoding="utf-8")

    # 메인 = 최신 주차
    newest = items[0]
    (out / "index.html").write_text(_render_week(newest, "", nav_html("", months, "latest")), encoding="utf-8")
    (out / "latest.md").write_text(
        markdown.render(newest["meta"], newest["metrics"], newest["analysis"], record_rows), encoding="utf-8")

    # 월별 탭
    for p in (out / "months").glob("*.html"):
        if p.stem not in months:
            p.unlink()
    for mk in months:
        weeks = [d["meta"] for d in items if month_key(d["meta"]["report_date"]) == mk]
        (out / "months" / f"{mk}.html").write_text(
            render.render_month(mk, month_label(mk), weeks, nav_html("../", months, mk)), encoding="utf-8")

    # 기록 탭
    metas = [d["meta"] for d in items]
    (out / "record.html").write_text(
        render.render_record(metas, record_rows, nav_html("", months, "record")), encoding="utf-8")
    (out / ".nojekyll").touch()
    log.info("사이트 재구성: 리포트 %d주, 월 탭 %d개", len(items), len(months))
