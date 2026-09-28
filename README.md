# CFTC COT 주간 분석 — 자동화

글로벌 매크로 헤지펀드 CIO 시각의 스마트머니 포지션 분석을 **매주 자동으로** 생성해 GitHub Pages 웹사이트로 게시합니다.

```
토요일 09:37 KST (GitHub Actions)
 ├─ [prepare] Step 1 CFTC 데이터 조회 (공식 API → 실패 시 CFTC 히스토리 파일)
 │            Step 2 기준일(화) 검증 — 불일치 시 게시 안 함, 일·월·화 자동 재시도
 │            지표 계산 (Python): Net/전주대비/진단/연속주차/3·5년 백분위/COT Index/z-score/퀀트 점수
 │            RSS 헤드라인 수집 (Fed·BLS·EIA·ECB·BOJ + Google News의 Reuters/Bloomberg/FT/WSJ) — 무료, 키 불필요
 ├─ [Claude Code Action · Pro 구독 토큰]
 │            Step 2.5 뉴스 서치 (WebSearch/WebFetch) → work/news.md
 │            Step 3  CIO 분석 → work/analysis.json  (수치는 계산값만 사용)
 └─ [render]  결과 검증 → HTML 대시보드 → docs/ 커밋 → GitHub Pages 배포
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
- **아카이브 페이지(`index.html`)**: 주차별 점수 히트맵과 누적 수치 기록 표

## 주차별 수치 기록 (자동 누적)

`docs/data/cot_record.csv`에 기존 구글 시트 "COT 주차별 수치 기록"과 **같은 열 순서**로 매주 한 행씩 자동 누적됩니다
(날짜 · ES/NQ/WTI/EUR/JPY/BTC Net · 각 점수 · 투자의견 · 핵심메모). 2026-06-23 ~ 08-18 수기 기록도 포함되어 있습니다.

- 같은 주차를 다시 생성하면 해당 행만 갱신됩니다.
- 사이트 목록 페이지 하단에 표로 표시되고, CSV로 내려받을 수 있습니다.
- **구글 시트 자동 연동**: 빈 시트의 A1 셀에 `=IMPORTDATA("https://janbakbakee.github.io/woong/data/cot_record.csv")`
  → 복붙 없이 항상 최신 기록이 표시됩니다 (구글이 약 1시간 주기로 갱신).

## AI 상담용 요약본

`https://janbakbakee.github.io/woong/latest.md` — 항상 최신 주차의 텍스트 요약(시장별 수치·백분위·점수·의견·확률,
그룹별 포지션, CIO 판단, 최근 12주 기록). 매매 상담 시 이 URL을 Claude에게 주면 됩니다.
주차별 파일은 `reports/<기준일>.md`.

## 알림 (텔레그램 / ntfy)

리포트가 게시되거나 실행이 실패하면 휴대폰으로 알림을 보냅니다. Secrets에 설정한 채널로만 전송됩니다.

| 채널 | Secrets | 설정 |
|---|---|---|
| 텔레그램 (추천) | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | @BotFather → `/newbot` → 토큰 발급 → 만든 봇에게 아무 메시지 전송 → `https://api.telegram.org/bot<토큰>/getUpdates` 에서 `"chat":{"id":…}` 값 확인 |
| ntfy (가입 불필요) | `NTFY_TOPIC` | ntfy 앱 설치 → 추측하기 어려운 토픽 이름(예: `cot-woong-8f3k2`)을 구독 → 같은 이름을 Secret에 등록 |

## 설정 방법 (최초 1회)

API 크레딧 없이 **Claude Pro/Max 구독**으로 분석합니다.

1. **구독 토큰 발급 (PC에서 1회)**
   - Windows PowerShell: `irm https://claude.ai/install.ps1 | iex` 로 Claude Code 설치
     (Mac/Linux: `curl -fsSL https://claude.ai/install.sh | bash`)
   - `claude setup-token` 실행 → 브라우저 로그인 → 출력된 `sk-ant-oat...` 토큰 복사
2. **GitHub에 토큰 등록**: repo → Settings → Secrets and variables → **Actions** → New repository secret
   - Name: `CLAUDE_CODE_OAUTH_TOKEN` / Secret: 위 토큰
   - (배포 키 메뉴 아님 주의)
3. **GitHub Pages 켜기**: Settings → Pages → Source를 **GitHub Actions**로 선택
4. **기본 브랜치에 병합** 후 Actions 탭 → "COT 주간 리포트" → **Run workflow**
   - 과거 주차: 기준 주차 칸에 `2026년 9월 2주차` 또는 `2026-09-08` 입력
     (N주차 = 그 달의 N번째 화요일 = CFTC 기준일. 날짜를 넣으면 그 주 화요일로 자동 변환)
5. 결과: `https://<사용자명>.github.io/<repo>/`

- 분석은 구독 사용 한도에서 차감됩니다 (주 1회 실행).
- (선택) Variables에 `COT_MODEL`(예: `opus`, `sonnet`)을 넣으면 모델 지정.
- 토큰이 없거나 Claude 단계가 실패해도 수치·차트·RSS 헤드라인 리포트는 게시됩니다.
- API 키 방식도 지원: 로컬에서 `ANTHROPIC_API_KEY` 설정 후 `python -m cot.main`.

## 데이터 원칙 (프롬프트 규칙 반영)

- 기준일이 맞지 않거나 조회에 실패하면 **이전 데이터나 추정치로 대체하지 않습니다**. 게시하지 않고 다음 예약 시간에 재시도합니다.
- 일부 시장만 확인되면 그 시장만 분석하고, 나머지는 "데이터 수신 대기 중"으로 표시합니다. 다음 실행 때 자동으로 다시 만듭니다.
- 뉴스는 Reuters, Bloomberg, FT, WSJ, Fed, BLS, BEA, EIA, BOJ, ECB, Farside, CBOE, CME 등 공신력 사이트만 근거로 쓰도록 지시합니다. 확인되지 않은 내용은 "확인 불가"로 표기합니다.
- Claude가 쓴 JSON은 게시 전에 검증합니다: 누락 필드는 수치 기반 기본값, 점수는 퀀트 ±15점, 확률 합 100 강제.

## 로컬 실행

```bash
pip install -r requirements-dev.txt
export ANTHROPIC_API_KEY=...        # 없으면 수치만
python -m cot.main                   # 최신 주차 (API 키 없으면 수치만)
python -m cot.main prepare           # Actions와 동일: work/prompt.md 생성 → Claude Code로 실행 후
python -m cot.main render            #   결과 렌더링
python -m cot.main --date "2026년 7월 4주차" --force
pytest -q                            # 합성 데이터 테스트 (네트워크 불필요)
```
