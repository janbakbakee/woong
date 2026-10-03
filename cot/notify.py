"""리포트 생성 알림 (텔레그램 / ntfy 푸시).

설정된 채널로만 보낸다 (GitHub Secrets):
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID   텔레그램 봇 메시지
  NTFY_TOPIC                              ntfy 앱 푸시 (가입 불필요)

    python -m cot.notify --date 2026-09-22
    python -m cot.notify --failure --run-url https://github.com/.../actions/runs/123
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

import requests

from . import config, fetch

log = logging.getLogger("cot.notify")
TIMEOUT = 20


def success_message(meta: dict) -> tuple[str, str, str]:
    """(제목, 본문, 링크)"""
    d = date.fromisoformat(meta["report_date"])
    link = f"{config.site_url()}/reports/{d.isoformat()}.html"
    title = f"📊 COT 주간 리포트 — {meta.get('week_label') or fetch.week_label(d)}"
    scores = " · ".join(f"{k} {v}" for k, v in meta.get("scores", {}).items())
    lines = [f"기준일 {d.month}/{d.day}(화) · 발표 {meta.get('release_date', '')[5:].replace('-', '/')}(금)"]
    if not meta.get("ai"):
        lines.append("⚠️ AI 분석 미실행 — 수치·차트만 게시")
    if meta.get("missing"):
        lines.append("⏳ 데이터 대기: " + ", ".join(meta["missing"]))
    if meta.get("comment"):
        lines.append(f"💬 {meta['comment']}")
    lines += [
        f"투자의견: {meta.get('sheet_opinion', '')}",
        f"점수: {scores}",
        f"핵심: {meta.get('sheet_memo', '')}",
    ]
    return title, "\n".join(lines), link


def failure_message(run_url: str) -> tuple[str, str, str]:
    return ("⚠️ COT 리포트 생성 실패",
            "이번 주 워크플로가 실패했습니다. 실행 로그를 확인해 주세요.", run_url)


def send_telegram(title: str, body: str, link: str) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=TIMEOUT, json={
        "chat_id": chat, "text": f"{title}\n\n{body}\n\n🔗 {link}", "disable_web_page_preview": False,
    })
    resp.raise_for_status()
    return True


def send_ntfy(title: str, body: str, link: str) -> bool:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    resp = requests.post("https://ntfy.sh/", timeout=TIMEOUT, json={
        "topic": topic, "title": title, "message": body, "click": link, "tags": ["chart_with_upwards_trend"],
    })
    resp.raise_for_status()
    return True


def send_all(title: str, body: str, link: str) -> list[str]:
    sent = []
    for name, fn in (("telegram", send_telegram), ("ntfy", send_ntfy)):
        try:
            if fn(title, body, link):
                sent.append(name)
        except requests.RequestException as e:
            log.error("%s 알림 실패: %s", name, e)
    return sent


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="COT 리포트 알림")
    p.add_argument("--date", help="기준일 YYYY-MM-DD (생략 시 최신 리포트)")
    p.add_argument("--out", default="docs")
    p.add_argument("--failure", action="store_true", help="실패 알림")
    p.add_argument("--run-url", default="")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.failure:
        msg = failure_message(args.run_url)
    else:
        reports = Path(args.out) / "data" / "reports"
        if args.date:
            meta_path = reports / f"{args.date}.json"
        else:  # 날짜 미지정(이미 생성된 주차를 수동 실행한 경우) → 최신 리포트
            found = sorted(reports.glob("*.json"))
            meta_path = found[-1] if found else reports / "none.json"
        if not meta_path.exists():
            log.warning("메타 파일 없음: %s — 알림 생략", meta_path)
            return 0
        msg = success_message(json.loads(meta_path.read_text(encoding="utf-8"))["meta"])
    sent = send_all(*msg)
    log.info("알림 전송: %s", ", ".join(sent) or "설정된 채널 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
