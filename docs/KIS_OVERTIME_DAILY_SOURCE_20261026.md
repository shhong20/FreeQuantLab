# KIS 시간외 일자별 거래량 독립 조회 및 일봉-분봉 차이 교차 검증 (2026-10-26)

## 공식 API에서 확인한 범위

1. **주식현재가 시간외일자별주가** `FHPST02320000`: `GET /uapi/domestic-stock/v1/quotations/inquire-daily-overtimeprice`. 필수 쿼리는 `FID_COND_MRKT_DIV_CODE=J`, `FID_INPUT_ISCD=<6자리 종목>`; **과거 날짜를 지정하는 쿼리 키가 없다.** 한국투자증권 공식 예제에서 **최근일 30건만 조회 가능**이라고 설명한다.
2. 제공 데이터 `output2`에는 `stck_bsop_date`(영업일자), `ovtm_untp_vol`(시간외 단일가 거래량), `acml_vol`(누적 거래량) 등의 필드가 표기된다. 그러나 **시간외 단일가 거래량의 실제 범위를 KRX 장전/장후 시간외종가 또는 2026년 애프터마켓 전체 거래량으로 간주하지 않는다.**
3. **주식현재가 시간외시간별체결** `FHPST02310000`: `GET /uapi/domestic-stock/v1/quotations/inquire-time-overtimeconclusion`. `FID_HOUR_CLS_CODE=1` 등의 값을 사용하지만 공식 예제에서 **거래일 날짜 입력을 받지 않는다**. 따라서 2026-10-21/22/23 과거 원본을 이 API로 자동 복구한다고 주장하지 않는다.
4. 이 단계는 첫 번째 **과거 30건 API만** 활용한다. 첫 번째 API가 요청 날짜를 반환하지 않으면 그 사례는 **NOT_AVAILABLE**이며 0주라고 간주하지 않는다.

**공식 KIS 예제 원문:**
- https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_overtimeprice/inquire_daily_overtimeprice.py
- https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_overtimeprice/chk_inquire_daily_overtimeprice.py
- https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_time_overtimeconclusion/inquire_time_overtimeconclusion.py

## 개발한 코드

- `quantlab.intraday_kis_overtime_daily`: KIS `J` 시간외 일자별 조회 **기본 DRY RUN**, `--execute` 에서만 KIS 인증 및 GET 호출. 삼성전자·SK하이닉스 **2개 종목 2회 요청**. 공급사 응답의 `output2`를 날짜로 정합성 검증하고 원문 전체/해시/SHA-256/기준 날짜 목록을 **종목별 별도 불변 디렉터리**에 보존. 쿼리에 2026-10-21 등의 역사 날짜가 들어갔다고 잘못 기록하지 않는다.
- `quantlab.intraday_kis_overtime_bridge`: **인증·네트워크 미사용 오프라인** 비교. 기존 KIS `J` 공식 일봉과 보존된 분봉 합계의 차이 옆에 `ovtm_untp_vol`을 배치한다. 같은 값이 나와도 "그 차이가 100% 해당 시간외 단일가 체결"이라고 인정하지 않으며 **어떠한 OHLCV 자동 보정도 하지 않는다**.

기존 2026-10-21/22/23 삼성전자·SK하이닉스 2종목 × 3거래일 × 3시장 18개 원본 스냅샷은 읽기 전용. 90일 평가, 실주문, 운영 DB 및 기존 시세 스냅샷의 저장 위치를 변경하지 않는다.

## 1. 인증정보 없이 작동하는 사전 미리보기

```bash
cd /home/hong/apps/FreeQuantLab-intraday-20261026

/home/hong/apps/FreeQuantLab/.venv/bin/python \
  -m quantlab.intraday_kis_overtime_daily \
  --out-dir /home/hong/apps/FreeQuantLab/state/intraday_research/overtime_daily_20261026_run1
```

기본 상태 `DRY_RUN_NO_API_CALL`, KIS 비밀키 필요 없음, 파일 쓰기 없음.

## 2. WSL 사용자 로컬 환경에서 실제 read-only 조회

ChatGPT 원격 작업 도구가 KIS 인증정보를 사용하는 직접 API 호출을 보안 정책에 따라 차단할 수 있으므로 이를 우회하거나 키·토큰을 출력하지 않는다.

```bash
(
  set -e
  cd /home/hong/apps/FreeQuantLab-intraday-20261026

  set -a
  source ~/.config/freequantlab/kis.env
  set +a

  PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
  ROOT=/home/hong/apps/FreeQuantLab/state/intraday_research

  "$PY" -m quantlab.intraday_kis_overtime_daily \
    --symbols 005930,000660 \
    --sessions 2026-10-21,2026-10-22,2026-10-23 \
    --out-dir "$ROOT/overtime_daily_20261026_run1" \
    --execute

  "$PY" -m quantlab.intraday_kis_overtime_bridge \
    --overtime-root "$ROOT/overtime_daily_20261026_run1" \
    --cohort-root "$ROOT/venues_cohort_20261026_run1" \
    --symbols 005930,000660 \
    --sessions 2026-10-21,2026-10-22,2026-10-23 \
    --out-dir "$ROOT/overtime_gap_bridge_20261026_run1"
)
```

**주문 API는 호출하지 않음**. 이미 수집한 동일 종목의 불변 파일이 존재하면 원본 SHA 검사 후 `EXISTING_VERIFIED`로 넘어가고 네트워크 호출하지 않음. 오류 발생 시 기존 성공한 종목 자료는 남는다.

## 3. 결과 해석

결과 `bridge.json` 각 행에:
- `krx_daily_minus_observed_minute_volume`: 기존 KIS `J` 공식 일봉 − 역사 분봉 합계. 이미 실측된 여섯 차이는 632,089 / 153,682 / 61,890 / 294,375 / 179,083 / 231,428주.
- `overtime_reported_untp_volume`: 신규 KIS 시간외 단일가 API가 해당 **동일 날짜**로 반환한 거래량.
- `comparison_difference_gap_minus_untp_volume`: 두 수의 산술 차이. **값이 0이라도 집계 범위/거래시간이 같다는 증명은 아니다.**
- `overtime_date_in_vendor_response=false`: 최근 30건에서 해당 날짜가 누락됐다는 뜻. 0건 체결로 해석하면 안 된다.

백테스트 및 자동매매 사용은 모든 경우에 차단(`usable_for_backtesting=false`).

## 4. 공식 KIS에 추가 확인할 질문

1. `FHKST03010230` `J` 과거 1분봉의 `cntg_vol`에서 **KRX 08:30~08:40 장전 시간외종가 및 15:40~16:00 장후 시간외종가** 체결량이 제외되는가?
2. `FHPST02320000` `output2.ovtm_untp_vol`은 각 날짜 기준 **장전 시간외종가, 장후 시간외종가, 시간외 단일가(옛 제도), 2026년 16:00~20:00 애프터마켓** 중 무엇을 포함하는가? `J` 일봉 `acml_vol`에는 어떤 장 단계가 포함되는가?
3. `stck_cntg_hour`의 1분 집계 시각 기준(시작/종료)과 마지막 분봉 `19:59` 이후 20:00 경계의 체결 반영 규칙은?
4. 2026-10-23 005930 `UN` 09:08: 1분 거래량 406→407주로 소급 변경됐으나 동시간 KRX+NXT 거래량은 384,826주다. 수정 이력, 정정/취소 체결 정책 및 통합 1분봉 누락 여부를 확인할 수 있는 독립 TR/체결 이력은?
5. 2026년 9월 이후 신규 KRX 애프터마켓 데이터가 위 기존 시세 API들에 언제, 어떤 방식으로 반영됐는가?

외부 제공사 확인 전에는 간극이 설명됐다고 주장하지 않는다.
