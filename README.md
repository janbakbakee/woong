# CFTC COT 주간 분석 — 자동화

글로벌 매크로 헤지펀드 CIO 시각의 스마트머니 포지션 분석을 **매주 자동으로** 생성해 GitHub Pages 웹사이트로 게시합니다.

```
토요일 09:37 KST (GitHub Actions)
 ├─ Step 1  CFTC 데이터 조회 (공식 API → 실패 시 CFTC 히스토리 파일)
 ├─ Step 2  기준일(화) 검증 — 불일치 시 게시 안 함, 일·월·화 자동 재시도
 ├─ 지표 계산 (Python, 결정론적): Net/전주대비/진단/연속주차/3·5년 백분위/COT Index/z-score/퀀트 점수
 ├─ Step 2.5 매크로 뉴스 서치 (Claude + web_search, 공신력 사이트만 허용)
 ├─ Step 3  CIO 분석 (Claude, 구조화 출력) — 수치는 계산값만 사용
 └─ HTML 대시보드 생성 → docs/ 커밋 → GitHub Pages 배포
```

## 분석 대상

| 시장 | CFTC 코드 | 카드 Net 기준 | 비교 그룹 |
|---|---|---|---|
| NQ (나스닥100) | 20974+ (Consolidated) | Lev Funds | Asset Mgr |
| ES (S&P500) | 13874+ (Consolidated) | Lev Funds | Asset Mgr |
| WTI 원유 | 067651 (NYMEX) | Managed Money | — |
| BTC | 133741 | Lev Funds | Asset Mgr |
| EUR/USD | 099741 | Asset Mgr | Lev Funds |
| USD/JPY | 097741 | Lev Funds | Asset Mgr |
| USD Index | 098662 | Lev Funds | Asset Mgr |

Consolidated 코드가 API에 없으면 단일 계약 코드(13874A, 209742)로 대체하고 리포트 하단에 사용 코드를 표기합니다. 설정은 `cot/config.py`.

## 리포트 구성

기존 `COT_template.html`과 같은 구조, 색상, 순서(①~⑬)를 유지합니다. 색상은 모두 하드코딩되어 있고, 차트는 JS 없이 인라인 SVG로 그려서 카카오톡, 구글 드라이브, 모바일 크롬에서 그대로 보입니다.

**추가된 부분**
- **③-1 그룹별 상세표**: Lev Funds와 AM의 롱/숏/Net/변화/진단/연속 주차/3년 백분위
- **⑤ Extreme**: "추정" 대신 CFTC 히스토리로 계산한 **실제 3년/5년 백분위**
- **⑤-1 포지셔닝 게이지**: 전 시장의 3년 백분위 위치(현재 ●, 4주 전 ○)
- **⑦ 퀀트 점수**: 방향성 40 + 일치도 30 + 추세 30을 코드로 계산하고, AI는 ±15점 안에서만 조정
- **⑬ 트렌드**: 과거 주차 퀀트 점수를 CFTC 히스토리로 자동 계산 (구글 시트 붙여넣기 불필요)
- **⑭ 3년 순포지션 추이 차트**: COT Index(26/156주), 주간변화 z-score, 미결제약정
- **뉴스 브리프 원문과 출처 링크**
- **아카이브 페이지(`index.html`)**: 주차별 점수 히트맵과 `data/history.csv` 누적 기록

⑫ 구글 시트 행은 기존과 같은 열 순서로 계속 출력됩니다.

## 설정 방법 (최초 1회)

1. **API 키 등록**: GitHub repo → Settings → Secrets and variables → Actions → New repository secret
   - `ANTHROPIC_API_KEY` = Claude API 키 ([console.anthropic.com](https://console.anthropic.com))
   - (선택) Variables 탭에서 `COT_MODEL`로 모델 변경. 기본값은 `claude-opus-5`
2. **GitHub Pages 켜기**: Settings → Pages → Source를 **GitHub Actions**로 선택
3. **첫 실행**: Actions 탭 → "COT 주간 리포트" → Run workflow
   - 과거 주차를 만들려면 `date`에 화요일 날짜 입력 (예: `2026-07-28`)
4. 결과: `https://<사용자명>.github.io/<repo>/`

API 키가 없으면 AI 분석 없이 수치, 차트, 퀀트 점수만 있는 리포트가 생성됩니다.

## 데이터 원칙 (프롬프트 규칙 반영)

- 기준일이 맞지 않거나 조회에 실패하면 **이전 데이터나 추정치로 대체하지 않습니다**. 게시하지 않고 다음 예약 시간에 재시도합니다.
- 일부 시장만 확인되면 그 시장만 분석하고, 나머지는 "데이터 수신 대기 중"으로 표시합니다. 다음 실행 때 자동으로 다시 만듭니다.
- 뉴스는 Reuters, Bloomberg, FT, WSJ, Fed, BLS, BEA, EIA, BOJ, ECB, Farside, CBOE, CME 도메인만 검색합니다. 확인되지 않은 내용은 "확인 불가"로 표기합니다.
- 모델이 요청을 거절하면 서버 측 fallback(`server-side-fallback-2026-07-01`, `fallbacks="default"`)으로 다른 모델이 이어받습니다.

## 로컬 실행

```bash
pip install -r requirements-dev.txt
export ANTHROPIC_API_KEY=...        # 없으면 수치만
python -m cot.main                   # 최신 주차
python -m cot.main --date 2026-07-28 --force
pytest -q                            # 합성 데이터 테스트 (네트워크 불필요)
```
