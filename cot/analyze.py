"""Claude API를 이용한 뉴스 서치(Step 2.5) + CIO 분석(Step 3).

수치(포지션, 변화, 백분위, 퀀트 점수)는 metrics.py에서 계산된 값만 사용하고,
Claude는 해석·추론·확률·의견·서술만 담당한다.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta

from . import config

log = logging.getLogger(__name__)

MODEL = os.environ.get("COT_MODEL") or "claude-opus-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_CONTINUATIONS = 5


class AnalysisError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Step 2.5 — 매크로 & 시장 뉴스 서치
# --------------------------------------------------------------------------

NEWS_PROMPT = """너는 글로벌 매크로 헤지펀드의 리서치 애널리스트다.
CFTC COT 기준일은 {report_date}(화), 공식 발표일은 {release_date}(금)이다.
{start} ~ {end} 기간(발표일 직후 주말까지)을 중심으로 아래 항목을 웹 검색해 사실만 정리해라.

[중앙은행 / 통화정책]
① Fed — 최근 1주 FOMC 위원 발언 / 금리 선물 인하 확률 변화
② BOJ — 금리 인상 시그널 / 엔화 관련 최신 발언
③ ECB — 매파/비둘기 스탠스 변화
[주식 시장]
④ S&P500 / 나스닥 — 해당 주 주요 등락 원인 및 섹터 동향
⑤ AI / 빅테크 실적 또는 이슈 (밸류에이션 영향)
⑥ VIX 공포지수 — 주간 수준 및 변화
[원자재 / 에너지]
⑦ OPEC+ — 최신 증산/감산 결정 및 실제 이행률
⑧ WTI 스팟 가격 — 주간 변동 및 EIA 재고 결과
[암호화폐]
⑨ BTC 현물 ETF — 최근 주간 순유입/유출 금액 (farside.co.uk 우선)
⑩ BTC 가격 — 주요 저항/지지 구간 현황
[지정학 / 거시]
⑪ 미-중 관세 — 협상 진행 또는 교착 최신 상황
⑫ 주요 경제지표 — 해당 주 발표된 CPI/PCE/고용 결과

규칙:
- 검색은 허용된 공신력 사이트(Reuters, Bloomberg, FT, WSJ, 각국 중앙은행, BLS, BEA, EIA, Farside, CBOE, CME)만 사용.
- 각 항목마다 핵심 사실 1~3줄 + 날짜 + 수치. 출처 사이트명을 괄호로 표기.
- 확인되지 않거나 출처가 상충하면 그 항목은 "확인 불가"라고 쓴다. 추측 금지.
- 출력은 한국어, ①~⑫ 번호 목록만. 서론/결론 없이.
"""


def _client():
    import anthropic
    return anthropic.Anthropic()


def _check_stop(msg) -> None:
    if msg.stop_reason == "refusal":
        raise AnalysisError(f"모델이 요청을 거절했습니다: {getattr(msg, 'stop_details', None)}")
    if msg.stop_reason == "max_tokens":
        raise AnalysisError("응답이 max_tokens에서 잘렸습니다.")


def news_brief(report_date: date, release: date, run_date: date) -> dict:
    client = _client()
    prompt = NEWS_PROMPT.format(
        report_date=report_date.isoformat(), release_date=release.isoformat(),
        start=(report_date - timedelta(days=6)).isoformat(),
        end=run_date.isoformat(),
    )
    tools = [{
        "type": "web_search_20260209", "name": "web_search",
        "max_uses": 25, "allowed_domains": config.NEWS_DOMAINS,
    }]
    messages = [{"role": "user", "content": prompt}]
    for _ in range(MAX_CONTINUATIONS + 1):
        with client.beta.messages.stream(
            model=MODEL, max_tokens=32000, messages=messages, tools=tools,
            thinking={"type": "adaptive"}, output_config={"effort": "high"},
            betas=[FALLBACK_BETA], fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
        if msg.stop_reason == "pause_turn":
            messages = [messages[0], {"role": "assistant", "content": msg.content}]
            continue
        break
    _check_stop(msg)

    texts, sources, seen = [], [], set()
    for block in msg.content:
        if block.type != "text":
            continue
        texts.append(block.text)
        for c in getattr(block, "citations", None) or []:
            url = getattr(c, "url", None)
            if url and url not in seen:
                seen.add(url)
                sources.append({"title": getattr(c, "title", None) or url, "url": url})
    text = "".join(texts).strip()
    if not text:
        raise AnalysisError("뉴스 서치 결과가 비어 있습니다.")
    return {"text": text, "sources": sources}


# --------------------------------------------------------------------------
# Step 3 — CIO 분석 (구조화 출력)
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """너는 20년 경력의 글로벌 매크로 헤지펀드 CIO다.
CFTC 공식 COT Report에서 계산된 포지션 지표와, 공신력 사이트에서 검색한 이번 주 매크로 뉴스를 받아
스마트머니 포지션을 분석한다.

원칙:
1. 모든 수치는 제공된 지표 JSON에 있는 값만 인용한다. 새로운 포지션 수치를 만들어내지 않는다.
2. 단순 Long/Short 나열 금지. Fed 통화정책, CPI, 고용, 지정학 등 거시 팩터와 연결해
   "기관들이 왜 그런 포지션을 구축했는지" 추론하되, 근거는 반드시 제공된 뉴스 브리프에서 가져온다.
   뉴스 브리프에 없거나 "확인 불가"인 내용은 추측하지 말고 "확인 불가"라고 쓴다.
3. 데이터가 없는 시장은 "데이터 확인 불가 — 보류"라고 쓴다.
4. 시장 해석 기준:
   - NQ/ES: Leveraged Funds vs Asset Managers (카드 Net = Lev Funds)
   - WTI: Managed Money
   - BTC: Leveraged Funds (AM은 참고)
   - EUR: 카드 Net = Asset Managers, Lev Funds와 비교
   - JPY: 카드 Net = Lev Funds, AM과 비교. JPY 선물 롱 = 엔화 강세 베팅.
   - USD Index: Lev Funds vs AM. 롱 = 달러 강세 베팅.
5. pct_3y/pct_5y는 헤드라인 그룹 순포지션의 실제 3년/5년 백분위(0=역대 최대 순숏, 100=역대 최대 순롱)다.
   cot_idx는 기간 내 min-max 위치(0~100)다. Extreme/Contrarian 판단은 이 값을 근거로 한다.
6. score(100점 만점)는 quant.total을 기준으로 하되, 뉴스·맥락을 반영해 ±15점 이내에서만 조정한다.
   50 = 중립, 50 이상 = 강세 우위, 50 미만 = 약세 우위 (해당 자산 가격 기준).
7. 확률(prob_up/prob_side/prob_down)은 향후 1~4주, 합계 100.
8. 문체: 간결한 한국어 리서치 노트체. 카드/행 설명은 한 줄(60자 내외), 심리 분석과 결론은 충분히 깊게.
"""


def _market_schema() -> dict:
    s = {"type": "string"}
    i = {"type": "integer"}
    props = {
        "market": {"type": "string", "enum": [m.key for m in config.MARKETS]},
        "rating": {"type": "string", "enum": ["Bullish", "Neutral", "Bearish"]},
        "rating_label": s, "stars": i,
        "card_note": s, "change_note": s,
        "smart_money": s, "smart_money_icon": {"type": "string", "enum": ["✅", "🔴", "🔵", "⚠️", "⚪"]},
        "extreme_note": s,
        "contrarian": s, "contrarian_tone": {"type": "string", "enum": ["bull", "bear", "neut"]},
        "score": i, "score_reason": s,
        "prob_up": i, "prob_side": i, "prob_down": i,
        "verdict": {"type": "string", "enum": ["Strong Buy", "Buy", "Hold", "Reduce", "Sell"]},
        "trend_short": s, "trend_mid": s, "trend_long": s,
    }
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


ANALYSIS_SCHEMA = _obj({
    "markets": {"type": "array", "items": _market_schema()},
    "smart_money_highlight": {"type": "string"},
    "sentiment": {"type": "array", "items": _obj({"label": {"type": "string"}, "text": {"type": "string"}})},
    "top5": {"type": "array", "items": _obj({"title": {"type": "string"}, "body": {"type": "string"}})},
    "exec_theme": {"type": "string"},
    "exec_paragraphs": {"type": "array", "items": _obj({"label": {"type": "string"}, "text": {"type": "string"}})},
    "exec_recommendation": {"type": "string"},
    "monitoring": {"type": "array", "items": {"type": "string"}},
    "trend_overview": {"type": "string"},
    "sheet_opinion": {"type": "string"},
    "sheet_memo": {"type": "string"},
})

ANALYSIS_INSTRUCTIONS = """아래 [COT 지표]와 [뉴스 브리프]로 이번 주 분석을 작성해라.

필드 안내:
- markets: 지표에 있는 모든 시장에 대해 1개씩.
  rating_label: 배지 문구 (보통 Bullish/Neutral/Bearish, 통화는 "JPY 강세 지속"처럼 방향을 풀어써도 됨)
  stars: 1~5 / card_note: 카드 하단 한 줄 (전주 대비 변화 요약, 예: "2주 연속 숏 커버링 (전주 −90,587)")
  change_note: ② 전주 대비 진단 한 줄. 앞에 [diag] 라벨이 자동으로 붙으므로 diag 단어는 반복하지 말고 수치·연속성·그룹 간 차이를 설명
  smart_money: ③ 그룹 간 의견 일치/불일치 + 신뢰 시그널 한 줄
  extreme_note: ⑤ 백분위 해석 짧은 구 (예: "3주 연속 완화", "극단권 진입")
  contrarian: ⑥ 극단 포지션 판단 + 반전 시나리오 한두 문장
  trend_short/mid/long: ⑬ 다주차 트렌드 (단기=이번 주, 중기=최근 6주 quant_history, 장기=3년 백분위/추세)
- smart_money_highlight: ③ 하단 "이번 주 핵심" 한 문장
- sentiment: ④ 기관 심리 & 거시 베팅 심층 추론. label은 "주식","WTI","BTC","FX" 4개. 각 3~5문장, 뉴스 브리프 근거 필수.
- top5: ⑩ 이번 주 핵심 변화 TOP 5 (title 굵은 제목, body 설명)
- exec_theme: ⑪ 핵심 테마 한 문장 / exec_paragraphs: label "개요","주식 (ES/NQ)","WTI 원유","통화 (EUR/JPY/USD)","BTC" 순서로 A4 한 장 분량
- exec_recommendation: 종합 권고 한 문장 / monitoring: 모니터링 포인트 3~5개
- trend_overview: ⑬ 다주차 트렌드 요약 문단
- sheet_opinion: 구글 시트용 투자의견 요약 (예: "NQ/ES/WTI Hold, JPY Buy, EUR/BTC Reduce")
- sheet_memo: 구글 시트용 핵심메모 (슬래시로 구분, 80자 내외)
"""


def _metrics_for_prompt(metrics: dict) -> list[dict]:
    out = []
    for m in metrics.values():
        d = {k: v for k, v in m.items() if k not in ("chart",)}
        out.append(d)
    return out


def run_analysis(metrics: dict, news: dict, report_date: date) -> dict:
    client = _client()
    payload = json.dumps(_metrics_for_prompt(metrics), ensure_ascii=False, default=str)
    user = (f"{ANALYSIS_INSTRUCTIONS}\n\nCFTC 기준일: {report_date.isoformat()}\n\n"
            f"[COT 지표]\n{payload}\n\n[뉴스 브리프]\n{news['text']}")
    with client.beta.messages.stream(
        model=MODEL, max_tokens=64000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        thinking={"type": "adaptive"},
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
        betas=[FALLBACK_BETA], fallbacks="default",
    ) as stream:
        msg = stream.get_final_message()
    _check_stop(msg)
    text = next((b.text for b in msg.content if b.type == "text"), None)
    if not text:
        raise AnalysisError("분석 응답에 텍스트가 없습니다.")
    return postprocess(json.loads(text), metrics)


# --------------------------------------------------------------------------
# 후처리 & API 미사용 시 수치 기반 기본 분석
# --------------------------------------------------------------------------

def _rating_from_score(s: int) -> str:
    return "Bullish" if s >= 60 else ("Bearish" if s < 40 else "Neutral")


def _verdict_from_score(s: int) -> str:
    if s >= 75:
        return "Strong Buy"
    if s >= 60:
        return "Buy"
    if s >= 40:
        return "Hold"
    if s >= 25:
        return "Reduce"
    return "Sell"


def _normalize_probs(a: int, b: int, c: int) -> tuple[int, int, int]:
    vals = [max(0, int(a)), max(0, int(b)), max(0, int(c))]
    tot = sum(vals) or 1
    scaled = [round(v * 100 / tot) for v in vals]
    scaled[1] += 100 - sum(scaled)
    return tuple(scaled)


def postprocess(result: dict, metrics: dict) -> dict:
    by_key = {}
    for m in result.get("markets", []):
        k = m.get("market")
        if k not in metrics:
            continue
        q = (metrics[k].get("quant") or {}).get("total")
        if q is not None:
            m["score"] = max(q - 15, min(q + 15, int(m["score"])))
        m["score"] = max(0, min(100, int(m["score"])))
        m["stars"] = max(1, min(5, int(m["stars"])))
        m["prob_up"], m["prob_side"], m["prob_down"] = _normalize_probs(
            m["prob_up"], m["prob_side"], m["prob_down"])
        by_key[k] = m
    for k, mt in metrics.items():
        if k not in by_key:
            by_key[k] = fallback_market(mt)
    result["markets"] = by_key
    result["ai"] = True
    return result


def _fmt(n: int) -> str:
    return f"{n:+,}".replace("-", "−")


def fallback_market(mt: dict) -> dict:
    q = (mt.get("quant") or {}).get("total", 50)
    h = mt["groups"][mt["headline"]]
    streak = f", {mt['streak']}주 연속" if mt["streak"] >= 2 else ""
    return {
        "market": mt["key"], "rating": _rating_from_score(q), "rating_label": _rating_from_score(q),
        "stars": max(1, min(5, round(q / 20))),
        "card_note": f"{mt['diag']}{streak} (전주 {_fmt(mt['net_prev'])})",
        "change_note": f"{h['label']} 순포지션 {_fmt(mt['net_chg'])}{streak} "
                       f"(롱 {_fmt(h['long_chg'])} / 숏 {_fmt(h['short_chg'])})",
        "smart_money": " / ".join(f"{g['label']} {_fmt(g['net_chg'])}" for g in mt["groups"].values())
                       + f" — {mt['agreement']}",
        "smart_money_icon": {"일치(개선)": "✅", "일치(악화)": "🔴"}.get(mt["agreement"], "⚪"),
        "extreme_note": "",
        "contrarian": "", "contrarian_tone": "neut",
        "score": q, "score_reason": "퀀트 점수",
        "prob_up": None, "prob_side": None, "prob_down": None,
        "verdict": _verdict_from_score(q),
        "trend_short": "", "trend_mid": "", "trend_long": "",
    }


def fallback_analysis(metrics: dict, reason: str) -> dict:
    return {
        "markets": {k: fallback_market(m) for k, m in metrics.items()},
        "smart_money_highlight": "",
        "sentiment": [],
        "top5": [],
        "exec_theme": f"AI 분석 미실행 — {reason}",
        "exec_paragraphs": [{"label": "안내", "text": "CFTC 원본 수치와 퀀트 점수만 표시합니다. "
                             "뉴스 기반 심리 분석·확률·결론은 AI 분석이 실행되어야 생성됩니다."}],
        "exec_recommendation": "",
        "monitoring": [],
        "trend_overview": "",
        "sheet_opinion": " / ".join(f"{k} {fallback_market(m)['verdict']}" for k, m in metrics.items()),
        "sheet_memo": "퀀트 점수 기반 (AI 미실행)",
        "ai": False,
    }
