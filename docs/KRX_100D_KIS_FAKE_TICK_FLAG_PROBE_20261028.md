# KIS KRX 100거래일 누락 분봉: 허봉 포함 플래그 실조회 대조 (2026-10-28)

## 조사 목적
현재 자료의 7개 종목·거래일에서 누락된 13개 KRX 정규장 1분 시간 라벨이 KIS의 `FID_FAKE_TICK_INCU_YN`(허봉 포함 여부) 설정이나 수집 당시 일시적인 응답 문제 때문인지 분리한다.

KIS 공식 예제는 `inquire-time-dailychartprice`의 `fid_fake_tick_incu_yn`을 허봉 포함 여부로 정의하고 기본값을 `""`로 제공한다. 이는 **실제 거래가 없었던 시간의 허봉을 진짜 체결 기록으로 승격하라는 의미가 아니다.**
- 공식 예제: https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_time_dailychartprice/inquire_time_dailychartprice.py
- 공식 API 설정: https://github.com/koreainvestment/open-trading-api/blob/main/MCP/Kis%20Trading%20MCP/configs/domestic_stock.json

## 실제 KIS API 대조
- 읽기 전용 KRX J 분봉 조회, 거래 주문 API 호출 없음
- 전수 감사 원본: `krx100_source_triage_20261028_r1`
- 기존 원본: `venues_cohort_20261026_run1`, 7개 종목·거래일 모두 SHA 보존
- 분봉 설정 비교: `FID_FAKE_TICK_INCU_YN=""` vs `"Y"`
- 8개 동일 (종목, 거래일, 커서) 쿼리 쌍, 총 **16회 실제 HTTP GET**
- 목표 누락 시간 라벨: **13개**, 두 모드에서 **추가 관측 0개**
- 실제 응답 16개 SHA 저장, 로컬 오프라인 대조: **8쌍 모두 같은 날짜의 분봉 시간 라벨 집합과 OHLCV 값이 일치**
- 단일 커서당 KIS 최대 120건 응답이며, **하루 전체의 독립 완전한 재수집이나 거래소 체결 원장 검증은 아니다.**
- 시작 직후 누락 예: 삼성전자 7월 31일(09:00/09:01), SK하이닉스 6월 25일·7월 15일(09:00/09:01), 7월 31일(09:00/09:01/09:03)
- 장중 누락: 삼성전자 6월 1일 15:00; SK하이닉스 7월 3일 14:18, 7월 8일 10:14/14:30.

## 결론과 안전 경계
이번 16회 공식 API 재조회 결과 **허봉 포함 옵션이 13개 누락 라벨을 복원하지 못했다.** 응답 차이가 관측되지 않았다는 의미이지 원인이 실제 무체결인지, 수집 서버 보관 공백인지, 다른 공급사 기록과 충돌하는지까지 증명하지 않는다. 새 1분봉 값/거래량을 생성·보간하지 않는다. 원본 품질 차단과 주문 불능 상태를 유지한다.

또한 KIS의 `stck_cntg_hour`는 체결 시간 필드지만 **분봉의 시작/종료 라벨 기준이 문서로 확정됐다는 증거는 부족**하다. 이 결론은 15:32 종가 라벨 5건에도 적용된다. 기존 일봉 종가와 가격이 일치한다는 사실만으로 15:32를 정규장 15:30 체결로 조정하지 않는다.

### 불변 파일
- 실제 KIS 응답/보고서: `/home/hong/apps/FreeQuantLab/state/intraday_research/krx100_gap_flag_probe_20261028_r1/`
- 별도 저장 응답 재감사: `/home/hong/apps/FreeQuantLab/state/intraday_research/krx100_gap_flag_pair_audit_20261028_r1/`
- 각 `probe_report.json`, `pair_audit.json`은 원본 SHA-256의 연쇄로 연결됨. 보고서 자체의 `.sha256` 검증을 지원한다.
- 새로운 코드: `quantlab.intraday_krx_gap_flag_probe`, `quantlab.intraday_krx_gap_pair_audit`
- 90일 운영 평가·실계좌 주문·모의매매 자동 실행은 변경하지 않음.

## 후속 우선순위
1. KIS 측에 `stck_cntg_hour`의 분봉 구간 정의, 09:00/09:01 결측 및 15:32 종가 라벨 의미를 공식 문의하거나 체결 틱 원장과 대조.
2. KRX 공식 거래중단·재개 이벤트의 실제 전달 시각과 장중 단일가 종료 시점을 별도 확인.
3. Time-in-force, 호가잔량 및 매수 후 보유 포지션의 청산 불능을 시간순 체결 시뮬레이터에 반영하되, 미확인 구간 수익은 계속 `null` 처리.
4. 충분한 미사용 새 기간에서 거래비용·슬리피지·중단 위험을 검증하기 전까지 전략을 가상/실거래로 승격하지 않음.
