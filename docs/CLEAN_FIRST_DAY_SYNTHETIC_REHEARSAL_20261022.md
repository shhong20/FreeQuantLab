# FreeQuantLab — 사전등록된 청정 90일 첫날 모의 운영 리허설 (2026-10-22)

## 목적 및 실제 상태

2026-10-21 01:57 UTC에 6개 KRW-BTC/ETH × buy&hold / MA20/80 / factor_r5 계좌를 처음부터 독립 등록했다. 2026-10-22 UTC 일봉은 2026-10-23 **09:00 KST**에 비로소 마감되므로, 실제 첫 정상 관측은 **2026-10-23 09:24 KST** scheduled retry이다.

2026-10-22 오전, live clean DB에는 6개 등록 계좌·0개 이벤트·0개 공공 ticker 증거. Ledger/integrity/frozen account proof PASS, `clean_cohort`는 `WAITING_FIRST_DUE` 및 issues=[]; 2개 timer 활성. 다음날 실제 마감 전에는 절대 소급 가상거래를 실행하지 말 것.

이 PR은 첫날 전에 사용할 **완전히 합성된 데이터만 사용하는 임시 운영 리허설**을 추가한다. 실제 시장 결과·투자 성과·알파·실주문 가능성을 검증하는 프로그램이 아니다.

## 리허설 설계 및 안전

`python -m quantlab.clean_first_day_rehearsal`은:

1. 진짜 원장 `/home/hong/apps/FreeQuantLab/state/clean_shadow_90d.db`를 **SQLite URI mode=ro**로 읽어 처음 개설한 계좌 6개, 지문·시작현금·포지션0, 원장 PASS/공공 ticker 없음 검증. 프로토콜 원문 SHA는 `6f5e9cf6333d3dd5fe4339ef9d7ba401df36bc361915584bf45226f464cc5054`.
2. 2026-10-23 09:24 KST를 합성 UTC clock으로 설정하고, **미래 데이터를 조회하지 않고** 별도의 demo_daily 320개 합성 일봉을 양쪽 종목의 입력으로 사용한다. 두 종목의 public ticker도 **demo 마지막 종가로 만든 가짜 quote**만 사용한다. 실제 Upbit·증권 API 요청 없음.
3. SQLite `Connection.backup()`으로 live DB를 Python `TemporaryDirectory` 안의 **임시 DB로만 복사**한다. 계좌 ID/설정은 원본 그대로 복사하지만 실제 live DB를 열어 쓰지 않는다.
4. 임시 DB에 첫날 BTC/ETH × 3계좌 **6개 가상 사건, 2개 가짜 종목 ticker 증거**가 atomic batch로 기록되는지 확인한다. frozen protocol, strict UTC close/time, independent ledger replay and clean Watchdog (`PASS`), 중복 같은 날 재실행 0추가, 5시간 후 늦은 재부팅 0추가를 검사한다.
5. 아무것도 수집하지 않은 임시/실원장을 다음날 10:21 KST 기준으로 검사해 **6개 계좌 누락 = INCOMPLETE**를 감지하는지 검사한다. 임시 DB의 저장된 quote를 고의 변조해 감사가 INCOMPLETE를 반환하는지도 검사한다.
6. 모든 검증 후 임시 DB를 **즉시 폐기**한다. 원본 live DB를 다시 열어 등록 계좌/현금/이벤트가 리허설 전후 동일한지 비교한다. 절대 현실 시장가·계좌 주문·실제 paper transaction을 기록하지 않는다.
7. `--db`에 임의의 테스트 fixture 원장도 지정할 수 있지만 동일 검증을 적용한다. 기존 이벤트/호가가 들어갔거나 원장 무결성/지문이 달라진 경우 FAIL CLOSED. 첫날이 이미 지나면 합성 리허설이 사후 증명인 것처럼 보이지 않도록 수행 거부. 운영 환경에서는 **첫날 전 1회만** 사용하는 명령이다.

기본 출력은 JSON `status=SYNTHETIC_FIRST_DAY_REHEARSAL_PASS_NOT_REAL_MARKET`. `synthetic_virtual_order_count_not_performance`는 임시 DB만의 가짜 가상주문 횟수일 뿐 **실제 수익률/실제 주문·체결·미래 결과가 아니다**. 전체 결과 `source_database_unchanged=true`, `discarded_synthetic_database=true`, `live_network_queried=false`, `real_broker_orders=0`.

## 운영에서 실제로 확인한 리허설

2026-10-22 오전 고정 사전등록 원장을 읽어 실행:

```bash
cd /home/hong/apps/FreeQuantLab-clean-runtime
/home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.clean_first_day_rehearsal
```

- 임시 6개 synthetic first-day account-events, 2개 synthetic public-quote evidence 생성.
- 동일 일자 재실행 중복 0, 늦은 시작 0.
- 첫날 누락 및 quote 변조에 Watchdog `INCOMPLETE`.
- 원본 real clean events 0, 원본 현금과 포지션 보존.
- 테스트 DB는 자동 폐기.
- 네트워크/브로커/실주문 없음.

## 남은 실제 검증

**2026-10-23 09:24 KST 이후**: `freequant-clean-shadow.service` 실제 public Upbit closed daily candles/ticker를 사용한 여섯 가상 account-day 기록 및 두 quote 증거, 중복 0, 실제 타임스탬프 09:10~10:19 사이, 고정 전략 지문 변경 0 여부 확인.

**2026-10-23 10:50 KST**: `freequant-clean-watchdog.service`의 실제 독립 누락/quote/ledger 검증 및 `reports/clean-daily/2026-10-23.json` 감사. 마감 후 누락되었다면 소급 가짜 주문을 만들지 말고 경고·불완전 기록으로 영구 남겨야 한다.

**90일 성과는 2027-01-20 09:00 KST 전에는 평가 불가**. 그 이후에도 수동 판단만 허용한다. 기존 original/Exaone/Qlib/stock/WebSocket 기록은 어떤 경우에도 이 리허설과 섞지 않는다.
