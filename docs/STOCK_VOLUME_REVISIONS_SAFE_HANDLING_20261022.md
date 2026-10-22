# FreeQuantLab — 공개 주식 공급원 거래량 사후 정정 격리 (2026-10-22)

## 발견

2026-10-22 오전, 신설 `freequant-stock-history.service`의 실제 첫 실행이 `LONG_VENDOR_HISTORY_REVIEW_REQUIRED`로 종료코드 2를 반환했다. **이것은 권한 문제가 아니라 보호 조건에 따른 실패 차단이다.** 기존 원장 및 가격을 수정하지 않았다.

불일치 6건 전수 원문 검사:

- KRX 삼성전자 005930, SK하이닉스 000660, KODEX 200 069500: 2026-10-21 **거래량**이 수집 이후 사후 정정됐다.
- 미국 AAPL, MSFT, SPY: 2026-10-20 **거래량**이 공급원 재조회 때 증가했다.
- 6건 모두 OHLC **시가/고가/저가/종가 가격은 변하지 않았다**. 가격 바뀜 0.
- 수정 전 거래량과 새로운 응답을 `conflicts`와 `raw_history`에 append-only 보존했다. 이미 저장된 원본 가격·거래량을 덮어쓰지 않는다.
- 별도로 기록된 2,187건의 Yahoo adjusted-close 부동소수점 정밀도 흔들림(유의미한 조정가격 차이 0)도 그대로 보존한다.

## 코드 계약

`stock_long_history.py`가 두 집합을 엄격히 구분한다.

1. **거래량만 다른 경우**: OHLC 4개 필드가 정밀하게 일치하고 거래량만 다를 때에만 `LIVE_BASELINE_VOLUME_REVISION`/`LONG_VENDOR_VOLUME_REVISION` 증거를 추가한다. 보관된 최초 행을 수정하지 않는다. `LONG_VENDOR_HISTORY_WITH_VOLUME_REVISIONS_NOT_TRADING`으로 종료코드 0, 그러나 '총수익률/인과적인 거래량 변수 모델 인증 완료'는 여전히 **false**.
2. **OHLC 가격 또는 실질 조정가격 차이 / 원본 SHA 오류 / 누락**: `LONG_VENDOR_HISTORY_REVIEW_REQUIRED` 또는 `LONG_HISTORY_OR_SOURCE_INTEGRITY_REVIEW_REQUIRED`로 계속 종료코드 2, 명시적인 재검토. 사전 증거를 바꿔 정상이라고 표시하지 않는다.

`stock_evidence_quality.py`에 `vendor_volume_revisions_stored_immutably` 및 `VENDOR_VOLUME_REVISIONS_REQUIRE_VOLUME_FEATURE_RECONCILIATION` 항목을 추가한다. **주식 Qlib 모델·향후 90일 가상관측 등록 및 실거래는 계속 차단**한다. 수정된 공급원 거래량이 거래량 변수의 과거 가용 시점/가격 유동성에 미치는 영향을 검증하기 전에는 거래량을 모델에 공급해 승인할 수 없다.

## 실제 DB 적용 및 CI

- 기존 기록된 `LIVE_BASELINE_MISMATCH` 6건도 원본 JSON의 OHLC만 비교하여 **역호환 방식으로 volume-only**로 분류한다. 과거 증거 원문을 변경하지 않는다.
- 재조회한 새 원본도 append-only로 기록할 수 있으며, 같은 수정된 응답을 반복해도 `UNIQUE(market,ticker,kind,day,payload_sha)` 계약으로 중복 증거가 추가되지 않는다.
- 관련 테스트: 거래량만 정정·동일 공급원 재수집 중복 제거·가격 close 정정은 실패 유지·구버전 volume-only 증거 인식.
- CI 검증 통과 후 운영 main 업데이트·고정 관측기 실행, 기존 사용자 systemd 타이머 유지.

## 안전 및 후속

현재 신설 주간 타이머 다음 예약은 2026-10-24 11:40 KST. 데이터 가격을 임의 보간·수정하지 않고, 실주문과 가상거래 스위치를 켜지 않는다.

**원본 주식 장기 가격이 8,022개 있다는 사실은 공식 거래소 또는 발행사 기업행동 가격원 확인·수익률 승격의 근거가 아니다.** 거래량 사후 정정도 데이터 품질에 관한 경고로 계속 보존한다.

## 08:53 KST 자동 주식 일일 수집 추가 회귀와 완전 복구

재부팅 직후 `freequant-stock-observer.timer`가 실행한 일일 `stock_daily_observer --capture`도 마지막 거래일 거래량 정정 6종목을 공급자 전체 가격 수정으로 처리하고 exit2로 중단됐다. 따라서 본 PR은 **장기 원장에 한정하지 않고 기존 자동 일봉 관측기의 가격/거래량 분류를 동일하게 보완**한다.

- daily `daily_bars` 최초 OHLCV는 절대 덮어쓰지 않는다. `revision_alerts`의 기존 원문 JSON에 원본 값·새로운 관측 값·새 payload SHA를 append-only 보존.
- 거래량만 다른 경우: `VENDOR_VOLUME_REVISION_RETAINED_NO_TRADING` 및 전체 `STOCK_DAILY_CAPTURED_WITH_VOLUME_REVISIONS_NO_TRADING`으로 명시적 경고 처리, **수집 서비스 계속 실행 가능**.
- 가격 OHLC가 다른 경우: 기존 `HISTORICAL_PROVIDER_REVISION_BLOCKED` 및 전체 exit2를 유지.
- 읽기 전용 일일 감사의 `vendor_volume_only_revision_evidence`와 `historical_price_revision_alerts`를 분리. `stock_evidence_quality`에도 `DAILY_VENDOR_VOLUME_REVISIONS_REQUIRE_VOLUME_FEATURE_RECONCILIATION` 게이트를 기록. **Qlib 가상거래/성과 발행은 여전히 차단**.
- 이미 예전 버전이 생성한 `revision_alerts`의 6개 원문도 JSON OHLC 동등성으로 분리되므로 과거 증거를 변경하지 않는다.
- 수정 코드로 실제 2026-10-22 09:01 KST 공개 일봉 재수집 성공, 6종목 가격 수정 0, 거래량 수정 원본 보존, raw SHA 확인 PASS. 기존 코인 서비스/모델/영구 거래 상태 무변경.

## 테스트 시계 경계 회귀

전체 회귀 2026-10-22 UTC 기준 실행 중 기존 `tests/test_websocket_incremental_archive.py::test_no_current_day_by_default_until_the_next_utc_day`가 시간 경과에 따라 실패함을 확인했다. 과거 고정된 fixture 날짜(2026-10-21)가 이미 UTC 어제가 되어 '오늘은 자동으로 제외'라는 assertion이 잘못 발동한 것이며 운영 WebSocket 증분 로직은 불변이다. 테스트 기준 UTC 시계만 fixture의 동일 일자로 freeze하여 **달력 날짜가 바뀌어도 deterministic**하게 개선했다. 수정 후 WSL **전체 413 PASS**.
