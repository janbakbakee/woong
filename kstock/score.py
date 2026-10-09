"""수급·거래량·캔들·추세 지표 계산과 100점 점수 (네트워크 없음).

금액은 모두 '수량 × 당일 종가'(원)로 환산해 단위를 통일한다.
기관 = 기관계 − 금융투자(증권). 금융투자는 ETF LP·차익 헤지 물량이라 방향성이 없다.
기준값은 초기 가설이며, 후보 기록(CSV)이 쌓이면 검증해 다시 맞춘다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .kis import Stock

EOK = 1e8  # 1억원

# ── 1차 필터 ──
MIN_MCAP = {"KOSPI": 3000, "KOSDAQ": 2000}           # 시가총액 (억)
MIN_AVG_VALUE = 50 * EOK                              # 20일 평균 거래대금
MIN_FLOW5 = {"KOSPI": 50 * EOK, "KOSDAQ": 20 * EOK}  # 5일 외국인+기관(금투 제외) 합계
MAX_DAY_CHG = 15.0    # 당일 +15% 이상 제외 (추격)
MAX_5D_RET = 30.0     # 5일 +30% 이상 제외

# ── 점수 기준 ──
# 20일 외국인+기관 누적 ÷ 시총 만점 기준 (%) — (시총 하한(억), 기준). 절반 이상이면 중간 점수
CAP_FULL = ((100_000, 0.3), (20_000, 0.6), (0, 1.0))
NEWS_PENALTY, NEWS_PENALTY_MAX = 5, 15   # 악재 헤드라인 1건당 / 최대 감점 (뉴스 없음 = 0, 가점 없음)


def _f(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except ValueError:
        return 0.0


def _streak(rows: list[dict], *keys: str) -> int:
    n = 0
    for r in rows:
        if all(_f(r, k) > 0 for k in keys):
            n += 1
        else:
            break
    return n


def _inst(r: dict) -> float:
    """기관(금융투자 제외) 순매수 수량."""
    return _f(r, "orgn_ntby_qty") - _f(r, "scrt_ntby_qty")


@dataclass
class Pick:
    stock: Stock
    date: str
    close: float
    chg: float             # 당일 등락률 %
    frgn5: float           # 5일 외국인 순매수 (원)
    orgn5: float           # 5일 기관(금투 제외) 순매수 (원)
    quality5: float        # 5일 연기금+투신+사모 순매수 (원)
    flow20: float          # 20일 외국인+기관(금투 제외) 순매수 (원)
    streak: int            # 외국인·기관 동시 순매수 연속일
    frgn_streak: int
    fund_streak: int       # 연기금 연속 순매수일
    vol_ratio: float       # 당일 거래량 / 직전 20일 평균
    avg_value: float       # 20일 평균 거래대금 (원)
    clv: float             # 종가 위치 (0=저가, 1=고가)
    upper_wick: float      # 윗꼬리 / 당일 범위
    above_ma20: bool
    ma_aligned: bool       # MA20 > MA60 (데이터 부족 시 가용 일수 기준)
    near_high: bool        # 종가가 60일 고점의 95% 이상
    disparity: float       # 종가 / MA20 − 1 (%)
    ext_atr: float         # (종가 − MA20) / ATR14 — 변동성 대비 이격
    rs20: float | None     # 20일 수익률 − 지수 20일 수익률 (%p), 지수 없으면 None
    score: int = 0
    parts: dict = field(default_factory=dict)
    news: list = field(default_factory=list)   # [(title, url)]
    flags: list = field(default_factory=list)  # 악재 헤드라인
    opinions: list = field(default_factory=list)  # 증권사 투자의견 원본 행 (웹페이지용)
    rows: list = field(default_factory=list, repr=False)  # 일별 원본 (웹페이지 차트·표용, 최신 먼저)

    @property
    def flow5(self) -> float:
        return self.frgn5 + self.orgn5


def analyze(stock: Stock, rows: list[dict], index: dict[str, float] | None = None) -> Pick | None:
    """rows: 최신일 먼저. index: {날짜: 지수 종가}. 필터 탈락 시 None."""
    if len(rows) < 21:
        return None
    close = [_f(r, "stck_clpr") for r in rows]
    vol = [_f(r, "acml_vol") for r in rows]
    if min(close) <= 0:
        return None

    def amt(r, qty):
        return qty * _f(r, "stck_clpr")

    avg_value = sum(v * c for v, c in zip(vol[:20], close[:20])) / 20
    chg = _f(rows[0], "prdy_ctrt")
    ret5 = (close[0] / close[5] - 1) * 100
    frgn5 = sum(amt(r, _f(r, "frgn_ntby_qty")) for r in rows[:5])
    orgn5 = sum(amt(r, _inst(r)) for r in rows[:5])

    if (stock.mcap < MIN_MCAP[stock.market] or avg_value < MIN_AVG_VALUE or chg >= MAX_DAY_CHG
            or ret5 >= MAX_5D_RET or frgn5 <= 0 or orgn5 <= 0 or frgn5 + orgn5 < MIN_FLOW5[stock.market]):
        return None

    hi, lo, op = (_f(rows[0], k) for k in ("stck_hgpr", "stck_lwpr", "stck_oprc"))
    rng = hi - lo
    ma20 = sum(close[:20]) / 20
    ma60 = sum(close[:60]) / len(close[:60])
    # ATR14: 전일 종가 포함 진폭 평균
    trs = [max(_f(r, "stck_hgpr"), close[i + 1]) - min(_f(r, "stck_lwpr"), close[i + 1])
           for i, r in enumerate(rows[:14])]
    atr = sum(trs) / len(trs)
    avg_vol20 = sum(vol[1:21]) / 20
    d0, d20 = rows[0]["stck_bsop_date"], rows[20]["stck_bsop_date"]
    rs20 = None
    if index and d0 in index and d20 in index:
        rs20 = ((close[0] / close[20]) - (index[d0] / index[d20])) * 100

    return Pick(
        stock=stock, date=d0, close=close[0], chg=chg, frgn5=frgn5, orgn5=orgn5,
        quality5=sum(amt(r, _f(r, k)) for r in rows[:5] for k in ("fund_ntby_qty", "ivtr_ntby_qty", "pe_fund_ntby_vol")),
        flow20=sum(amt(r, _f(r, "frgn_ntby_qty") + _inst(r)) for r in rows[:20]),
        streak=next((i for i, r in enumerate(rows) if not (_f(r, "frgn_ntby_qty") > 0 and _inst(r) > 0)), len(rows)),
        frgn_streak=_streak(rows, "frgn_ntby_qty"), fund_streak=_streak(rows, "fund_ntby_qty"),
        vol_ratio=vol[0] / avg_vol20 if avg_vol20 else 0, avg_value=avg_value,
        clv=(close[0] - lo) / rng if rng > 0 else 0.5,
        upper_wick=(hi - max(op, close[0])) / rng if rng > 0 else 0.0,
        above_ma20=close[0] > ma20, ma_aligned=ma20 > ma60,
        near_high=close[0] >= 0.95 * max(_f(r, "stck_hgpr") for r in rows[:60]),
        disparity=(close[0] / ma20 - 1) * 100, ext_atr=(close[0] - ma20) / atr if atr > 0 else 0.0, rs20=rs20,
        rows=rows[:60],
    )


def score(p: Pick) -> Pick:
    # ① 수급 35 (기관은 금융투자 제외)
    r5 = p.flow5 / p.avg_value
    s_amt = 12 if r5 >= 1 else 8 if r5 >= 0.5 else 4 if r5 >= 0.2 else 0
    r20 = p.flow20 / (p.stock.mcap * EOK) * 100
    full = next(t for cap, t in CAP_FULL if p.stock.mcap >= cap)  # 대형주일수록 시총 대비 기준을 낮춤
    s_cap = 10 if r20 >= full else 6 if r20 >= full / 2 else 2 if r20 > 0 else 0
    s_streak = 8 if 3 <= p.streak <= 10 else 4 if p.streak else 0
    s_quality = 5 if p.orgn5 > 0 and p.quality5 >= 0.5 * p.orgn5 else 0
    # ② 거래량·캔들 25 — 거래량은 종가 위치·윗꼬리와 함께 봐야 '질'이 보인다
    v = p.vol_ratio
    s_vol = 10 if 1.2 <= v < 3 else 6 if 3 <= v < 5 else 2 if v >= 5 else 3
    s_clv = 8 if p.clv >= 0.7 else 4 if p.clv >= 0.4 else 0
    s_wick = 7 if p.upper_wick < 0.25 else 3 if p.upper_wick < 0.5 else 0
    # ③ 추세·위치·상대강도 25 — 이격이 클수록 추격 위험
    s_trend = 4 * p.above_ma20 + 4 * p.ma_aligned + 4 * p.near_high
    s_ext = 5 if p.ext_atr <= 2 else 2 if p.ext_atr <= 3.5 else 0
    s_rs = 0 if p.rs20 is None else 8 if p.rs20 >= 10 else 5 if p.rs20 >= 3 else 2 if p.rs20 > 0 else 0
    # ④ 재무 15 (KIS 마스터 기준 — 최소 안정성)
    s_fin = (6 if p.stock.op_profit > 0 else 0) + (9 if p.stock.roe >= 10 else 5 if p.stock.roe >= 5 else 0)
    # 뉴스: 감점 전용 (DART 중대 악재는 main에서 아예 제외)
    s_news = -min(NEWS_PENALTY_MAX, NEWS_PENALTY * len(p.flags))

    p.parts = {"수급": s_amt + s_cap + s_streak + s_quality, "거래량·캔들": s_vol + s_clv + s_wick,
               "추세·위치": s_trend + s_ext + s_rs, "재무": s_fin}
    if s_news:
        p.parts["뉴스"] = s_news
    p.score = max(0, sum(p.parts.values()))
    return p
