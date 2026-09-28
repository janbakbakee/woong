"""AI·사람이 읽기 쉬운 텍스트 요약본 (reports/<date>.md, latest.md).

HTML 리포트는 차트/스타일이 많아 AI가 읽기에 무겁다. 매매 상담 시
"https://<사이트>/latest.md" 하나만 주면 핵심 수치·판단·최근 기록을 모두 읽을 수 있게 만든다.
"""
from __future__ import annotations

from datetime import date

from . import config, fetch, record


def _n(v) -> str:
    return "—" if v is None else f"{int(v):+,}"


def _cell(v) -> str:
    return str(v if v not in (None, "") else "—").replace("|", "/").replace("\n", " ")


def render(meta: dict, metrics: dict, analysis: dict, record_rows: list[dict], recent_weeks: int = 12) -> str:
    d = date.fromisoformat(meta["report_date"])
    site = config.site_url()
    a_mk = analysis.get("markets", {})
    out = [
        f"# CFTC COT 주간 분석 — {fetch.week_label(d)}",
        "",
        f"- CFTC 기준일: {d.isoformat()} (화) / 공식 발표: {meta.get('release_date', '')} (금)",
        f"- 생성: {meta.get('generated_at', '')[:16].replace('T', ' ')} UTC · "
        f"AI 분석: {'예' if meta.get('ai') else '아니오 (수치만)'}"
        + (f" · 데이터 대기: {', '.join(meta['missing'])}" if meta.get("missing") else ""),
        f"- 전체 리포트(HTML): {site}/reports/{d.isoformat()}.html",
        f"- 주차별 누적 기록(CSV): {site}/data/cot_record.csv",
        "",
        "## 1. 시장별 요약",
        "",
        "Net = 헤드라인 그룹 순포지션 (NQ/ES/BTC/JPY/USD=Leveraged Funds, EUR=Asset Managers, WTI=Managed Money). "
        "%ile = 실제 3년/5년 백분위(0=역대 최대 순숏, 100=역대 최대 순롱). 점수 50=중립.",
        "",
        "| 시장 | Net | 전주 대비 | 진단 | 연속 | 3y %ile | 5y %ile | COT Idx 26w/156w | 주간변화 z | 점수(퀀트) | 의견 | 상승/횡보/하락 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cfg in config.MARKETS:
        m = metrics.get(cfg.key)
        if not m:
            out.append(f"| {cfg.key} | 데이터 수신 대기 |  |  |  |  |  |  |  |  |  |  |")
            continue
        a = a_mk.get(cfg.key, {})
        q = (m.get("quant") or {}).get("total")
        probs = ("/".join(f"{a.get(k)}%" for k in ("prob_up", "prob_side", "prob_down"))
                 if a.get("prob_up") is not None else "—")
        out.append(
            f"| {cfg.key} | {_n(m['net'])} | {_n(m['net_chg'])} | {m['diag']} | {m['streak']}주 | "
            f"{m['pct_3y']} | {m['pct_5y']} | {m['cot_idx_26w']}/{m['cot_idx_156w']} | {m['chg_z']} | "
            f"{a.get('score', '—')} ({q if q is not None else '—'}) | {a.get('verdict', '—')} | {probs} |")

    out += ["", "## 2. 그룹별 포지션 (Smart Money)", "",
            "| 시장 | 그룹 | 롱 | 숏 | Net | 전주 대비 | 진단 | 3y %ile |", "|---|---|---|---|---|---|---|---|"]
    for k, m in metrics.items():
        for g in m["groups"].values():
            out.append(f"| {k} | {g['label']} | {g['long']:,} | {g['short']:,} | {_n(g['net'])} | "
                       f"{_n(g['net_chg'])} | {g['diag']} | {g['pct_3y']} |")

    if meta.get("ai"):
        out += ["", "## 3. 시장별 판단", ""]
        for k in metrics:
            a = a_mk.get(k, {})
            out += [f"### {k} — {a.get('rating_label', '')} · {a.get('verdict', '')} · {a.get('score', '')}점",
                    f"- 변화: {_cell(a.get('change_note'))}",
                    f"- 스마트머니: {_cell(a.get('smart_money'))}",
                    f"- Extreme: {m_pct(metrics[k])} — {_cell(a.get('extreme_note'))}",
                    f"- Contrarian: {_cell(a.get('contrarian'))}",
                    f"- 트렌드: 단기 {_cell(a.get('trend_short'))} / 중기 {_cell(a.get('trend_mid'))} / "
                    f"장기 {_cell(a.get('trend_long'))}", ""]
        if analysis.get("sentiment"):
            out += ["## 4. 기관 심리 & 거시 베팅", ""]
            out += [f"- **{s.get('label', '')}**: {_cell(s.get('text'))}" for s in analysis["sentiment"]]
        if analysis.get("top5"):
            out += ["", "## 5. 이번 주 핵심 변화 TOP 5", ""]
            out += [f"{i}. **{t.get('title', '')}** — {_cell(t.get('body'))}"
                    for i, t in enumerate(analysis["top5"][:5], 1)]
        out += ["", "## 6. CIO Executive Summary", "", f"**{analysis.get('exec_theme', '')}**", ""]
        out += [f"- {p.get('label', '')}: {_cell(p.get('text'))}" for p in analysis.get("exec_paragraphs", [])]
        if analysis.get("exec_recommendation"):
            out.append(f"- 종합 권고: {analysis['exec_recommendation']}")
        if analysis.get("monitoring"):
            out.append("- 모니터링: " + " / ".join(analysis["monitoring"]))

    rows = sorted(record_rows, key=lambda r: r["날짜"], reverse=True)[:recent_weeks]
    if rows:
        out += ["", f"## 7. 최근 {len(rows)}주 기록 (최신순)", "",
                "| " + " | ".join(record.COLUMNS) + " |", "|" + "---|" * len(record.COLUMNS)]
        out += ["| " + " | ".join(_cell(r.get(c)) for c in record.COLUMNS) + " |" for r in rows]
    out += ["", "---", "데이터: CFTC Commitments of Traders (Futures Only). 투자 참고용이며 투자 권유가 아닙니다.", ""]
    return "\n".join(out)


def m_pct(m: dict) -> str:
    return f"3y {m['pct_3y']}%ile / 5y {m['pct_5y']}%ile"
