"""뉴스(Google News RSS, 키 불필요)와 DART 공시 악재 체크 (DART_API_KEY 있을 때만)."""
from __future__ import annotations

import html
import io
import logging
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, timedelta
from urllib.parse import quote_plus

import requests

log = logging.getLogger(__name__)
TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (kstock-daily)"}

# 제목에 포함되면 악재로 보고 감점/제외
BAD_WORDS = ("유상증자", "전환사채", "신주인수권", "교환사채", "감자", "횡령", "배임", "상장폐지", "거래정지",
             "감사의견", "불성실공시", "블록딜", "오버행", "소송", "적자전환", "압수수색", "검찰")


# 동명이인·스팸 기사 대신 증권 기사만 걸리도록 함께 검색 (예: '미코' → 미스코리아 기사)
NEWS_HINT = "주가 OR 주식 OR 실적 OR 수주 OR 증권 OR 특징주"


# 악재 단어가 있어도 해소·반대 문맥이면 악재 아님 (예: '유상증자 철회', '소송 승소')
RELIEF_WORDS = ("철회", "해소", "승소", "무혐의", "취하", "해제")


def is_bad(title: str) -> bool:
    return any(w in title for w in BAD_WORDS) and not any(w in title for w in RELIEF_WORDS)


def news(name: str, limit: int = 3) -> list[tuple[str, str]]:
    url = ("https://news.google.com/rss/search?q=" + quote_plus(f'"{name}" ({NEWS_HINT}) when:3d')
           + "&hl=ko&gl=KR&ceid=KR:ko")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        items = ET.fromstring(resp.content).iter("item")
        found = [(" ".join((i.findtext("title") or "").split()), i.findtext("link") or "") for i in items]
        # 제목에 종목명 없는 스팸·무관 기사 제외. 두 글자 이하 이름('미코')은 동음이의가 많아 증권 단어도 요구
        hints = NEWS_HINT.split(" OR ") + ["목표가", "매출", "영업이익"]
        return [(t, u) for t, u in found
                if name in t and (len(name) > 2 or any(h in t for h in hints))][:limit]
    except Exception as e:  # 뉴스 실패는 추천을 막지 않는다
        log.warning("뉴스 실패 %s: %s", name, e)
        return []


class Dart:
    """최근 공시 제목 조회. 키가 없으면 None을 돌려주는 from_env() 사용."""

    def __init__(self, key: str):
        self.key = key
        resp = requests.get("https://opendart.fss.or.kr/api/corpCode.xml", params={"crtfc_key": key}, timeout=60)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            root = ET.fromstring(z.read(z.namelist()[0]))
        self.corp = {(el.findtext("stock_code") or "").strip(): el.findtext("corp_code")
                     for el in root.iter("list") if (el.findtext("stock_code") or "").strip()}

    @classmethod
    def from_env(cls) -> "Dart | None":
        key = os.environ.get("DART_API_KEY")
        if not key:
            return None
        try:
            return cls(key)
        except Exception as e:
            log.warning("DART 비활성 (초기화 실패): %s", e)
            return None

    def recent(self, stock_code: str, days: int = 30) -> list[str]:
        corp = self.corp.get(stock_code)
        if not corp:
            return []
        today = date.today()
        try:
            body = requests.get("https://opendart.fss.or.kr/api/list.json", timeout=TIMEOUT, params={
                "crtfc_key": self.key, "corp_code": corp, "page_count": 100,
                "bgn_de": (today - timedelta(days=days)).strftime("%Y%m%d"), "end_de": today.strftime("%Y%m%d"),
            }).json()
        except Exception as e:
            log.warning("DART 조회 실패 %s: %s", stock_code, e)
            return []
        return [r.get("report_nm", "") for r in body.get("list", [])]

    # 정기보고서 주요계정 (상장사 다중회사, 금융업 제외). 최신 보고서부터 찾아 내려감
    REPORTS = (("11014", 9, "3분기"), ("11012", 6, "반기"), ("11013", 3, "1분기"), ("11011", 12, "사업보고서"))

    def financials(self, codes: list[str], today: date | None = None) -> dict[str, dict]:
        """{종목코드: {period, fs, months, op, op_prev, net, equity, liab}} — 금액은 억원, 손익은 누적."""
        today = today or date.today()
        left = {self.corp[c]: c for c in codes if c in self.corp}
        out: dict[str, dict] = {}
        periods = [(today.year, r) for r in self.REPORTS[:3]] + [(today.year - 1, self.REPORTS[3]), (today.year - 1, self.REPORTS[0])]
        for year, (rc, months, label) in periods:
            if not left:
                break
            corps = list(left)
            for i in range(0, len(corps), 100):
                try:
                    body = requests.get("https://opendart.fss.or.kr/api/fnlttMultiAcnt.json", timeout=TIMEOUT, params={
                        "crtfc_key": self.key, "corp_code": ",".join(corps[i:i + 100]),
                        "bsns_year": str(year), "reprt_code": rc}).json()
                except Exception as e:
                    log.warning("DART 재무 조회 실패 %s %s: %s", year, label, e)
                    continue
                if body.get("status") not in ("000", "013"):
                    log.warning("DART 재무 %s %s: %s %s", year, label, body.get("status"), body.get("message"))
                for code, fin in _parse_fin(body.get("list", []), months, f"{year} {label}").items():
                    corp = self.corp.get(code)
                    if corp in left:
                        out[left.pop(corp)] = fin
        log.info("DART 재무: %d/%d종목", len(out), len(codes))
        return out


def company(dart: "Dart", stock_code: str, known: dict | None = None) -> dict:
    """DART 기업개황 + 최신 정기보고서의 '사업의 개요' 앞부분. 실패한 항목은 비운다.
    known(이전에 저장한 회사 정보)의 보고서가 같으면 원문(수 MB)을 다시 받지 않는다."""
    corp = dart.corp.get(stock_code)
    if not corp:
        return {}
    out: dict = {}
    try:
        b = requests.get("https://opendart.fss.or.kr/api/company.json", timeout=TIMEOUT,
                         params={"crtfc_key": dart.key, "corp_code": corp}).json()
        if b.get("status") == "000":
            out.update(ceo=b.get("ceo_nm", ""), est=b.get("est_dt", ""), homepage=b.get("hm_url", ""),
                       acc_mt=b.get("acc_mt", ""))
    except Exception as e:
        log.warning("DART 기업개황 실패 %s: %s", stock_code, e)
    try:
        today = date.today()
        b = requests.get("https://opendart.fss.or.kr/api/list.json", timeout=TIMEOUT, params={
            "crtfc_key": dart.key, "corp_code": corp, "pblntf_ty": "A", "page_count": 20,
            "bgn_de": (today - timedelta(days=400)).strftime("%Y%m%d"), "end_de": today.strftime("%Y%m%d")}).json()
        rep = next((r for r in b.get("list", []) if any(k in r.get("report_nm", "") for k in ("사업보고서", "반기보고서", "분기보고서"))
                    and "기재정정" not in r.get("report_nm", "")), None) or next(iter(b.get("list", [])), None)
        if rep:
            out.update(report=rep["report_nm"].strip(), rcept_no=rep["rcept_no"])
            same = known and known.get("rcept_no") == rep["rcept_no"] and known.get("summary")
            out["summary"] = known["summary"] if same else business_overview(dart.key, rep["rcept_no"])
    except Exception as e:
        log.warning("DART 사업의 개요 실패 %s: %s", stock_code, e)
    return out


def business_overview(key: str, rcept_no: str, limit: int = 600) -> str:
    """정기보고서 원문(XML)에서 'II. 사업의 내용 > 1. 사업의 개요' 본문 앞부분."""
    resp = requests.get("https://opendart.fss.or.kr/api/document.xml", timeout=60,
                        params={"crtfc_key": key, "rcept_no": rcept_no})
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        name = min(z.namelist(), key=len)  # 본문은 접수번호.xml (첨부는 접수번호_00760.xml 형태)
        raw = z.read(name)
    text = raw.decode("utf-8", errors="ignore")
    return overview_from_xml(text, limit)


def overview_from_xml(text: str, limit: int = 600) -> str:
    m = re.search(r"<TITLE[^>]*>\s*(?:\d+\.\s*)?사업의\s*개요\s*</TITLE>(.*?)(?=<TITLE|$)", text, re.S)
    if not m:
        return ""
    body = re.sub(r"<TABLE.*?</TABLE>", " ", m.group(1), flags=re.S)  # 표는 빼고 문단만
    body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    body = re.sub(r"\s+", " ", body).strip()
    if len(body) <= limit:
        return body
    cut = body[:limit]
    end = max(cut.rfind(". "), cut.rfind("다. "))
    return (cut[:end + 1] if end > limit // 2 else cut).strip() + " …"


def _amount(v) -> float | None:
    v = (v or "").replace(",", "").strip()
    try:
        return float(v) / 1e8
    except ValueError:
        return None


def _parse_fin(rows: list[dict], months: int, period: str) -> dict[str, dict]:
    """주요계정 행 → 종목별 값. 연결(CFS)이 있으면 연결, 없으면 별도(OFS). 영업이익이 없으면 제외."""
    by: dict[tuple, dict] = {}
    for r in rows:
        code = (r.get("stock_code") or "").strip()
        name = (r.get("account_nm") or "").replace(" ", "")
        acc = by.setdefault((code, r.get("fs_div", "")), {})
        cum = r.get("thstrm_add_amount") or r.get("thstrm_amount")
        prev = r.get("frmtrm_add_amount") or r.get("frmtrm_amount")
        if name == "영업이익":
            acc["op"], acc["op_prev"] = _amount(cum), _amount(prev)
        elif name.startswith("당기순이익"):
            acc.setdefault("net", _amount(cum))
        elif name == "자본총계":
            acc["equity"] = _amount(r.get("thstrm_amount"))
        elif name == "부채총계":
            acc["liab"] = _amount(r.get("thstrm_amount"))
    out = {}
    for (code, fs), acc in sorted(by.items(), key=lambda kv: kv[0][1] != "CFS"):  # CFS 먼저
        if code and code not in out and acc.get("op") is not None:
            out[code] = {"period": period, "fs": "연결" if fs == "CFS" else "별도", "months": months,
                         "op": acc["op"], "op_prev": acc.get("op_prev"), "net": acc.get("net"),
                         "equity": acc.get("equity"), "liab": acc.get("liab")}
    return out
