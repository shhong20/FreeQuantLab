# FreeQuantLab KRX 주문 상태·체결 증거 검증 (2026-10-28)

## 문제 정의
기존 Point-in-Time(PIT) 5분봉 리플레이는 `HYPOTHETICAL_NEXT_OPEN_ENTRY`와 `HYPOTHETICAL_CLOSE_BEFORE_AUCTION`을 사용한다. 이 이벤트들은 분봉 시가를 **가정**한 연구 시나리오이며 브로커 주문접수·실체결·취소·잔고의 증거가 아니다. 따라서 종목·전략별 'FLAT' 시나리오 상태도 실제 청산 확인으로 사용할 수 없고, 다음 날 중복 재진입의 정당성이 되지 않는다.

## 이번 구현
### 1. 순수 연구 원장: `quantlab.intraday_krx_order_evidence`
- 기존 SHA-256이 맞는 `krx100_pit_replay_20261028_r3/pit_replay.json`만 읽는다.
- 종목·전략별 `SIGNAL_AFTER_COMPLETED_BAR`를 받아 **외부 주문 증거 대기**로 표시한다. 실제 주문 송신 이벤트가 아니다.
- 다음 봉 가상 진입은 `EXPOSURE_UNCONFIRMED`로, 가상 청산은 `unconfirmed_exit_observations`로 기록한다. 가상 가격·수량을 broker-confirmed 자산으로 승격하지 않는다.
- CB/원본 분봉 공백 진입 및 보유 불명확 상황은 `HALT_RECONCILIATION_REQUIRED` 또는 `GAP_RECONCILIATION_REQUIRED`로 격리. CB 종료·다음 날 도래만으로 현금화하거나 자동 재주문하지 않는다.
- 증거 없는 후속 진입 시뮬레이션은 "quarantined"로 분류한다. 이 분류는 운영 주문 차단 스위치를 연결한 결과가 아니며, **독립 연구 감사**다.
- 계좌 잔고·실현손익·연속 복리수익·MDD·샤프는 모두 확정하지 않는다. 원본, 매매 데이터베이스, 기존 90일 평가 변경 없음.

### 2. 브로커 주문 응답 테스트용 순수 상태 기계: `quantlab.intraday_krx_execution_state`
실제 KIS 주문 API와 연동하지 않는다. `fixture_origin=SYNTHETIC_TEST` 외의 입력은 거절한다. 테스트 케이스로만 다음을 평가한다.

| 합성 입력 시나리오 | 기대 결과 |
|---|---|
| 주문 접수/부분체결 `ACK→FILL_CUMULATIVE` | 누적 체결 수량 증가, 범위 제한 |
| `CANCEL_REQUEST` | **취소 완료가 아님**, 미체결 수량 남음 |
| `VENUE_HALT→VENUE_REOPEN` | 자동 체결·자동 청산 없음, 재조정 필요 |
| `ORDER_STATUS_SNAPSHOT` | 테스트 원장의 명시적 확인이 있어야 불확실 상태 해소 |
| `CANCEL_CONFIRM` | 최종 누적 수량이 기존 체결보다 작을 수 없음 |
| 중복 동일 이벤트 ID | 멱등 처리 |
| 충돌 ID·시각 역행·허용치 초과 | 거부 |
| 완료된 주문에 신규 취소/오픈 상태 | 거부 |

이 시뮬레이터의 `synthetically_confirmed_cumulative_fills`는 테스트 상 값이며 실제 주식 계좌 보유 수량이 아니다. `actual_filled_shares=null`, `realized_pnl=null`, `trading_authorized=false`를 고정한다.

## 실제 100거래일 원장 검사
- 원본 연구 기간: 2026-05-28~10-23, KRX 삼성전자(005930)·SK하이닉스(000660), 200개 종목·거래일.
- 원장의 종목 × 전략 흐름: **8개**. 브로커 체결 증거 미확보: **8개**.
- 기존 PIT가 나중에 재진입을 가정한 건: **39건**, 외부 증거 없으므로 격리 대상으로 계산.
- 외부 청산 증빙 없이 OHLCV 시가로 가정 청산한 건: **44건**.
- 이미 운영 계좌에서 발생한 실제 주문 또는 체결: **이번 연구 경로 0건**.
- 이 숫자는 '실전 주문 39건이 차단되었다'는 의미가 아니고, 기존 PIT 가정 흐름을 회계·체결 증빙 기준으로 다시 분석한 결과다.

## 재현
```bash
P=/home/hong/apps/FreeQuantLab/.venv/bin/python
R=/home/hong/apps/FreeQuantLab/state/intraday_research
$P -m quantlab.intraday_krx_order_evidence \
  --pit-dir "$R/krx100_pit_replay_20261028_r3" \
  --output-dir "$R/krx100_order_evidence_20261028_r1"
```

결과는 불변 `order_evidence.json` 및 `order_evidence.sha256`에 저장한다.

## 다음 실증 게이트
1. **실제 주문이 없는 과거 KIS 분봉만으로 매수·매도 체결 확정은 불가능**하다. 주문/체결 관련 증거는 사용자가 소유한 별도의 브로커 익명화된 실제 내역이나 별도 실제 호가/틱 이력 데이터가 있어야 한다. 임의 주문 실행으로 증거를 만들지 않는다.
2. KIS `stck_cntg_hour` 분봉 시작/종료 라벨, 15:32 종가 인식, 공식 KRX CB·재개 타임라인이 확인되지 않으면 시가 기반 가정 체결 신뢰도를 승격시키지 않는다.
3. 미래 별도 장중 호가·체결 모니터링은 read-only로 구축하고 보유 포지션의 미체결·재개 조건에 대한 모의 체결 범위(상·하한)를 평가할 수 있으나, 가격 보간이나 결측 구간의 가상 매도를 정답으로 삼지 않는다.
4. 운영 90일 평가 `d6b531e`, 브로커 주문·자동 가상매매 시스템 연결은 변경하지 않는다. 위에서 분류한 8개 미확정 계좌 상태는 연구용 시나리오 상태이고 실제 계좌 상태가 아니다.
