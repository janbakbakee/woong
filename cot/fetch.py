"""CFTC COT 데이터 수집.

1순위: CFTC 공개 API (publicreporting.cftc.gov, Socrata)
2순위: CFTC 연도별 히스토리 압축파일 (cftc.gov/files/dea/history)

두 소스 모두 컬럼명이 조금씩 달라서(대소문자, _all 접미사)
정규식으로 필드를 찾아 공통 레코드 형식으로 변환한다.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from datetime import date, datetime, timedelta

import requests

from . import config

log = logging.getLogger(__name__)

TIMEOUT = 60
HEADERS = {"User-Agent": "cot-weekly-report/1.0 (github actions)"}

FIELD_PATTERNS = {
    "date": r"^report_date_as_yyyy_mm_dd$",
    "code": r"^cftc_contract_market_code$",
    "name": r"^market_and_exchange_names$",
    "oi": r"^open_interest_all$",
    "dealer_long": r"^dealer_positions_long(_all)?$",
    "dealer_short": r"^dealer_positions_short(_all)?$",
    "am_long": r"^asset_mgr_positions_long(_all)?$",
    "am_short": r"^asset_mgr_positions_short(_all)?$",
    "lev_long": r"^lev_money_positions_long(_all)?$",
    "lev_short": r"^lev_money_positions_short(_all)?$",
    "mm_long": r"^m_money_positions_long(_all)?$",
    "mm_short": r"^m_money_positions_short(_all)?$",
}
NUMERIC = [k for k in FIELD_PATTERNS if k not in ("date", "code", "name")]


class FetchError(RuntimeError):
    pass


def _norm_key(k: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", k.strip().lower()).strip("_")


def _parse_date(v: str) -> date:
    v = v.strip()[:10]
    return datetime.strptime(v, "%Y-%m-%d").date()


def _to_int(v) -> int | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "."):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def normalize_rows(raw_rows: list[dict]) -> list[dict]:
    """임의 소스의 행 목록을 공통 레코드 목록으로 변환."""
    if not raw_rows:
        return []
    keys = {_norm_key(k): k for k in raw_rows[0].keys()}
    fmap = {}
    for field, pat in FIELD_PATTERNS.items():
        for nk, orig in keys.items():
            if re.match(pat, nk):
                fmap[field] = orig
                break
    for required in ("date", "code"):
        if required not in fmap:
            raise FetchError(f"필수 컬럼 '{required}'을(를) 찾을 수 없음: {list(keys)[:15]}")
    out = []
    for r in raw_rows:
        try:
            rec = {
                "date": _parse_date(r[fmap["date"]]),
                "code": str(r[fmap["code"]]).strip(),
                "name": str(r.get(fmap.get("name", ""), "")).strip(),
            }
        except (ValueError, KeyError):
            continue
        for f in NUMERIC:
            rec[f] = _to_int(r.get(fmap[f])) if f in fmap else None
        out.append(rec)
    return out


def _fetch_socrata(dataset: str, codes: list[str], since: date) -> list[dict]:
    code_list = ",".join(f"'{c}'" for c in codes)
    params = {
        "$where": (f"cftc_contract_market_code in({code_list}) "
                   f"AND report_date_as_yyyy_mm_dd >= '{since.isoformat()}T00:00:00'"),
        "$order": "report_date_as_yyyy_mm_dd ASC",
        "$limit": "50000",
    }
    url = f"{config.SOCRATA_BASE}/{dataset}.json"
    resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows, list):
        raise FetchError(f"예상치 못한 응답: {str(rows)[:200]}")
    return normalize_rows(rows)


def _fetch_history_zip(report: str, codes: set[str], years: list[int]) -> list[dict]:
    out = []
    for y in years:
        url = config.HISTORY_ZIP[report].format(year=y)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning("히스토리 파일 실패 %s: %s", url, e)
            continue
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            for member in zf.namelist():
                text = zf.read(member).decode("latin-1")
                rows = list(csv.DictReader(io.StringIO(text)))
                out.extend(r for r in normalize_rows(rows) if r["code"] in codes)
    return out


def fetch_report(report: str, since: date) -> tuple[list[dict], str]:
    """(레코드 목록, 사용한 소스 설명) 반환."""
    markets = [m for m in config.MARKETS if m.report == report]
    codes = [c for m in markets for c in m.codes]
    dataset = config.TFF_DATASET if report == "tff" else config.DISAGG_DATASET
    errors = []
    try:
        rows = _fetch_socrata(dataset, codes, since)
        if rows:
            return rows, f"CFTC Public Reporting API ({dataset})"
        errors.append("Socrata: 0건")
    except (requests.RequestException, FetchError, ValueError) as e:
        errors.append(f"Socrata: {e}")
        log.warning("Socrata 실패 (%s): %s", report, e)
    years = list(range(since.year, date.today().year + 1))
    rows = _fetch_history_zip(report, set(codes), years)
    rows = [r for r in rows if r["date"] >= since]
    if rows:
        return rows, "CFTC 히스토리 파일 (cftc.gov/files/dea/history)"
    errors.append("히스토리 파일: 0건")
    raise FetchError(f"{report} 데이터 수집 실패 — " + " / ".join(errors))


def split_by_market(rows: list[dict], report: str) -> dict[str, dict]:
    """시장별 시계열 dict {key: {"code":..., "name":..., "rows":[...]}} (날짜 오름차순)."""
    result = {}
    for m in config.MARKETS:
        if m.report != report:
            continue
        chosen = None
        for code in m.codes:
            sel = [r for r in rows if r["code"] == code]
            if sel:
                chosen = code
                break
        if chosen is None:
            continue
        sel.sort(key=lambda r: r["date"])
        dedup = {r["date"]: r for r in sel}
        series = [dedup[d] for d in sorted(dedup)]
        result[m.key] = {"code": chosen, "name": series[-1]["name"], "rows": series}
    return result


def expected_report_date(now_utc: datetime) -> date:
    """현재 시점에 공개되어 있어야 할 최신 CFTC 기준일(화요일).

    정규 발표: 기준일(화) + 3일(금) 15:30 ET ≈ 20:30 UTC (여유 있게 21:00 UTC).
    """
    d = now_utc.date()
    tuesday = d - timedelta(days=(d.weekday() - 1) % 7)
    release = datetime.combine(tuesday + timedelta(days=3), datetime.min.time()) + timedelta(hours=21)
    if now_utc.replace(tzinfo=None) < release:
        tuesday -= timedelta(days=7)
    return tuesday


def release_date(report_date: date) -> date:
    return report_date + timedelta(days=3)


def week_label(report_date: date) -> str:
    """기준일(화) → '2026년 9월 2주차' (그 달의 N번째 화요일)."""
    return f"{report_date.year}년 {report_date.month}월 {(report_date.day - 1) // 7 + 1}주차"


def parse_week(text: str) -> date:
    """사용자 입력 → CFTC 기준일(화요일).

    지원 형식
      2026-09-22 / 2026.09.22       날짜 (화요일이 아니면 그 이전 가장 가까운 화요일)
      2026년 9월 2주차 / 26년 9월 2주   그 달의 N번째 화요일
      2026-09-2주 / 2026-09-W2
    """
    s = text.strip()
    m = re.fullmatch(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", s)
    if m:
        d = date(int(m[1]), int(m[2]), int(m[3]))
        return d - timedelta(days=(d.weekday() - 1) % 7)
    m = re.fullmatch(r"(\d{2}|\d{4})\s*(?:년|[-./])\s*(\d{1,2})\s*(?:월|[-./])?\s*[wW]?\s*(\d)\s*(?:주차|주)?", s)
    if not m:
        raise ValueError(f"기준 주차 형식을 알 수 없습니다: '{text}' (예: 2026년 9월 2주차, 2026-09-22)")
    year = int(m[1]) + (2000 if len(m[1]) == 2 else 0)
    month, nth = int(m[2]), int(m[3])
    first = date(year, month, 1)
    first_tue = first + timedelta(days=(1 - first.weekday()) % 7)
    d = first_tue + timedelta(weeks=nth - 1)
    if nth < 1 or d.month != month:
        raise ValueError(f"{year}년 {month}월에는 {nth}주차(화요일)가 없습니다.")
    return d
