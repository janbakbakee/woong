"""Claude Code(구독 토큰)용 작업 지시서 생성.

GitHub Actions의 claude-code-action이 이 파일(work/prompt.md)을 읽고
1) 뉴스 서치 → work/news.md, 2) CIO 분석 → work/analysis.json 을 작성한다.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from . import analyze, config

TEMPLATE = """# COT 주간 분석 작업 지시서

{system}

---

## 작업 파일
- 입력 ① COT 지표 (CFTC 원본에서 계산된 수치): `{metrics_path}`
- 입력 ② RSS 헤드라인 (공식기관·주요 매체, 보조 근거): `{rss_path}`
- 출력 ① 뉴스 브리프 (마크다운): `{news_path}`
- 출력 ② 분석 결과 (JSON): `{analysis_path}`

위 두 출력 파일 외에는 어떤 파일도 만들거나 수정하지 마라. 코드를 실행하지 마라.

## 1단계 — 매크로 & 시장 뉴스 서치 (Step 2.5)

먼저 입력 ② RSS 헤드라인을 Read로 읽어 이번 주 흐름을 파악한 뒤, WebSearch / WebFetch로 확인·보강한다.

{news}

검색 결과는 반드시 공신력 사이트({domains})에서 확인된 내용만 사용한다.
출처 불명 블로그·유튜브 요약 사이트는 참조 금지. 각 항목 끝에 출처 URL을 붙인다.
완성한 ①~⑫ 목록을 출력 ① 경로에 Write로 저장한다.

## 2단계 — CIO 분석 (Step 3)

입력 ① COT 지표를 Read로 읽고, 1단계 뉴스 브리프를 근거로 분석한다.

{instructions}

### 출력 형식
출력 ② 경로에 아래 JSON 스키마를 **정확히** 따르는 JSON 객체 하나를 Write로 저장한다 (코드블록·주석 없이 순수 JSON).
추가로 최상위에 `"sources": [{{"title": "...", "url": "..."}}]` 배열을 넣어 1단계에서 실제 인용한 기사 URL을 기록한다.

```json
{schema}
```

- `markets` 배열에는 지표 파일에 있는 시장({market_keys}) 각각에 대해 정확히 1개씩.
- 정수 필드(stars, score, prob_*)는 숫자로. 확률 3개 합은 100.

## 완료 조건
두 출력 파일이 모두 저장되면 "완료"라고만 답하고 종료한다.
"""


def build(work: Path, report_date: date, run_date: date, market_keys: list[str]) -> Path:
    news = analyze.NEWS_PROMPT.format(
        report_date=report_date.isoformat(),
        release_date=(report_date + timedelta(days=3)).isoformat(),
        start=(report_date - timedelta(days=6)).isoformat(),
        end=run_date.isoformat(),
    )
    text = TEMPLATE.format(
        system=analyze.SYSTEM_PROMPT.strip(),
        metrics_path=(work / "metrics.json").resolve(),
        rss_path=(work / "rss.json").resolve(),
        news_path=(work / "news.md").resolve(),
        analysis_path=(work / "analysis.json").resolve(),
        news=news.strip(),
        domains=", ".join(config.NEWS_DOMAINS),
        instructions=analyze.ANALYSIS_INSTRUCTIONS.strip(),
        schema=json.dumps(analyze.ANALYSIS_SCHEMA, ensure_ascii=False, indent=1),
        market_keys=", ".join(market_keys),
    )
    path = work / "prompt.md"
    path.write_text(text, encoding="utf-8")
    return path
