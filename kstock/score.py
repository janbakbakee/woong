"""수급·거래량·재무 지표 계산과 100점 점수 (네트워크 없음).

금액은 모두 '수량 × 당일 종가'(원)로 환산해 단위를 통일한다.
기준값은 시작점이며, 추천 기록이 쌓이면 다시 맞춘다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .kis import Stock

EOK = 1e8  # 1억원

# 후보 필터
MIN_MCAP = {"KOSPI": 3000, "KOSDAQ": 2000}      # 시가총액 (억)
MIN_AVG_VALUE = 50 * EOK                         # 20일 평균 거래대금
MIN_FLOW5 = {"KOSPI": 50 * EOK, "KOSDAQ": 20 * EOK}  # 5일 외국인+기관 누적 순매수 (잡음 제거)
MAX_DAY_CHG = 15.0    # 당일 +15% 이상은 추격 매수로 제외
MAX_5D_RET = 30.0     # 5일 +30% 이상 제외
# 20일 외국인+기관 누적 ÷ 시총 만점 기준 (%) — (시총 하한(억), 기준). 절반이면 6점
CAP_FULL = ((100_000, 0.3), (20_000, 0.6), (0, 1.0))


def _f(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except ValueError:
        return 0.0


@dataclass
class Pick:
    stock: Stock
    date: str
    close: float
    chg: float             # 당일 등락률 %
    frgn5: float           # 5일 외국인 순매수 (원)
    orgn5: float           # 5일 기관 순매수 (원)
    quality5: float        # 5일 연기금+투신+사모 순매수 (원)
    flow20: float          # 20일 외국인+기관 순매수 (원)
    streak: int            # 외국인·기관 동시 순매수 연속일
    vol_ratio: float       # 당일 거래량 / 20일 평균
    avg_value: float       # 20일 평균 거래대금 (원)
    above_ma20: bool
    ma_aligned: bool       # MA20 > MA60 (데이터 부족 시 가용 일수 기준)
    near_high: bool        # 종가가 기간 고점의 95% 이상
    score: int = 0
    parts: dict = field(default_factory=dict)
    news: list = field(default_factory=list)   # [(title, url)]
    flags: list = field(default_factory=list)  # 감점 사유

    @property
    def flow5(self) -> float:
        return self.frgn5 + self.orgn5


def analyze(stock: Stock, rows: list[dict]) -> Pick | None:
    """rows: 최신일 먼저. 필터 탈락 시 None."""
    if len(rows) < 10:
        return None
    close = [_f(r, "stck_clpr") for r in rows]
    vol = [_f(r, "acml_vol") for r in rows]
    if min(close) <= 0:
        return None

    def amt(r, k):
        return _f(r, k) * _f(r, "stck_clpr")

    n20 = rows[:20]
    avg_value = sum(v * c for v, c in zip(vol[:20], close[:20])) / len(n20)
    chg = _f(rows[0], "prdy_ctrt")
    ret5 = (close[0] / close[min(5, len(close) - 1)] - 1) * 100
    frgn5 = sum(amt(r, "frgn_ntby_qty") for r in rows[:5])
    orgn5 = sum(amt(r, "orgn_ntby_qty") for r in rows[:5])

    if (stock.mcap < MIN_MCAP[stock.market] or avg_value < MIN_AVG_VALUE or chg >= MAX_DAY_CHG
            or ret5 >= MAX_5D_RET or frgn5 <= 0 or orgn5 <= 0 or frgn5 + orgn5 < MIN_FLOW5[stock.market]):
        return None

    streak = 0
    for r in rows:
        if _f(r, "frgn_ntby_qty") > 0 and _f(r, "orgn_ntby_qty") > 0:
            streak += 1
        else:
            break

    ma20 = sum(close[:20]) / len(close[:20])
    ma60 = sum(close[:60]) / len(close[:60])
    avg_vol20 = sum(vol[1:21]) / max(1, len(vol[1:21]))
    highs = [_f(r, "stck_hgpr") for r in rows[:60]]
    return Pick(
        stock=stock, date=rows[0]["stck_bsop_date"], close=close[0], chg=chg,
        frgn5=frgn5, orgn5=orgn5,
        quality5=sum(amt(r, k) for r in rows[:5] for k in ("fund_ntby_qty", "ivtr_ntby_qty", "pe_fund_ntby_vol")),
        flow20=sum(amt(r, "frgn_ntby_qty") + amt(r, "orgn_ntby_qty") for r in n20),
        streak=streak, vol_ratio=vol[0] / avg_vol20 if avg_vol20 else 0, avg_value=avg_value,
        above_ma20=close[0] > ma20, ma_aligned=ma20 > ma60, near_high=close[0] >= 0.95 * max(highs),
    )


def score(p: Pick) -> Pick:
    # ① 수급 40
    r5 = p.flow5 / p.avg_value
    s_amt = 15 if r5 >= 1 else 10 if r5 >= 0.5 else 5 if r5 >= 0.2 else 0
    r20 = p.flow20 / (p.stock.mcap * EOK) * 100
    full = next(t for cap, t in CAP_FULL if p.stock.mcap >= cap)  # 대형주일수록 시총 대비 기준을 낮춤
    s_cap = 10 if r20 >= full else 6 if r20 >= full / 2 else 2 if r20 > 0 else 0
    s_streak = 10 if 3 <= p.streak <= 10 else 5 if p.streak in (1, 2) else 4 if p.streak > 10 else 0
    s_quality = 5 if p.orgn5 > 0 and p.quality5 >= 0.5 * p.orgn5 else 0
    # ② 거래량·가격 25
    v = p.vol_ratio
    s_vol = 10 if 1.2 <= v < 3 else 6 if 3 <= v < 5 else 2 if v >= 5 else 3
    s_trend = 5 * p.above_ma20 + 5 * p.ma_aligned + 5 * p.near_high
    # ③ 재무 20 (KIS 마스터 기준 실적)
    s_fin = (8 if p.stock.op_profit > 0 else 0) + (12 if p.stock.roe >= 10 else 6 if p.stock.roe >= 5 else 0)
    # ④ 뉴스·공시 15 (악재 헤드라인 1건당 -5)
    s_news = max(0, 15 - 5 * len(p.flags))

    p.parts = {"수급": s_amt + s_cap + s_streak + s_quality, "거래량·추세": s_vol + s_trend,
               "재무": s_fin, "뉴스": s_news}
    p.score = sum(p.parts.values())
    return p
