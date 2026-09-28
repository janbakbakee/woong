"""주차별 수치 기록 (구글 시트 "COT 주차별 수치 기록"과 같은 열 구성).

docs/data/cot_record.csv 에 날짜별로 한 행씩 누적한다.
- 같은 날짜를 다시 생성하면 그 행을 덮어쓴다 (upsert).
- 과거 수기 기록 행도 같은 파일에 함께 보관한다.
- 공개 URL이므로 구글 시트에서 =IMPORTDATA("<사이트>/data/cot_record.csv") 로 자동 연동 가능.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

ORDER = ["ES", "NQ", "WTI", "EUR", "JPY", "BTC"]
COLUMNS = (["날짜"] + [f"{k} Net" for k in ORDER] + [f"{k}점수" for k in ORDER]
           + ["투자의견", "핵심메모"])


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8-sig")
    return [dict(r) for r in csv.DictReader(io.StringIO(text)) if r.get("날짜")]


def save(path: Path, rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda r: r["날짜"])
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    # BOM: 엑셀에서 한글이 깨지지 않도록
    path.write_text("﻿" + buf.getvalue(), encoding="utf-8")


def make_row(report_date: str, nets: dict, scores: dict, opinion: str, memo: str) -> dict:
    row = {"날짜": report_date, "투자의견": opinion, "핵심메모": memo}
    for k in ORDER:
        row[f"{k} Net"] = str(nets[k]) if k in nets else ""
        row[f"{k}점수"] = str(scores[k]) if k in scores else ""
    return row


def upsert(path: Path, row: dict) -> list[dict]:
    rows = [r for r in load(path) if r["날짜"] != row["날짜"]]
    rows.append(row)
    save(path, rows)
    return sorted(rows, key=lambda r: r["날짜"], reverse=True)


def fmt_net(v: str) -> str:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return v or "—"
    return f"{n:+,}".replace("-", "−")
