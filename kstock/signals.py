"""진입 신호 (셋업 A 눌림목 / B 박스 돌파)와 위험 기반 비중 (네트워크 없음).

기존 100점 점수(기준 모델)는 그대로 두고, 그 위에 '언제·어디서 사고, 어디서 틀렸다고 인정하나'만 얹는다.
모든 수치는 검증 전 초기 가설 — 신호 기록(아티팩트)으로 사후 검증한다.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import score as sc

# ── 위험 관리 (소액·2~3종목, 초보 초기값) ──
RISK_PCT = 0.75         # 거래당 계좌 위험 % (🟡 시장은 절반)
MAX_WEIGHT = 33.0       # 종목당 최대 계좌 비중 %
MAX_STOP_PCT = 8.0      # 손절폭이 이보다 넓으면 셋업 무효 (손익비 안 나옴)
# ── 셋업 기준 ──
A_TOUCH_ATR = 0.5       # 최근 3일 저가가 MA20 + 0.5ATR 이내로 내려옴
A_DRY_VOL = 0.8         # 눌림 3일 평균 거래량 ≤ 20일 평균 × 0.8
A_CLV = 0.6             # 반등 확인: 종가 위치 ≥ 60% 또는 전일 고가 돌파
B_BOX = 0.20            # 직전 20일(돌파일 제외) 고저폭 ≤ 20%
B_VOL = 1.5             # 돌파일 거래량 ≥ 20일 평균 × 1.5 (5배↑는 이벤트 확인 경고)
B_CLV, B_WICK = 0.7, 0.25
B_PRE_EXT = 2.5         # 돌파 전일 (종가−MA20)/ATR ≤ 2.5
GAP_SKIP = {"A": 3.0, "B": 2.0}   # 다음 날 시초가가 신호가 대비 +N% 이상이면 패스
BUY_RANGE = {"A": 1.0, "B": 2.0}  # 다음 날 매수 허용 구간 (신호가 대비 +N%)

_f = sc._f


@dataclass
class Signal:
    code: str
    name: str
    market: str
    setup: str         # "A" 눌림 / "B" 돌파
    grade: str         # "A" / "B"
    entry: float       # 신호가 (장 마감=종가, 장중=현재가)
    stop: float
    stop_pct: float
    weight: float      # 계좌 대비 비중 % (위험 기반, 상한 적용)
    risk_pct: float
    notes: list
    vol_ratio: float = 0.0

    @property
    def r1(self) -> float:
        return self.entry + (self.entry - self.stop)

    @property
    def r2(self) -> float:
        return self.entry + 2 * (self.entry - self.stop)

    @property
    def buy_max(self) -> float:
        return self.entry * (1 + BUY_RANGE[self.setup] / 100)

    @property
    def gap_skip(self) -> float:
        return self.entry * (1 + GAP_SKIP[self.setup] / 100)

    def shares(self, capital: float) -> int:
        return int(capital * self.weight / 100 // self.entry) if capital else 0


def _ma(vals: list[float], n: int, start: int = 0) -> float:
    w = vals[start:start + n]
    return sum(w) / len(w)


def evaluate(stock, rows: list[dict], light: str, rs20: float | None, flags: list, vol_scale: float = 1.0) -> Signal | None:
    """rows: 최신일(=신호일) 먼저, 최소 26일. vol_scale: 장중이면 당일 거래량을 하루치로 환산하는 배수.
    🔴 시장·악재·데이터 부족이면 신호 없음."""
    if light == "🔴" or flags or len(rows) < 26:
        return None
    c = [_f(r, "stck_clpr") for r in rows]
    if min(c) <= 0:
        return None
    h = [_f(r, "stck_hgpr") for r in rows]
    lo = [_f(r, "stck_lwpr") for r in rows]
    v = [_f(r, "acml_vol") for r in rows]
    v[0] *= vol_scale
    today = rows[0]
    rng = h[0] - lo[0]
    clv = (c[0] - lo[0]) / rng if rng > 0 else 0.5
    wick = (h[0] - max(_f(today, "stck_oprc"), c[0])) / rng if rng > 0 else 0.0
    atr = sum(max(h[i], c[i + 1]) - min(lo[i], c[i + 1]) for i in range(14)) / 14
    ma20, ma20_5 = _ma(c, 20), _ma(c, 20, 5)
    ma60 = _ma(c, 60)
    avg_vol = _ma(v, 20, 1)                   # 신호일 제외 직전 20일
    vr = v[0] / avg_vol if avg_vol else 0
    flow = lambda r: _f(r, "frgn_ntby_qty") + sc._inst(r)
    if atr <= 0:
        return None

    setup, stop, notes = None, 0.0, []
    # 🅐 눌림목: 상승 중인 20일선까지 거래량 줄며 쉬었다가 반등
    if (ma20 > ma20_5 and ma20 > ma60 and c[0] >= ma20
            and min(lo[:3]) <= ma20 + A_TOUCH_ATR * atr
            and _ma(v, 3, 1) <= A_DRY_VOL * avg_vol
            and sum(flow(r) for r in rows[:3]) >= 0
            and (clv >= A_CLV or c[0] > h[1])):
        setup, stop = "A", min(lo[:3]) - 0.3 * atr
        notes.append(f"20일선 눌림 후 반등 · 눌림 거래량 {_ma(v, 3, 1) / avg_vol:.1f}배")
    # 🅑 박스 돌파: 돌파일을 뺀 직전 20일 박스 고점을 종가로 돌파
    else:
        box_hi, box_lo = max(h[1:21]), min(lo[1:21])
        prev_ext = (c[1] - _ma(c, 20, 1)) / atr
        if (box_hi / box_lo - 1 <= B_BOX and c[0] > box_hi and vr >= B_VOL and clv >= B_CLV and wick < B_WICK
                and _f(today, "frgn_ntby_qty") > 0 and sc._inst(today) > 0 and prev_ext <= B_PRE_EXT):
            setup, stop = "B", max(lo[0], box_hi - atr)
            notes.append(f"20일 박스({box_hi / box_lo - 1:.0%}) 돌파 · 거래량 {vr:.1f}배")
            if vr >= 5:
                notes.append("⚠️ 거래량 5배↑ — 이벤트·과열 여부 확인")
    if not setup:
        return None

    stop_pct = (c[0] - stop) / c[0] * 100
    if stop_pct <= 0 or stop_pct > MAX_STOP_PCT:
        return None
    # 등급은 종목 강도만 (지수 대비 +3%p↑). 시장 상황은 아래에서 따로: 🟡는 A급만·위험 절반
    grade = "A" if rs20 is not None and rs20 >= 3 else "B"
    if light == "🟡" and grade != "A":
        return None  # 🟡 시장은 A급만
    risk = RISK_PCT / 2 if light == "🟡" else RISK_PCT
    return Signal(stock.code, stock.name, stock.market, setup, grade, c[0], stop, stop_pct,
                  min(MAX_WEIGHT, risk / stop_pct * 100), risk, notes, vr)
