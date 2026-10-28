# KIS 실제 API 응답 형식 현장 점검 및 거래일 증빙 한계 (2026-10-28)

## 검증 환경
- WSL Linux + 시각: 2026-10-28 약 20:31 KST (KRX 정규장 이후)
- 실제 KIS 계정의 기존 공유 OAuth 캐시 토큰 사용, **신규 OAuth 발급/주문 API 호출 없음**.
- 삼성전자 `005930`, 시장 `J`에서 **읽기 전용 GET 총 3회**, 인증정보·HTTP 응답 본문 출력·저장하지 않음.
- 조회 당시 호가·시장 체결·1분봉 모두 **HTTP 200, `rt_cd=0`, `msg_cd=MCA00000`** 반환.
- 장 마감 후 자료이므로 이 결과를 정규장 **신선한 호가/거래일별 실측 또는 실제 주문 증거**로 분류하지 않음.

| KIS 시장 데이터 API | TR ID | 실제 JSON 구조 | 현장 특징 |
|---|---|---|---|
| `inquire-asking-price-exp-ccn` (10호가) | `FHKST01010200` | `output1` 객체, `output2` 객체 | `aspr_acpt_hour`, 매수/매도 1~10호가 가격·잔량 필드 확인 |
| `inquire-ccnl` (최근 체결) | `FHKST01010300` | `output` 배열 30행 | `stck_cntg_hour`, `stck_prpr`, `cntg_vol` 확인. **실제 이 응답에는 `stck_bsop_date` 필드 없음** |
| `inquire-time-itemchartprice` (당일 분봉) | `FHKST03010200` | `output1` 객체, `output2` 배열 30행 | `stck_bsop_date=20261028` 날짜 필드 확인 |

공식 한국투자증권 샘플:
- https://github.com/koreainvestment/open-trading-api/blob/main/legacy/Sample01/kis_domstk.py
- https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_time_itemchartprice/inquire_time_itemchartprice.py

## 수정 배경
장 마감 실측 최근 체결 응답의 **시각만 있고 거래일 필드가 없는 형태**는 현재 앱이 실제로 받는 KIS 응답과 동일하다. `HHMMSS`와 수신 시각이 근접하다고 해도 그것은 최근 거래일을 **독립 검증한 증거가 아니다**. 10월 29일의 실측 수집은 시간 신선도 제한과 독립 감사 조건을 계속 적용하되, 수집 종목·동일 시각·영수증·시장 거래일 신뢰수준을 구별해야 한다.

## 코드 변경
- `quantlab.intraday_krx_joint_evidence._decode_market`: 원본 틱 행에 `stck_bsop_date`가 모든 행에 있는지 `provider_business_date_field_on_all_rows`로 명시. 누락·혼합이면 **false**. 실제 KIS 원본에서 거래일을 임의로 추가하지 않음.
- 반환 `market_date_independently_certified=false`: **날짜 필드가 있더라도 공급사 주장일 뿐 외부 거래소 독립 증빙은 아니다.**
- 최근 체결 및 분봉의 **최신 관측 시각·수신 대비 경과초**를 정상화·감사 파일에 추가.
- 3종 독립 비교의 각 표본에 `tick_business_date_field_on_all_rows`, `minute_business_date_field_on_all_rows`, `trade_date_independently_certified=false` 기록. 전체 감사에 `sample_triplets_with_all_trade_date_fields`, `sample_triplets_without_trade_date_fields` 합산.
- 틱이 미래 거래일 등 명시된 잘못된 날짜를 제공하면 기존대로 **거부**한다. 동일 HHMMSS 또는 SHA 일치만으로 체결 날짜를 보증하지 않는다.
- 이 변경은 읽기 전용 연구 자료에만 적용. 계좌·주문·90일 운영 평가 불변.

## 유효성 검증
- 단순 모의 형식 뿐 아니라 **실제 API에서 확인한 최근 체결 날짜 필드 누락 형태**를 테스트 케이스에 포함.
- 날짜 필드가 일부 행에만 존재하는 **혼합 응답**을 전행 확인으로 오인하지 않도록 검사.
- 기존 12개 시세 응답 모의 E2E·원본/정규화 SHA·오프라인 재감사·동적 비용 연동 테스트와 함께 검증.
- 다음 실제 수집 **2026-10-29 09:43·12:35·14:35 KST**에 작성되는 원본/감사/최종 보고서에 날짜 증빙 범위가 명확히 기록된다.
- 실제 계좌 체결률, 전략 실수익률과 무관하며 여전히 `broker_orders=0`·`strategy_returns=null`.

### 중요한 해석 제한
체결 틱에 날짜 필드가 누락된 시세라도 수신 시각과 가까우면 시장 관측 참고자료로서 활용할 수 있다. 하지만 이 경우 **해당 시각 실제 주문 체결·진짜 거래일·원자적 호가/틱 동시성을 모두 입증하지는 않는다**. 10월 29일 독립 수집의 성공은 소스·시각·가격 범위 검증을 뜻할 뿐 주문 실행 승인조건이 아니다.
