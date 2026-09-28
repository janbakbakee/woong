"""분석 대상 시장 및 데이터 소스 설정."""
from __future__ import annotations

from dataclasses import dataclass, field

# CFTC 공개 API (Socrata). 금융선물 = TFF(Traders in Financial Futures),
# 원유 = Disaggregated. 둘 다 Futures Only.
SOCRATA_BASE = "https://publicreporting.cftc.gov/resource"
TFF_DATASET = "gpe5-46if"
DISAGG_DATASET = "72hh-3qpy"

# Socrata 실패 시 사용하는 CFTC 연도별 히스토리 압축 파일
HISTORY_ZIP = {
    "tff": "https://www.cftc.gov/files/dea/history/fut_fin_txt_{year}.zip",
    "disagg": "https://www.cftc.gov/files/dea/history/fut_disagg_txt_{year}.zip",
}

# 사용자가 수동 확인할 원문 페이지 (실패 안내 메시지용)
SOURCE_PAGES = {
    "tff": "https://www.cftc.gov/dea/futures/financial_lf.htm",
    "disagg": "https://www.cftc.gov/dea/futures/petroleum_lf.htm",
}

HISTORY_YEARS = 6  # 5년 백분위 + 여유분


@dataclass(frozen=True)
class Market:
    key: str            # 내부 키 (ES, NQ ...)
    name: str           # 카드 표기명
    report: str         # "tff" | "disagg"
    codes: tuple        # CFTC 코드 (앞쪽 우선, 없으면 다음 코드 사용)
    headline: str       # 카드 Net 기준 그룹: "lev" | "am" | "mm"
    groups: tuple       # 스마트머니 비교 그룹
    up_label: str = "상승"
    down_label: str = "하락"
    in_sheet: bool = True  # ⑫ 구글 시트 행에 포함 여부
    name_hint: str = ""    # 코드 매칭 실패 시 시장명 부분일치
    extra: dict = field(default_factory=dict)


MARKETS: tuple[Market, ...] = (
    Market("NQ", "NQ (나스닥100)", "tff", ("20974+", "209742"), "lev", ("lev", "am"),
           name_hint="NASDAQ"),
    Market("ES", "ES (S&P500)", "tff", ("13874+", "13874A"), "lev", ("lev", "am"),
           name_hint="S&P 500"),
    Market("WTI", "WTI 원유", "disagg", ("067651",), "mm", ("mm",),
           name_hint="WTI"),
    Market("BTC", "BTC (비트코인)", "tff", ("133741",), "lev", ("lev", "am"),
           name_hint="BITCOIN"),
    Market("EUR", "EUR/USD", "tff", ("099741",), "am", ("lev", "am"),
           up_label="EUR↑", down_label="EUR↓", name_hint="EURO FX"),
    Market("JPY", "USD/JPY (엔)", "tff", ("097741",), "lev", ("lev", "am"),
           up_label="엔↑", down_label="엔↓", name_hint="JAPANESE YEN"),
    Market("USD", "USD Index (DXY)", "tff", ("098662",), "lev", ("lev", "am"),
           up_label="달러↑", down_label="달러↓", in_sheet=False, name_hint="USD INDEX"),
)

MARKET_BY_KEY = {m.key: m for m in MARKETS}

GROUP_LABEL = {"lev": "Lev Funds", "am": "Asset Mgr", "mm": "Managed Money"}

# Step 2.5 뉴스 서치에 허용할 공신력 사이트
NEWS_DOMAINS = [
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
    "federalreserve.gov", "bls.gov", "bea.gov", "eia.gov",
    "boj.or.jp", "ecb.europa.eu", "farside.co.uk", "cboe.com",
    "cmegroup.com",  # FedWatch 금리 확률 원본
]

# 템플릿 하드코딩 색상
COLORS = {
    "bg": "#f5f5f0", "card": "#ffffff", "text": "#0b0b0b", "sub": "#52514e",
    "border": "#e0dfd8", "bull_bg": "#eaf3de", "bear_bg": "#fcebeb", "neut_bg": "#faeeda",
    "bull": "#3B6D11", "bear": "#A32D2D", "neut": "#854F0B",
    "lev": "#2a78d6", "am": "#c2650a", "mm": "#2a78d6",
    "muted": "#888780", "grid": "#f0efec",
}
