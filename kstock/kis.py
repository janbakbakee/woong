"""한국투자증권 Open API (조회 전용) + KIS 종목 마스터 파일.

명세 출처: github.com/koreainvestment/open-trading-api (examples_llm, stocks_info)
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
import zipfile
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

BASE = "https://openapi.koreainvestment.com:9443"
MASTER_URL = "https://new.real.download.dws.co.kr/common/master/{}_code.mst.zip"
TIMEOUT = 20
MIN_INTERVAL = 0.1   # 초당 10건 (문서상 한도 20건이지만 실측 0.07초 간격에서 한도 초과 발생)


@dataclass
class Stock:
    code: str
    name: str
    market: str        # KOSPI / KOSDAQ
    mcap: float        # 전일기준 시가총액 (억원)
    op_profit: float   # 영업이익 (마스터 기준)
    roe: float
    ok: bool           # 보통주 · 정상 거래 (관리/정지/경고/스팩/우선주 아님)
    prev_value: float  # 전일 거래대금 근사 (원) = 기준가 × 전일거래량
    sector: str = ""   # 지수업종 대분류 코드 (쏠림 진단용)


# 마스터 레코드 = 한글명 등 가변부 + 고정폭 꼬리(part2). 꼬리 길이와 앞쪽 필드 위치는 시장별로 다름.
# 시가총액·ROE·영업이익은 두 시장 모두 꼬리 끝에서 같은 위치.
_TAIL = {"KOSPI": 227, "KOSDAQ": 221}
_HEAD = {  # part2 시작 기준 (start, end)
    "KOSPI": {"grp": (0, 2), "spac": (29, 30), "price": (41, 50), "halt": (60, 61), "sltr": (61, 62),
              "mang": (62, 63), "warn": (63, 65), "prev_vol": (81, 93), "pref": (158, 159)},
    "KOSDAQ": {"grp": (0, 2), "spac": (24, 25), "price": (36, 45), "halt": (55, 56), "sltr": (56, 57),
               "mang": (57, 58), "warn": (58, 60), "prev_vol": (76, 88), "pref": (153, 154)},
}


def _num(s: str) -> float:
    try:
        return float(s.strip() or 0)
    except ValueError:
        return 0.0


def parse_master(text: str, market: str) -> list[Stock]:
    n, h = _TAIL[market], _HEAD[market]
    out = []
    for line in text.splitlines():
        if len(line) <= n:
            continue
        head, tail = line[:-n], line[-n:]
        f = {k: tail[a:b] for k, (a, b) in h.items()}
        ok = (f["grp"] == "ST" and f["spac"] != "Y" and f["halt"] != "Y" and f["sltr"] != "Y"
              and f["mang"] != "Y" and f["warn"].strip() in ("", "00", "01") and f["pref"].strip() in ("", "0"))
        out.append(Stock(
            code=head[0:9].strip(), name=head[21:].strip(), market=market,
            mcap=_num(tail[-15:-6]), op_profit=_num(tail[-55:-46]), roe=_num(tail[-32:-23]),
            ok=ok, prev_value=_num(f["price"]) * _num(f["prev_vol"]), sector=tail[3:7].strip(),
        ))
    return out


def load_master() -> list[Stock]:
    stocks = []
    for market in ("KOSPI", "KOSDAQ"):
        resp = requests.get(MASTER_URL.format(market.lower()), timeout=60)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            text = z.read(z.namelist()[0]).decode("cp949", errors="replace")
        stocks += parse_master(text, market)
    log.info("마스터 %d종목", len(stocks))
    return stocks


class KIS:
    def __init__(self, app_key: str | None = None, app_secret: str | None = None):
        self.key = app_key or os.environ["KIS_APP_KEY"]
        self.secret = app_secret or os.environ["KIS_APP_SECRET"]
        self.s = requests.Session()
        self._last = 0.0
        self._lock = threading.Lock()
        resp = self.s.post(f"{BASE}/oauth2/tokenP", timeout=TIMEOUT, json={
            "grant_type": "client_credentials", "appkey": self.key, "appsecret": self.secret})
        resp.raise_for_status()
        self.token = resp.json()["access_token"]

    def get(self, path: str, tr_id: str, params: dict) -> dict:
        headers = {"authorization": f"Bearer {self.token}", "appkey": self.key, "appsecret": self.secret,
                   "tr_id": tr_id, "custtype": "P", "content-type": "application/json; charset=utf-8"}
        for attempt in range(8):
            with self._lock:  # 호출 시작 간격만 직렬화, 응답 대기는 병렬
                slot = max(time.monotonic(), self._last + MIN_INTERVAL)
                self._last = slot
            time.sleep(max(0.0, slot - time.monotonic()))
            body = self.s.get(BASE + path, headers=headers, params=params, timeout=TIMEOUT).json()
            if body.get("rt_cd") == "0":
                return body
            if body.get("msg_cd") == "EGW00201":  # 초당 거래건수 초과
                time.sleep(1 + attempt)  # 1+2+…+7초까지 대기
                continue
            raise RuntimeError(f"KIS {tr_id}: {body.get('msg_cd')} {body.get('msg1')}")
        raise RuntimeError(f"KIS {tr_id}: 호출 한도 초과 반복")

    def is_open(self, ymd: str) -> bool:
        """국내 개장일 여부 (KIS 권고: 1일 1회 수준 호출)."""
        body = self.get("/uapi/domestic-stock/v1/quotations/chk-holiday", "CTCA0903R",
                        {"BASS_DT": ymd, "CTX_AREA_NK": "", "CTX_AREA_FK": ""})
        for row in body.get("output", []):
            if row.get("bass_dt") == ymd:
                return row.get("opnd_yn") == "Y"
        return True

    def investor_daily(self, code: str, ymd: str) -> list[dict]:
        """종목별 투자자매매동향(일별): 시세 + 외국인/기관/연기금/투신/사모 순매수, 최신일 먼저."""
        body = self.get("/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily", "FHPTJ04160001", {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": ymd,
            "FID_ORG_ADJ_PRC": "", "FID_ETC_CLS_CODE": ""})
        # 휴장일 등 종가 0인 행은 버린다
        rows = [r for r in body.get("output2", []) if r.get("stck_bsop_date") and float(r.get("stck_clpr") or 0) > 0]
        return sorted(rows, key=lambda r: r["stck_bsop_date"], reverse=True)

    def index_daily(self, code: str, start: str, end: str) -> dict[str, float]:
        """국내 업종지수 일봉 {날짜: 종가}. code: 0001 코스피, 1001 코스닥."""
        body = self.get("/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice", "FHKUP03500100", {
            "FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end, "FID_PERIOD_DIV_CODE": "D"})
        return {r["stck_bsop_date"]: float(r["bstp_nmix_prpr"]) for r in body.get("output2", [])
                if r.get("stck_bsop_date") and float(r.get("bstp_nmix_prpr") or 0) > 0}

    def overseas_daily(self, mkt: str, code: str, start: str, end: str) -> dict[str, float]:
        """해외지수(N)·환율(X) 일봉 {날짜: 종가}."""
        body = self.get("/uapi/overseas-price/v1/quotations/inquire-daily-chartprice", "FHKST03030100", {
            "FID_COND_MRKT_DIV_CODE": mkt, "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end, "FID_PERIOD_DIV_CODE": "D"})
        return {r["stck_bsop_date"]: float(r["ovrs_nmix_prpr"]) for r in body.get("output2", [])
                if r.get("stck_bsop_date") and float(r.get("ovrs_nmix_prpr") or 0) > 0}
