"""국장 추천 웹페이지 (GitHub Pages: docs/kstock/).

장 마감 실행마다 docs/kstock/data/<기준일>.json 을 저장하고, 최근 KEEP 거래일만 남겨
날짜 탭 페이지(index.html = 최신, <기준일>.html)를 다시 만든다. 차트는 JS 없는 인라인 SVG.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from . import score as sc

KEEP = 10                       # 보관 거래일 (2주)
PARTS_MAX = {"수급": 35, "거래량·캔들": 25, "추세·위치": 25, "재무": 15}
UP, DOWN = "#d62d2d", "#1f5fd1"  # 한국식: 상승 빨강, 하락 파랑
WEEKDAY = "월화수목금토일"


def _f(r: dict, k: str) -> float:
    return sc._f(r, k)


def series(rows: list[dict]) -> list[dict]:
    """차트·표용 일별 데이터 (오래된 날 먼저). 금액은 억원."""
    out = []
    for r in reversed(rows):
        c = _f(r, "stck_clpr")
        out.append({"d": r["stck_bsop_date"], "o": _f(r, "stck_oprc"), "h": _f(r, "stck_hgpr"),
                    "l": _f(r, "stck_lwpr"), "c": c, "v": _f(r, "acml_vol"),
                    "f": round(_f(r, "frgn_ntby_qty") * c / sc.EOK, 1),
                    "i": round(sc._inst(r) * c / sc.EOK, 1),
                    "fund": round(_f(r, "fund_ntby_qty") * c / sc.EOK, 1),
                    "ivtr": round(_f(r, "ivtr_ntby_qty") * c / sc.EOK, 1)})
    return out


def opinion_summary(rows: list[dict], close: float) -> dict | None:
    """최근 리포트들의 평균 목표가와 최신 3건. 목표가 없는 행은 뺀다."""
    rows = [r for r in rows if _f(r, "hts_goal_prc") > 0]
    if not rows:
        return None
    avg = sum(_f(r, "hts_goal_prc") for r in rows) / len(rows)
    latest = [{"date": r["stck_bsop_date"], "broker": (r.get("mbcr_name") or "").strip(),
               "opinion": (r.get("invt_opnn") or "").strip(), "target": _f(r, "hts_goal_prc")} for r in rows[:3]]
    return {"n": len(rows), "avg": avg, "upside": (avg / close - 1) * 100 if close else 0,
            "from": rows[-1]["stck_bsop_date"], "to": rows[0]["stck_bsop_date"], "latest": latest}


def record(p: sc.Pick, rank: int) -> dict:
    s = p.stock
    return {
        "rank": rank, "code": s.code, "name": s.name, "market": s.market, "score": p.score, "parts": p.parts,
        "close": p.close, "chg": p.chg, "mcap": s.mcap, "op_profit": s.op_profit, "roe": s.roe,
        "frgn5": p.frgn5 / sc.EOK, "orgn5": p.orgn5 / sc.EOK, "quality5": p.quality5 / sc.EOK,
        "flow20_pct": p.flow20 / (s.mcap * sc.EOK) * 100 if s.mcap else 0,
        "streak": p.streak, "frgn_streak": p.frgn_streak, "fund_streak": p.fund_streak,
        "vol_ratio": p.vol_ratio, "clv": p.clv, "upper_wick": p.upper_wick, "disparity": p.disparity,
        "ext_atr": p.ext_atr, "rs20": p.rs20, "above_ma20": p.above_ma20, "ma_aligned": p.ma_aligned,
        "near_high": p.near_high, "news": p.news[:3], "flags": p.flags,
        "opinion": opinion_summary(p.opinions, p.close), "series": series(p.rows),
    }


def signal_record(x) -> dict:
    return {**{k: v for k, v in vars(x).items()}, "r1": x.r1, "r2": x.r2, "buy_max": x.buy_max,
            "gap_skip": x.gap_skip}


def save_day(root: Path, ref: str, picks: dict, ctx: dict, stats: dict, dart_on: bool,
             sigs: dict | None = None, passed: list | None = None) -> Path:
    """장 마감 기록. watch = 조건 통과 전체 (다음 날 장중 재판정 대상). 장중 예비 상자는 지운다."""
    data = {"date": ref, "dart": dart_on, "stats": stats, "markets": {
        m: {k: ctx[m][k] for k in ("close", "chg", "above_ma20", "light")}
        | {"picks": [record(p, i) for i, p in enumerate(picks[m], 1)],
           "signals": [signal_record(x) for x in (sigs or {}).get(m, [])]} for m in picks},
        "watch": [{"code": p.stock.code, "name": p.stock.name, "market": p.stock.market,
                   "sector": p.stock.sector} for p in passed or []]}
    path = root / "data" / f"{ref}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (root / "intraday.json").unlink(missing_ok=True)
    return path


def save_intraday(root: Path, today: str, time: str, sigs: dict) -> None:
    """장중 예비 신호 — 최신 탭 위 노란 상자. 다음 장 마감 기록이 생기면 사라진다."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "intraday.json").write_text(json.dumps(
        {"date": today, "time": time, "signals": {m: [signal_record(x) for x in v] for m, v in sigs.items()}},
        ensure_ascii=False), encoding="utf-8")


def load_days(root: Path) -> list[dict]:
    """최신 먼저. KEEP 거래일을 넘는 오래된 데이터·페이지는 지운다."""
    files = sorted((root / "data").glob("*.json"), reverse=True)
    for old in files[KEEP:]:
        old.unlink()
        (root / f"{old.stem}.html").unlink(missing_ok=True)
    return [json.loads(f.read_text(encoding="utf-8")) for f in files[:KEEP]]


def history(days: list[dict], market: str, idx: int = 0) -> dict:
    """days[idx] 기준: 종목별 최근 등장 횟수·첫 등장 대비 수익률, 직전일 TOP에서 빠진 종목."""
    window = days[idx:]
    seen = {}
    for d in reversed(window):  # 오래된 날부터
        for p in d["markets"].get(market, {}).get("picks", []):
            h = seen.setdefault(p["code"], {"count": 0, "first": d["date"], "first_close": p["close"]})
            h["count"] += 1
    today = {p["code"] for p in window[0]["markets"].get(market, {}).get("picks", [])} if window else set()
    prev = window[1]["markets"].get(market, {}).get("picks", []) if len(window) > 1 else []
    return {"seen": seen, "window": len(window), "exits": [p["name"] for p in prev if p["code"] not in today]}


# ── 차트 (인라인 SVG) ──
W, PAD = 640, 44


def chart(s: list[dict]) -> str:
    if len(s) < 2:
        return ""
    n = len(s)
    step = (W - PAD - 8) / n
    x = lambda i: PAD + step * (i + 0.5)
    hi, lo = max(r["h"] for r in s), min(r["l"] for r in s)
    py = lambda v: 10 + (hi - v) / (hi - lo or 1) * 170          # 가격 10~180
    vmax = max(r["v"] for r in s) or 1
    fs = s[-20:]
    fmax = max(max(abs(r["f"]), abs(r["i"])) for r in fs) or 1
    fy0 = 330
    out = [f'<svg viewBox="0 0 {W} 400" width="100%" role="img" aria-label="60일 캔들·거래량·수급 차트" '
           'style="font:10px sans-serif;background:#fff">']
    for v in (hi, (hi + lo) / 2, lo):
        out.append(f'<text x="2" y="{py(v) + 3:.0f}" fill="#888">{v:,.0f}</text>'
                   f'<line x1="{PAD}" x2="{W - 8}" y1="{py(v):.1f}" y2="{py(v):.1f}" stroke="#eee"/>')
    for i, r in enumerate(s):
        col = UP if r["c"] >= r["o"] else DOWN
        top, bot = py(max(r["o"], r["c"])), py(min(r["o"], r["c"]))
        out.append(f'<line x1="{x(i):.1f}" x2="{x(i):.1f}" y1="{py(r["h"]):.1f}" y2="{py(r["l"]):.1f}" stroke="{col}"/>'
                   f'<rect x="{x(i) - step * 0.35:.1f}" y="{top:.1f}" width="{step * 0.7:.1f}" '
                   f'height="{max(1, bot - top):.1f}" fill="{col}"/>')
        vh = r["v"] / vmax * 50                                   # 거래량 200~250
        out.append(f'<rect x="{x(i) - step * 0.35:.1f}" y="{250 - vh:.1f}" width="{step * 0.7:.1f}" '
                   f'height="{vh:.1f}" fill="{col}" opacity=".45"/>')
    # 20일선만 그린다 (60일치 데이터로는 60일선이 한 점뿐 — 정배열 여부는 지표 칸에 표시)
    pts = [f"{x(i):.1f},{py(sum(r['c'] for r in s[i - 19:i + 1]) / 20):.1f}" for i in range(19, n)]
    if len(pts) > 1:
        out.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="#e08a00" stroke-width="1.2"/>')
    off = n - len(fs)
    for j, r in enumerate(fs):                                    # 외인·기관 순매수 (최근 20일) 290~370
        cx = x(off + j)
        for dx, v, colr in ((-0.3, r["f"], "#1f5fd1"), (0.05, r["i"], "#e08a00")):
            h = abs(v) / fmax * 38
            out.append(f'<rect x="{cx + step * dx:.1f}" y="{fy0 - h if v > 0 else fy0:.1f}" '
                       f'width="{step * 0.28:.1f}" height="{h:.1f}" fill="{colr}"/>')
    out.append(f'<line x1="{PAD}" x2="{W - 8}" y1="{fy0}" y2="{fy0}" stroke="#bbb"/>'
               '<text x="2" y="212" fill="#888">거래량</text><text x="2" y="290" fill="#888">순매수(20일)</text>'
               f'<text x="{PAD}" y="395" fill="#1f5fd1">■ 외국인</text>'
               f'<text x="{PAD + 60}" y="395" fill="#e08a00">■ 기관(금투제외)</text>'
               f'<text x="{PAD + 170}" y="395" fill="#e08a00">— 20일선</text>'
               f'<text x="{W - 8}" y="395" fill="#888" text-anchor="end">{s[0]["d"][4:6]}/{s[0]["d"][6:]}'
               f' ~ {s[-1]["d"][4:6]}/{s[-1]["d"][6:]}</text></svg>')
    return "".join(out)


def _label(d: str) -> str:
    return f"{d[4:6]}/{d[6:]}({WEEKDAY[date(int(d[:4]), int(d[4:6]), int(d[6:])).weekday()]})"


def build(root: Path, url: str = "") -> None:
    days = load_days(root)
    env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=True)
    env.filters.update(eok=lambda v: f"{v:+,.0f}억", pct=lambda v: f"{v:+.1f}%", num=lambda v: f"{v:,.0f}",
                       label=_label)
    tpl = env.get_template("day.html.j2")
    tabs = [d["date"] for d in days]
    ipath = root / "intraday.json"
    intraday = json.loads(ipath.read_text(encoding="utf-8")) if ipath.exists() else None
    if intraday and days and intraday["date"] <= days[0]["date"]:
        intraday = None  # 그날 장 마감 기록이 이미 있으면 장중 상자는 의미 없음
    for i, d in enumerate(days):
        hist = {m: history(days, m, i) for m in d["markets"]}
        html = tpl.render(day=d, tabs=tabs, hist=hist, chart=chart, parts_max=PARTS_MAX, url=url,
                          intraday=intraday if i == 0 else None,
                          generated=datetime.now().strftime("%Y-%m-%d %H:%M"))
        (root / f"{d['date']}.html").write_text(html, encoding="utf-8")
        if i == 0:
            (root / "index.html").write_text(html, encoding="utf-8")
    (root / "guide.html").write_text(env.get_template("guide.html.j2").render(tabs=tabs), encoding="utf-8")
