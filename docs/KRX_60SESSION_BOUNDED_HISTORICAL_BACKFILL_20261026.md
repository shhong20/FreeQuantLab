# FreeQuantLab — KRX 60거래일 과거 분봉 수집 계획·재개·5분봉 검증 (2026-10-26)

## 실제 상태와 목표

연구용 별도 WSL: `/home/hong/apps/FreeQuantLab-intraday-20261026`

기존 운영 프로젝트: `/home/hong/apps/FreeQuantLab` — **90일 평가에 영향 없음**.

2026-10-23까지 완료된 KRX 정규장을 기준으로 `exchange_calendars` 패키지의 **XKRX 거래 세션 달력**으로 최근 60개 세션을 구성한다. 해당 달력은 KRX의 실시간 공식 배포/임시 휴장 공지 자체가 아니므로, 공급사 응답의 거래일 진위·정규장 380개 분봉은 **개별 원본 검증**으로 다시 확인해야 한다.

실제 WSL에서 아래 불변 계획을 이미 생성·해시 검증했고, 외부 HTTP/KIS OAuth를 호출하지 않았다.

`/home/hong/apps/FreeQuantLab/state/intraday_research/krx60_backfill_plan_20261026_r1/plan.json`

| 항목 | 확인된 결과 |
|---|---:|
| 대상 | 삼성전자 `005930`, SK하이닉스 `000660`, KRX **J 시장만** |
| 거래일 후보 | **2026-07-27 ~ 2026-10-23**, 각 60 세션 |
| 필요한 시장 원본 | **120개** |
| 기존 KIS J 불변 원본 (SHA 검증) | **6개** |
| 추가 필요한 원본 | **114개** |
| 배치 | **12개 × 최대 5거래일/배치** |
| 최종 배치 #11 | 10/19~10/23, 기존 **6개**, 신규 **4개** |
| 전략 워크포워드/자동매매 | **미실행·차단** |

시장제도상 2026-09-14 전후 정규장 외 거래 규칙이 변경된 기간을 가로지른다. 기존 18개 J/NX/UN 원본 자체를 수정하지 않고 원본 디렉터리에 새로운 `J` 세션 폴더만 추가한다. 기존 `J`, `NX`, `UN` 스냅샷은 동일 SHA 재검증 후 건너뛴다. batch 진행 이력은 기존 원본 루트가 아니라 ***계획 폴더 아래*** `progress/batch_XX/batch_progress.json`에 기록한다.

## 구현

`quantlab.intraday_krx_backfill`

1. `--init`은 **인증 없이** XKRX 세션 달력으로 대상 60개 날짜와 12개 수집 배치를 `plan.json`/`plan.sha256`에 **불변 저장**. 이미 계획 폴더가 있으면 덮어쓰지 않는다.
2. `--batch N`은 기본적으로 DRY RUN. 해당 날짜의 기존 해시 검증 후 조회해야 할 KRX J 원본 수만 표시. `--execute`를 붙이면 **명시된 최대 5거래일 배치**에 대해서만 로컬 KIS 읽기 전용 API 사용.
3. `--all-batches`도 기본 DRY RUN. `--execute`와 함께 로컬 WSL에서만 사용하면 각 **5일 배치를 순서대로 실행**한다. 배치 사이 2초 쉬고, 개별 HTTP는 최소 1초 간격으로 직렬 처리한다. 첫 공급사 실패/인증 거절 즉시 정지한다.
4. 재실행하면 기존 원본의 **raw minute/daily + 저장된 SHA**를 독립 검사하고 없는 세션만 계속 수집. 진행기록과 KIS 인증정보는 분리된다.
5. 모든 요청은 **KIS 시장자료 GET**, 시장 코드 J만 허용. 인증 토큰/키는 환경변수에서만 사용하고 보고서에 저장하지 않는다. 주문·가상매매·운영 SQLite 수정 불가.
6. 날짜 캘린더 상 거래일 후보가 공급사 기준 휴장이거나, 종목 정지/원본의 분봉 누락이 있으면 자동으로 0주/완전한 하루로 보정하지 않는다. 5분봉 변환과 워크포워드는 별도 게이트.

## 이미 실행한 인증 없는 실제 미리보기

```bash
cd /home/hong/apps/FreeQuantLab-intraday-20261026
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
ROOT=/home/hong/apps/FreeQuantLab/state/intraday_research
PLAN="$ROOT/krx60_backfill_plan_20261026_r1"

"$PY" -m quantlab.intraday_krx_backfill \
  --all-batches --plan-dir "$PLAN"
# status: ALL_BATCHES_PREVIEW_NO_NETWORK
# batch_count: 12, remaining_before: 114

"$PY" -m quantlab.intraday_krx_backfill \
  --batch 11 --plan-dir "$PLAN"
# 2026-10-19~23 / existing 6 / pending 4
```

## 실제 읽기 전용 수집: 로컬 WSL 한 번 실행

**원격 ChatGPT 연결의 비밀키 보호 정책 때문에 본 대화에서 인증 호출을 직접 실행하지 않는다.** 사용자 로컬 WSL의 인증을 사용해 **아래 한 번만 명시적으로 실행**할 수 있고, 실패 시 **같은 명령**으로 중단 위치부터 재개 가능하다. 일반 운영 폴더에서 실행하지 않는다.

```bash
(
  set -e
  cd /home/hong/apps/FreeQuantLab-intraday-20261026

  set -a
  source ~/.config/freequantlab/kis.env
  set +a

  PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
  ROOT=/home/hong/apps/FreeQuantLab/state/intraday_research

  "$PY" -m quantlab.intraday_krx_backfill \
    --all-batches \
    --plan-dir "$ROOT/krx60_backfill_plan_20261026_r1" \
    --execute
)
```

조회 중 각 배치 성공·남은 종목일 스냅샷 수가 출력되며, 실패 시 임의로 무한 재시도하지 않고 중지한다. 이미 성공한 스냅샷은 인증 호출 없이 재사용한다. **실행하지 않았는데 114개 수집이 완료됐다고 보고하면 안 된다.**

단일 소량 배치로 먼저 검증할 경우:

```bash
"$PY" -m quantlab.intraday_krx_backfill \
  --batch 11 \
  --plan-dir "$ROOT/krx60_backfill_plan_20261026_r1" \
  --execute
```

## 수집 완료 후: 5분봉 60일 구조 검증

아래 변환은 기존에 구현·검증된 `quantlab.intraday_krx_5m_preview`를 사용하며, API 호출/실주문/수익률 평가를 하지 않는다. **먼저 120개 원본이 모두 정상 검증된 경우에만** 실행한다.

```bash
cd /home/hong/apps/FreeQuantLab-intraday-20261026

PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
ROOT=/home/hong/apps/FreeQuantLab/state/intraday_research
PLAN="$ROOT/krx60_backfill_plan_20261026_r1"

SESSIONS=$("$PY" - "$PLAN/plan.json" <<'PYCODE'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    print(",".join(json.load(f)["sessions"]))
PYCODE
)

"$PY" -m quantlab.intraday_krx_5m_preview \
  --source-root "$ROOT/venues_cohort_20261026_run1" \
  --baseline-root "$ROOT/venues" \
  --sessions "$SESSIONS" \
  --out-dir "$ROOT/krx60_5m_preview_run1"
```

성공하면 기대되는 정규장 후보는 2종목 × 60거래일 × 76개 5분봉 = **9,120개**이다. 그러나 이는 **달성 목표**일 뿐, 실제 수집 완료나 정확도 통과를 의미하지 않는다.

기존 백테스트 엔진 `intraday_research.evaluate`의 실행은 여전히 차단한다. 그 엔진은 **최소 60일** 외에도 KIS의 `stck_cntg_hour` 분 시작/종료 의미, 포인트인타임 수정 체결/기업행위, 봉 신호 이후 주문 시간·비용, KRX 종가 경매를 독립 검증해야 한다. 모든 자료가 확보되더라도 `usable_for_backtesting=false` 상태가 자동 승격되지는 않는다.
