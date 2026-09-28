"""인라인 SVG 차트 (JS 없음 → 카카오톡/구글드라이브/모바일 크롬에서 그대로 렌더링).

색상은 모두 하드코딩. 마우스 오버 시 <title> 기본 툴팁으로 값 표시.
"""
from __future__ import annotations

from html import escape

from .config import COLORS, GROUP_LABEL

C = COLORS


def fmt_num(n: float | int | None) -> str:
    if n is None:
        return "—"
    return f"{int(n):+,}".replace("-", "−")


def fmt_k(n: float) -> str:
    a = abs(n)
    s = f"{a / 1_000_000:.1f}M" if a >= 1_000_000 else (f"{a / 1000:.0f}K" if a >= 1000 else f"{a:.0f}")
    return ("−" if n < 0 else "+" if n > 0 else "") + s


def score_color(score: int | None) -> str:
    """템플릿 규칙: 60↑ 초록 / 40~59 주황 / 40 미만 파랑."""
    if score is None:
        return "#c0bfb8"
    if score >= 60:
        return "#1baf7a"
    if score >= 40:
        return "#EF9F27"
    return "#2a78d6"


def pct_color(p: float) -> str:
    if p >= 80:
        return C["bull"]
    if p <= 20:
        return C["bear"]
    return "#BA7517"


def gauge_svg(rows: list[dict]) -> str:
    """⑤ 3년 백분위 게이지. rows: [{label, pct, pct_prev, note}]"""
    w, row_h, top = 640, 34, 26
    x0, x1 = 92, 560
    h = top + row_h * len(rows) + 8
    sx = lambda p: x0 + (x1 - x0) * p / 100  # noqa: E731
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="3년 백분위 게이지" '
           f'style="display:block;font-family:inherit;">']
    # 극단 구간 음영 + 눈금
    out.append(f'<rect x="{sx(0)}" y="{top - 6}" width="{sx(10) - sx(0)}" height="{row_h * len(rows)}" fill="#fcebeb"/>')
    out.append(f'<rect x="{sx(90)}" y="{top - 6}" width="{sx(100) - sx(90)}" height="{row_h * len(rows)}" fill="#eaf3de"/>')
    for p in (0, 10, 50, 90, 100):
        out.append(f'<line x1="{sx(p)}" x2="{sx(p)}" y1="{top - 6}" y2="{top - 6 + row_h * len(rows)}" '
                   f'stroke="{C["border"]}" stroke-width="1" stroke-dasharray="{"0" if p in (0, 100) else "3 3"}"/>')
        out.append(f'<text x="{sx(p)}" y="{top - 12}" font-size="10" fill="{C["muted"]}" text-anchor="middle">{p}</text>')
    for i, r in enumerate(rows):
        cy = top + 11 + i * row_h
        p, pp = r["pct"], r.get("pct_prev")
        out.append(f'<text x="0" y="{cy + 4}" font-size="12" fill="{C["text"]}" font-weight="500">{escape(r["label"])}</text>')
        out.append(f'<line x1="{x0}" x2="{x1}" y1="{cy}" y2="{cy}" stroke="{C["grid"]}" stroke-width="6" stroke-linecap="round"/>')
        if pp is not None:
            out.append(f'<line x1="{sx(pp)}" x2="{sx(p)}" y1="{cy}" y2="{cy}" stroke="#c0bfb8" stroke-width="2"/>')
            out.append(f'<circle cx="{sx(pp)}" cy="{cy}" r="4.5" fill="#ffffff" stroke="#888780" stroke-width="1.5">'
                       f'<title>4주 전 {pp:.0f}%ile</title></circle>')
        col = pct_color(p)
        out.append(f'<circle cx="{sx(p)}" cy="{cy}" r="6" fill="{col}" stroke="#ffffff" stroke-width="2">'
                   f'<title>{escape(r["label"])}: 현재 {p:.0f}%ile</title></circle>')
        out.append(f'<text x="{w}" y="{cy + 4}" font-size="12" fill="{C["text"]}" text-anchor="end" '
                   f'font-weight="500">{p:.0f}%</text>')
    out.append("</svg>")
    return "".join(out)


def history_svg(chart: dict, groups: tuple, title: str) -> str:
    """3년 순포지션 추이 (그룹별 라인, 공통 y축)."""
    dates = chart["dates"]
    series = [(g, chart[g]) for g in groups if g in chart and any(v is not None for v in chart[g])]
    if len(dates) < 2 or not series:
        return ""
    w, h = 320, 150
    pl, pr, pt, pb = 44, 44, 10, 20
    vals = [v for _, s in series for v in s if v is not None] + [0]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or 1
    lo, hi = lo - pad, hi + pad
    n = len(dates)
    sx = lambda i: pl + (w - pl - pr) * i / (n - 1)  # noqa: E731
    sy = lambda v: pt + (h - pt - pb) * (hi - v) / (hi - lo)  # noqa: E731

    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{escape(title)}" '
           f'style="display:block;font-family:inherit;">']
    # 격자: 0선 + 상/하단
    for v, dash in ((hi - pad, "3 3"), (0, "0"), (lo + pad, "3 3")):
        y = sy(v)
        out.append(f'<line x1="{pl}" x2="{w - pr}" y1="{y:.1f}" y2="{y:.1f}" stroke="{C["border"] if v else "#c0bfb8"}" '
                   f'stroke-width="1" stroke-dasharray="{dash}"/>')
        out.append(f'<text x="{pl - 4}" y="{y + 3:.1f}" font-size="9" fill="{C["muted"]}" text-anchor="end">{fmt_k(v)}</text>')
    # x축: 연도 경계
    last_year = None
    for i, d in enumerate(dates):
        if d[:4] != last_year and i > 0 and d[5:7] == "01":
            out.append(f'<text x="{sx(i):.1f}" y="{h - 5}" font-size="9" fill="{C["muted"]}" text-anchor="middle">{d[:4]}</text>')
            out.append(f'<line x1="{sx(i):.1f}" x2="{sx(i):.1f}" y1="{pt}" y2="{h - pb}" stroke="{C["grid"]}" stroke-width="1"/>')
        last_year = d[:4]

    for g, s in series:
        col = C[g]
        pts = " ".join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in enumerate(s) if v is not None)
        out.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2" '
                   f'stroke-linejoin="round" stroke-linecap="round"/>')
        last_i = max(i for i, v in enumerate(s) if v is not None)
        lx, ly = sx(last_i), sy(s[last_i])
        out.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4" fill="{col}" stroke="#ffffff" stroke-width="2"/>')
        out.append(f'<text x="{lx + 6:.1f}" y="{ly + 3:.1f}" font-size="10" fill="{C["text"]}" '
                   f'font-weight="500">{fmt_k(s[last_i])}</text>')
    # 호버 영역 (주 단위 세로 띠 + 툴팁)
    step = (w - pl - pr) / (n - 1)
    for i, d in enumerate(dates):
        tip = " / ".join(f"{GROUP_LABEL[g]} {fmt_num(s[i])}" for g, s in series if s[i] is not None)
        out.append(f'<rect x="{sx(i) - step / 2:.1f}" y="{pt}" width="{step:.1f}" height="{h - pt - pb}" '
                   f'fill="transparent"><title>{d} — {tip}</title></rect>')
    out.append("</svg>")
    return "".join(out)


def mini_bars(history: list[dict], n: int = 6) -> str:
    """⑬ 트렌드 미니 막대 (템플릿 형식: 높이=점수/5, 색=점수 구간)."""
    bars = []
    for item in history[-n:]:
        s = item["score"]
        bars.append(f'<div class="trend-bar" style="height:{max(3, round(s / 5))}px;background:{score_color(s)};" '
                    f'title="{item["date"]}: {s}점"></div>')
    return "".join(bars)
