"""포지션 지표 계산 (모든 수치는 CFTC 원본에서 결정론적으로 계산)."""
from __future__ import annotations

import math
import statistics
from datetime import date

from . import config

WEEKS_3Y = 156
WEEKS_5Y = 260
CHART_WEEKS = 156


def net(row: dict, g: str) -> int | None:
    lo, sh = row.get(f"{g}_long"), row.get(f"{g}_short")
    if lo is None or sh is None:
        return None
    return lo - sh


def diagnose(long_chg: int, short_chg: int) -> str:
    """전주 대비 변화 진단: 순포지션 변화를 만든 주된 요인."""
    net_chg = long_chg - short_chg
    if net_chg > 0:
        return "신규 Long 구축" if long_chg >= -short_chg else "Short Covering"
    if net_chg < 0:
        return "신규 Short 구축" if short_chg >= -long_chg else "Long 청산"
    return "변화 없음"


def percentile_rank(values: list[float], current: float) -> float:
    if not values:
        return float("nan")
    return 100.0 * sum(1 for v in values if v <= current) / len(values)


def cot_index(values: list[float], current: float) -> float:
    lo, hi = min(values), max(values)
    if hi == lo:
        return 50.0
    return 100.0 * (current - lo) / (hi - lo)


def _sgn_points(chg: int | None, full: float) -> float:
    if chg is None:
        return full / 2
    return full if chg > 0 else (full / 2 if chg == 0 else 0.0)


def quant_score(rows: list[dict], i: int, headline: str, groups: tuple) -> dict | None:
    """퀀트 점수 (100점): 방향성 40 + 기관 일치도 30 + 추세 강도 30.

    - 방향성: 헤드라인 그룹 순포지션의 3년 백분위 × 0.4
    - 일치도: 2개 그룹 → 각 그룹의 주간 순변화가 강세 방향이면 15점씩
              1개 그룹 → 최근 4주 중 순포지션이 늘어난 주의 비율 × 30
    - 추세:   4주 순변화를 3년 4주 변화 표준편차로 나눈 z → 15 + 15·tanh(z/1.5)
    """
    if i < 5:
        return None
    nets = [net(r, headline) for r in rows[: i + 1]]
    if any(v is None for v in nets[-5:]):
        return None
    window = [v for v in nets[-WEEKS_3Y:] if v is not None]
    direction = 0.4 * percentile_rank(window, nets[-1])

    if len(groups) >= 2:
        agreement = 0.0
        for g in groups[:2]:
            a, b = net(rows[i - 1], g), net(rows[i], g)
            agreement += _sgn_points(None if a is None or b is None else b - a, 15.0)
    else:
        ups = sum(1 for k in range(1, 5) if nets[-k] > nets[-k - 1])
        agreement = 30.0 * ups / 4

    chg4 = [nets[k] - nets[k - 4] for k in range(max(4, len(nets) - WEEKS_3Y), len(nets))
            if nets[k] is not None and nets[k - 4] is not None]
    sd = statistics.pstdev(chg4) if len(chg4) > 2 else 0
    z = (nets[-1] - nets[-5]) / sd if sd else 0.0
    trend = 15 + 15 * math.tanh(z / 1.5)
    total = direction + agreement + trend
    return {"total": round(total), "direction": round(direction, 1),
            "agreement": round(agreement, 1), "trend": round(trend, 1), "trend_z": round(z, 2)}


def _streak(nets: list[int]) -> int:
    """현재 주의 순변화 방향이 몇 주 연속인지."""
    if len(nets) < 2:
        return 0
    sign = (nets[-1] > nets[-2]) - (nets[-1] < nets[-2])
    if sign == 0:
        return 0
    n = 0
    for k in range(len(nets) - 1, 0, -1):
        s = (nets[k] > nets[k - 1]) - (nets[k] < nets[k - 1])
        if s != sign:
            break
        n += 1
    return n


def market_metrics(mkt: config.Market, series: dict, target: date) -> dict | None:
    rows = [r for r in series["rows"] if r["date"] <= target]
    if len(rows) < 2 or rows[-1]["date"] != target:
        return None
    cur, prev = rows[-1], rows[-2]
    i = len(rows) - 1

    groups = {}
    for g in dict.fromkeys(mkt.groups + (mkt.headline,)):
        n_cur, n_prev = net(cur, g), net(prev, g)
        if n_cur is None or n_prev is None:
            continue
        long_chg = cur[f"{g}_long"] - prev[f"{g}_long"]
        short_chg = cur[f"{g}_short"] - prev[f"{g}_short"]
        gnets = [net(r, g) for r in rows if net(r, g) is not None]
        groups[g] = {
            "label": config.GROUP_LABEL[g],
            "long": cur[f"{g}_long"], "short": cur[f"{g}_short"],
            "net": n_cur, "net_prev": n_prev, "net_chg": n_cur - n_prev,
            "long_chg": long_chg, "short_chg": short_chg,
            "diag": diagnose(long_chg, short_chg),
            "streak": _streak(gnets),
            "pct_3y": round(percentile_rank(gnets[-WEEKS_3Y:], n_cur)),
        }
    h = groups.get(mkt.headline)
    if h is None:
        return None

    hnets = [net(r, mkt.headline) for r in rows]
    hnets = [v for v in hnets if v is not None]
    w3, w5 = hnets[-WEEKS_3Y:], hnets[-WEEKS_5Y:]
    weekly_chg = [hnets[k] - hnets[k - 1] for k in range(max(1, len(hnets) - WEEKS_3Y), len(hnets))]
    sd = statistics.pstdev(weekly_chg) if len(weekly_chg) > 2 else 0
    prior_4w = hnets[-5] if len(hnets) >= 5 else hnets[0]

    if len(mkt.groups) >= 2 and all(g in groups for g in mkt.groups[:2]):
        a, b = (groups[g]["net_chg"] for g in mkt.groups[:2])
        if a > 0 and b > 0:
            agreement = "일치(개선)"
        elif a < 0 and b < 0:
            agreement = "일치(악화)"
        elif a == 0 or b == 0:
            agreement = "보합"
        else:
            agreement = "불일치"
    else:
        agreement = "단일그룹"

    quant_hist = []
    for k in range(max(5, i - 11), i + 1):
        q = quant_score(rows, k, mkt.headline, mkt.groups)
        if q:
            quant_hist.append({"date": rows[k]["date"].isoformat(), "score": q["total"]})

    chart_rows = rows[-CHART_WEEKS:]
    chart = {"dates": [r["date"].isoformat() for r in chart_rows]}
    for g in mkt.groups:
        chart[g] = [net(r, g) for r in chart_rows]

    oi_prev = prev.get("oi")
    return {
        "key": mkt.key, "name": mkt.name, "code": series["code"],
        "market_name": series["name"], "report": mkt.report,
        "date": target.isoformat(), "prev_date": prev["date"].isoformat(),
        "headline": mkt.headline, "headline_label": config.GROUP_LABEL[mkt.headline],
        "groups": groups,
        "net": h["net"], "net_prev": h["net_prev"], "net_chg": h["net_chg"],
        "diag": h["diag"], "streak": h["streak"],
        "pct_3y": round(percentile_rank(w3, h["net"])),
        "pct_5y": round(percentile_rank(w5, h["net"])),
        "pct_3y_4w_ago": round(percentile_rank(w3[:-4] or w3, prior_4w)),
        "cot_idx_26w": round(cot_index(hnets[-26:], h["net"])),
        "cot_idx_156w": round(cot_index(w3, h["net"])),
        "min_3y": min(w3), "max_3y": max(w3),
        "history_weeks": len(hnets),
        "chg_z": round(h["net_chg"] / sd, 2) if sd else 0.0,
        "oi": cur.get("oi"), "oi_chg": (cur["oi"] - oi_prev) if cur.get("oi") and oi_prev else None,
        "agreement": agreement,
        "quant": quant_score(rows, i, mkt.headline, mkt.groups),
        "quant_history": quant_hist,
        "chart": chart,
    }


def extreme_label(pct: float) -> str:
    if pct >= 90:
        return "극단적 롱 (과매수)"
    if pct >= 80:
        return "롱 과열권"
    if pct <= 10:
        return "극단적 숏 (과매도)"
    if pct <= 20:
        return "숏 과열권"
    return "평균권"
