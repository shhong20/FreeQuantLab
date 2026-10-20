# Shadow Paper 데이터 품질 · 원장 무결성 · 수동 전략 심사

작성: 2026-10-20 · 서버 `DESKTOP-S51Q473` · FreeQuantLab 연구용 시스템

## 1. 시세 품질 검사

`quantlab/quality.py`를 사용해 BTC·ETH 일봉을 **가상 체결 전에 검사**합니다.

- **UTC 일봉 시각**: 현재 UTC 날짜의 전날까지 완료된 데이터인지 확인, 마지막 100개 일봉 날짜 연속·오름차순·중복 없음
- **OHLCV**: 가격·거래량 유한성 검사, 시가/고가/저가/종가 모순 차단, 최근 20일 거래량이 모두 양수인지 확인
- **이상 급변**: 전일 종가 대비 일간 종가 2배 초과 또는 1/2 미만의 데이터는 수동 확인까지 차단
- **가상 주문 호가**: 거래소의 현재 공개 체결가와 직전 완료 일봉 종가의 비율이 1.8배 초과 또는 1/1.8 미만이면 즉시 가상 체결 차단
- **원천 응답 검증**: Upbit API 페이지에서 동일한 UTC 거래일이 두 번 반환되면 중복 제거로 은폐하지 않고 오류 처리
- 해당 검사들은 **진위·품질 보증이 아니라 의심 데이터 차단 장치**입니다. 급격한 실제 시장 변동도 보수적으로 차단할 수 있으며, 실거래에는 사용하지 않습니다.

실시간 데이터가 불량하면 `shadow_events` 가상 체결은 생기지 않습니다. `shadow_runs`에 실패가 기록되며 이후 Watchdog에서 `LATEST_RUN_FAILED`과 `MARKET_DATA_QUALITY_REJECTED`를 표시할 수 있습니다. 이미 등록된 전략의 조건·현금은 변경하지 않습니다.

## 2. 원장 감사

`quantlab/ledger_audit.py`의 `audit_shadow_ledger()`는 원장을 **SQLite read-only**로 엽니다. 검사 항목:

- 저장된 `track_id`와 불변 전략 규칙/계정 설정 해시의 일치
- 초기 자본부터 이벤트를 순서대로 재생하며 가상 BUY/SELL 수량, 비용, 잔고·보유량, 평가금액 계산
- 등록 이전 거래, 비연속 날짜의 역방향 거래, 누락된 세션, 음수 잔고, 사건·계정 마지막 상태 불일치, 정지 상태 확인
- `PRAGMA quick_check` 기반 SQLite 무결성 검사
- 원장 손상이 의심되면 `SHADOW_LEDGER_INTEGRITY_FAILED` 중대 알림 기록 및 **일일 수익 보고서 새로 생성하지 않음**. 자동 수정·원장 재계산 덮어쓰기·정지 복구 없음

참고: 거래를 빼먹은 날이 있어도 이전 거래가 재계산된다는 뜻은 아닙니다. 해당 날짜는 원래 없던 것으로 남으며 `missing_intermediate_utc_days`에 표시하고 심사 준비를 보류합니다.

## 3. 전략의 수동 심사 준비 기준 (자동 투자 승인 아님)

`evaluate_manual_review`는 **최소 요건 충족 여부만** 나타내며, 전략의 실효성 또는 미래 수익성을 증명하지 않습니다.

| 심사 기준 | 설정 |
|---|---|
| 불변 규칙/가상 잔고 원장 재검산 | 모두 통과 |
| 실제 미래 관측일 | 90개 이상 |
| 등록 후 중간 UTC 관측 누락 | 0개 |
| 기록된 가상 BUY/SELL 건수 | 최소 8건 |
| 위험 정지 여부 | 정지 없음 |
| 관측 최대 낙폭 | -12%보다 작지 않음 |
| 동일 관측 날짜의 20% 비중 매수·보유 비교 | 정확히 같은 날짜 |
| 매수·보유 대비 단순 초과수익 | 양수 |

모든 기준을 충족하더라도 `READY_FOR_HUMAN_REVIEW`일 뿐이며, 투자 승인이나 종목 교체를 자동으로 하지는 않습니다. `strategy_auto_promotion=false`, `any_broker_order_permission=false`는 항상 유지됩니다. 후보가 많을 경우 다중 비교 문제와 미래 데이터 독립성 검증은 별도 필요합니다.

현재 초기 등록 단계(2026-10-20)에서는 BTC·ETH 각 3개 계정이 `NOT_READY`인 것이 정상입니다.

## 4. 운영 절차

```bash
cd /home/hong/apps/FreeQuantLab
.venv/bin/python -m pytest -q
.venv/bin/python - <<'PY'
from quantlab.ledger_audit import audit_shadow_ledger
from quantlab.forward_reports import report
from quantlab.ledger_audit import evaluate_manual_review
r = audit_shadow_ledger('state/shadow.db')
print(r['status'], r['accounts'], r['events'], r['issues'])
if r['status'] == 'PASS':
    summary = report('state/shadow.db')
    assessment = evaluate_manual_review(summary, r)
    print([(x['symbol'],x['strategy'],x['research_maturity'])
           for x in assessment['strategies']])
PY
```

정기 `freequant-watchdog.timer`는 장부를 읽어 위 검사를 실행한 뒤, 통과하면 `reports/forward/YYYY-MM-DD.json`에 `ledger_integrity`와 `manual_review`를 추가합니다. 대시보드의 **일일 전략 성과 보고서**에 심사 상태가 표시됩니다.

`qlib-compare`에도 별도 *연구 데이터셋*의 중복 시각·순서·OHLCV 품질 검사를 적용합니다. 국내주식은 휴장일/주말이 있으므로 코인 UTC 매일 연속 규칙을 적용하지 않습니다. 원본 Qlib Alpha158/Alpha360 다종목 성과를 재현한 것은 아닙니다.

## 5. 범위 한계

단일 코인 공개 시세의 가격/거래량 자체가 조작되었는지 독립 거래소로 확인하지 않습니다. 호가 스프레드·시장 충격·주문 거절/부분 체결은 반영되지 않습니다. 수집 API 서비스 약관과 상장 폐지·데이터 누락/정정 이력도 별도로 관리해야 합니다. 사용자 자금이 들어간 실거래 시스템에 적합한 감사/리스크 시스템이라고 판단할 수 없습니다.
