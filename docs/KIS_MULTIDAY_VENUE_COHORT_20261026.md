# FreeQuantLab: KIS 2종목 × 3거래일 × 3시장 분봉 품질 코호트 (2026-10-26)

## 목적

실제 확인된 삼성전자 2026-10-23 KIS 통합 분봉의 **09:07 거래량 -160주 / 09:08 거래량 -384,351주 및 가격범위 불일치**가 다른 종목·거래일에도 발생하는지 조사한다. 일봉 거래량이 KRX+NXT 합과 맞아도 통합 1분봉에는 오류가 발생할 수 있으므로 **과거 이력의 분봉·거래소·거래시간 정합성**을 별도 검증한다.

공식 KIS `FHKST03010230` 조회는 `FID_COND_MRKT_DIV_CODE=J/NX/UN`를 지원한다. https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_time_dailychartprice/inquire_time_dailychartprice.py

### 배치 기준

| 항목 | 대상 |
|---|---|
| 주식 | 삼성전자 `005930`, SK하이닉스 `000660` |
| 일자 | 2026-10-21(수), 22(목), 23(금) |
| 출처 | KIS J(KRX), NX(NXT), UN(통합) |
| 연구 대상 | 2종목 × 3일 × 3시장 = **18개 불변 시장 스냅샷** |
| API 사용 | 종목·일자·시장별 과거 분봉 페이지 + 공식 일봉, 읽기 전용 |
| 호출 제어 | GET 간 최소 **1초**, 시장 데이터셋 간 **2초** 추가 휴식, 단일 직렬 처리 |
| 제한 | 최대 3종목·5과거 평일·30개 데이터셋, 시장별 실패 3건이면 중단, 제한 오류 즉시 중단 |
| 주의 | 위 한도는 우리의 보수적 프로그램 설정이지 **KIS 공식 할당량 보장**이 아니다 |

### 1. 네트워크 없이 수집 계획 확인 (실행 완료)

```bash
cd /home/hong/apps/FreeQuantLab-intraday-20261026
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
ROOT=/home/hong/apps/FreeQuantLab/state/intraday_research

"$PY" -m quantlab.intraday_kis_cohort_capture \
  --out-dir "$ROOT/venues_cohort_20261026_run1"
```

기본값은 **DRY_RUN**이다. 실제 KIS 인증정보를 읽지 않고, API 호출·파일 작성도 하지 않는다. WSL에서 18개 `NOT_COLLECTED` 확인 완료.

### 2. KIS 인증정보가 있는 WSL 로컬 셸에서만 실제 수집

보안 제약상 ChatGPT 원격 작업 도구로 KIS 비밀키를 보내는 인증 호출은 하지 않는다. 비밀키를 채팅·GitHub·스크립트 인수에 붙여 넣지 않는다.

```bash
cd /home/hong/apps/FreeQuantLab-intraday-20261026
set -a
source ~/.config/freequantlab/kis.env
set +a

PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
ROOT=/home/hong/apps/FreeQuantLab/state/intraday_research

"$PY" -m quantlab.intraday_kis_cohort_capture \
  --symbols 005930,000660 \
  --sessions 2026-10-21,2026-10-22,2026-10-23 \
  --markets J,NX,UN \
  --out-dir "$ROOT/venues_cohort_20261026_run1" \
  --pacing-seconds 1.0 \
  --execute
```

- `--execute` 없이는 **실제 수집하지 않음**.
- 18개 각각의 API 원본 및 SHA-256을 기존 `KIS_J_EXT_<symbol>_<session>` 등 시장별 불변 경로에 저장한다.
- 이미 있는 경로는 검증 후 `EXISTING_VERIFIED`로 넘어간다. 원본 해시가 틀리면 **실패로 차단**, 덮어쓰지 않음.
- `batch_progress.json`에는 시장·날짜·종목별 진행 결과만 기록하며 API 키/토큰/공급사 `msg1`을 적지 않는다.
- 일시적으로 실패해도 기존 완료 파일은 유지되고 재실행으로 검증 후 재개할 수 있다. 과도한 API 호출이 감지되면 재시도 횟수를 늘리거나 무한 반복하지 않는다.
- 실제 거래소 휴일은 현재 별도의 공식 캘린더와 대조하지 않는다. 휴일/비거래 데이터는 `FAILED_CLOSED` 또는 미수집 처리하고 결코 0건 정상 거래로 간주하지 않는다.

### 3. 수집된 실제 자료를 전부 **오프라인** 평가

```bash
"$PY" -m quantlab.intraday_kis_cohort_audit \
  --source-root "$ROOT/venues_cohort_20261026_run1" \
  --symbols 005930,000660 \
  --sessions 2026-10-21,2026-10-22,2026-10-23 \
  --out-dir "$ROOT/venue_cohort_reports_20261026_run1"
```

비교 결과는 `cohort_summary.json` 및 각각의 `cases/KIS_J_NX_UN_<symbol>_<session>/audit.json`에 저장한다. 시장 3개가 모두 확보된 종목·날짜만 총 검사 분봉 수, 분봉 불일치율(분모 명시), 공식 일봉 합계 및 가격 고저가 오류 집계에 포함한다. 누락된 세션은 `excluded_missing_cases`에 별도 기록한다. 전체 평가는 **실제 과거 데이터 수집 성공/실패와 무관하게 매매 또는 백테스트 승격 금지**.

### 이미 수행한 실측 스모크 테스트

현재 저장된 기존 삼성전자(005930) 2026-10-23 KIS 3시장 자료만 가지고 위 오프라인 코호트 CLI를 독립 실행했음.

```text
requested_case_count = 6
complete_three_market_case_count = 1
incomplete_three_market_case_count = 5
checked_integrated_minute_labels = 691
minute_volume_mismatch_count = 2
minute_price_envelope_mismatch_count = 1
usable_for_backtesting = false
```

실측 보존 파일:
`/home/hong/apps/FreeQuantLab/state/intraday_research/cohort_baseline_existing_20261026/cohort_summary.json`.

### 후속 통과 기준

1. 서로 다른 최소 3일·2종목의 실제 **세 시장 자료가 모두 확보**되어야 시장별 이슈 발생률을 의미 있게 볼 수 있다.
2. 거래시간과 분봉 시간라벨 집계 계약(`stck_cntg_hour`), `cntg_vol` 집계 범위, KRX/NXT/UN 장후 거래 및 일봉 차이 설명 필요.
3. 시장별 차이가 남아 있으면 통합 분봉을 **매매전략 데이터로 자동 승격하지 않음**. 공급사 확인·분봉 수정 이력 검증, 거래정지·액면분할/권리조정·슬리피지/호가 비용 검토가 후속 우선순위다.
4. 기존 FreeQuantLab 90일 평가·운영 브랜치·주문 서비스는 건드리지 않는다.
