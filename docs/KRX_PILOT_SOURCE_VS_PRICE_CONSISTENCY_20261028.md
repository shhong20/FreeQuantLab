# KRX 3종 수집: 원본 검증과 시장가격 정합성 분리 (2026-10-28)

## 왜 필요한가
2026-10-29 장중 KIS 10호가·시장 체결·1분봉을 3회 구간에서 수집한다. 기존 독립 감사의 `VERIFIED_RESEARCH_ONLY` 및 전체 구간 완료 `ALL_THREE_WINDOWS_VERIFIED_RESEARCH_ONLY`는 SHA-256 검증과 원본 해석 완료를 의미하지만, 실제로 대응된 체결과 1분봉이 **0건**이거나 가격 범위를 벗어난 틱이 있어도 추가 설명 없이 성공으로 오해할 수 있다.

체결 API에는 날짜 필드가 없는 경우가 실제 확인됐고, KIS 1분봉 라벨의 시작/끝 의미도 미확정 상태다. 따라서 대응 자료가 없거나 가격이 불일치해도 은폐하지 않고 시장 품질과 원본 무결성을 별도로 표시해야 한다.

## 적용 변경
- `quantlab.intraday_krx_joint_pilot_review.py`: 실측 원본·감사 SHA가 맞고 원본 12개 요청의 시각/종목이 맞는 경우는 그대로 `VERIFIED_RESEARCH_ONLY`(출처 검증)로 분류.
- `matched_tick_minute_row_count=0`이면 별도 시장 비교 품질은 `NO_COMMON_TICK_MINUTE_LABELS`, 가격 일치율은 `null`. **0/0을 100% 일치나 정상 가격으로 처리하지 않음.**
- 일치한 체결틱은 있지만 대응 분봉의 고가·저가를 벗어난 관측이 있으면 `OBSERVED_TICK_PRICES_OUTSIDE_MINUTE_RANGE` 및 실제 일치 비율. 동시 호가/틱 캡처의 시간차, 공급사 분봉 라벨 의미 등이 원인일 수 있으므로 원인과 체결 가능성은 확정하지 않음.
- 관측된 매칭 틱이 전부 분봉 고저 범위에 있는 경우도 `ALL_OBSERVED_MATCHES_WITHIN_CANDLE_RANGE_NOT_CERTIFIED`. **계좌 체결이나 독립 거래일·시각 의미 인증은 아님.**
- 대응 매칭 수보다 분봉 가격 범위 내 틱 수가 더 많으면 자료 모순으로 `EVIDENCE_FAILED_VERIFICATION`.
- 최종 감사에서 `source_verified_windows_not_cross_source_price_certification=true`, `trade_day_independently_certified=false`, `minute_label_start_end_semantics_independently_certified=false`, `safe_to_calibrate_historical_strategy_fills=false` 기록.
- 10월 29일 유효 구간의 동적 표시 호가 비용 보고서도 **실측 가격 비교 진단 상태**를 전달하고 기존 미검증 100거래일 주문 슬리피지로 소급 보정하지 않음.

## 실제 수집에 대한 영향
- 기존 2026-10-29 09:43·12:35·14:35 수집, 15:45 종료 감사, 매시 정각 WSL 복구 예약은 변경하지 않음.
- 최초 원본·KIS 인증·거래소 요청·실주문 기능에 변경 없음.
- 오프라인 감사만 변경. 10월 29일 실제 수집된 파일이 만들어졌을 때 원본 SHA를 다시 검증하고 추가 품질진단을 합산.
- `broker_orders=0`, 확정 매매수익률·샤프·체결률 없음, 기존 90일 운영 평가 `d6b531e` 유지.
