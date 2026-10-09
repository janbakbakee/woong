"""뉴스(Google News RSS, 키 불필요)와 DART 공시 악재 체크 (DART_API_KEY 있을 때만)."""
from __future__ import annotations

import io
import logging
import os
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


def is_bad(title: str) -> bool:
    return any(w in title for w in BAD_WORDS)


def news(name: str, limit: int = 3) -> list[tuple[str, str]]:
    url = ("https://news.google.com/rss/search?q=" + quote_plus(f'"{name}" ({NEWS_HINT}) when:3d')
           + "&hl=ko&gl=KR&ceid=KR:ko")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        items = ET.fromstring(resp.content).iter("item")
        return [(" ".join((i.findtext("title") or "").split()), i.findtext("link") or "") for i in items][:limit]
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
