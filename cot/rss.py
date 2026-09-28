"""RSS 뉴스 헤드라인 수집 (키 불필요, 무료).

공식 기관 피드 + Google News RSS(공신력 매체 site: 필터)로 해당 주 헤드라인을 모은다.
AI 분석의 보조 근거이자, AI 미실행 시에도 리포트에 그대로 표시된다.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (cot-weekly-report; +https://github.com)"}
TIMEOUT = 30
MAX_PER_FEED = 12

OFFICIAL_FEEDS = [
    ("Fed 보도자료", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("Fed 연설", "https://www.federalreserve.gov/feeds/speeches.xml"),
    ("BLS", "https://www.bls.gov/feed/bls_latest.rss"),
    ("EIA", "https://www.eia.gov/rss/todayinenergy.xml"),
    ("ECB", "https://www.ecb.europa.eu/rss/press.html"),
    ("BOJ", "https://www.boj.or.jp/en/rss/whatsnew.xml"),
]

MEDIA = "(site:reuters.com OR site:bloomberg.com OR site:ft.com OR site:wsj.com)"
GOOGLE_NEWS_QUERIES = [
    ("Fed·금리", "Fed rate FOMC"),
    ("BOJ·엔화", "Bank of Japan yen"),
    ("ECB·유로", "ECB euro"),
    ("미국 증시", "S&P 500 Nasdaq stocks week"),
    ("원유·OPEC", "oil OPEC crude"),
    ("비트코인", "bitcoin ETF"),
    ("미중 관세", "US China tariffs"),
]


def _google_news_url(q: str) -> str:
    return ("https://news.google.com/rss/search?q=" + quote_plus(f"{q} {MEDIA} when:10d")
            + "&hl=en-US&gl=US&ceid=US:en")


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(xml_text: str) -> list[dict]:
    """RSS 2.0 / RSS 1.0(RDF) / Atom 공통 파서."""
    root = ET.fromstring(xml_text)
    items = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        rec = {"title": "", "url": "", "published": None}
        for child in el:
            name = _local(child.tag)
            if name == "title":
                rec["title"] = " ".join((child.text or "").split())
            elif name == "link":
                rec["url"] = (child.text or child.get("href") or "").strip()
            elif name in ("pubDate", "published", "updated", "date") and not rec["published"]:
                rec["published"] = _parse_date(child.text)
        if rec["title"] and rec["url"]:
            items.append(rec)
    return items


def _fetch(url: str) -> list[dict]:
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return parse_feed(resp.text)


def collect(report_date: date, until: date) -> list[dict]:
    """[{group, title, url, published(ISO)}] — 기준일 7일 전 ~ until 사이 헤드라인."""
    start = datetime.combine(report_date - timedelta(days=7), datetime.min.time(), timezone.utc)
    end = datetime.combine(until + timedelta(days=1), datetime.min.time(), timezone.utc)
    sources = OFFICIAL_FEEDS + [(g, _google_news_url(q)) for g, q in GOOGLE_NEWS_QUERIES]
    out, seen = [], set()
    for group, url in sources:
        try:
            items = _fetch(url)
        except (requests.RequestException, ET.ParseError) as e:
            log.warning("RSS 실패 %s: %s", group, e)
            continue
        n = 0
        for it in items:
            pub = it["published"]
            if pub is None or not (start <= pub < end) or it["url"] in seen:
                continue
            seen.add(it["url"])
            out.append({"group": group, "title": it["title"], "url": it["url"],
                        "published": pub.date().isoformat()})
            n += 1
            if n >= MAX_PER_FEED:
                break
    return out
