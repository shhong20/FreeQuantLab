# FreeQuantLab — 24시간+ 실시간 호가·공개 거래 프린트 수집기 (2026-10-21)

## 목적·검증 한계

기존 첫 8회/14초 호가 관측은 거래 시간대 다양성이 없었다. 이번 작업은 공개 Upbit REST **Quotation GET**만으로 BTC/ETH/XRP/ADA 네 시장을 한 번에 읽고, 뒤이어 각 시장의 최근 체결 프린트(최대 200건)를 읽어 **10분마다** 하나의 원자적 읽기 전용 증거 묶음으로 기록한다. 이 기록은 하루/야간/시간대별 호가·공개 거래 유동성 연구용이다.

**24시간이 실제로 경과하고 페어별 100개 이상 구별되는 관측 및 90% 이상의 슬롯 성공률이 확인되기 전에는 충분한 호가 검증을 선언하지 않는다.** 24시간·100개 조건을 충족해도 `DEPTH_COVERAGE_OBSERVED_NOT_REAL_FILL_CALIBRATION`이며 실제 주문 대기열·자기 주문 체결 성공률·시장충격 보정 완료는 아니다.

## 현재 구현

- 읽기 전용 호출:
  - `GET https://api.upbit.com/v1/orderbook?markets=KRW-BTC,KRW-ETH,KRW-XRP,KRW-ADA&count=30`
  - `GET https://api.upbit.com/v1/trades/ticks?market=KRW-...&count=200` × 4
- **별도 데이터베이스** `/home/hong/apps/FreeQuantLab/state/microstructure/observations.sqlite3` (SQLite WAL, FULL sync, 모든 원본 API 바이트 및 SHA-256 보존, 운영 `shadow.db` 와 분리).
- 한 UTC 10분 슬롯당 하나의 4호가+4체결 응답 완성본만 커밋. 같은 슬롯 재실행은 네트워크 재조회 없이 `ALREADY_CAPTURED`. 주문이 아닌 데이터 **원자적 증거 묶음** 저장.
- 응답 페어 일치·호가 bid/ask 비역전·단계별 단조 정렬·양의 가격/수량·12초 이하 호가시각 편차, SHA-256·체결 ID·최신 체결 10분 내 존재 검사. 응답 중 하나라도 실패하면 묶음 저장 안 함; 실패 기록은 `capture_failures`에 독립 기록.
- 수신시각, 거래소 호가 timestamp, 인접한 공개 체결 프린트 시각을 구별. 단순 시간 근접성 및 두 호가 관측 시각 사이 체결 건수/방향을 계산하되 **실제 체결된 자신의 주문으로 오인하지 않음**. 최근 200건 캡으로 인해 구간의 거래가 누락됐을 가능성 및 반복 반환된 체결 ID 중복을 표시.
- 휴면, 재부팅, 네트워크 실패로 수집되지 않은 **과거 orderbook은 복원할 수 없음**. 관측이 없는 UTC 10분 슬롯을 누락으로 집계. 다른 시각의 호가를 소급 복제하지 않음.
- 연구 보고서: 지난 **48시간** 상세 원본 무결성 감사와 주문 크기 10만/100만/1천만/1억원의 매수/매도 30호가 깊이 견적. 전체 보유 슬롯 및 누락 구간 수는 별도 간단 집계. 전체 과거 정밀 재감사는 수동 `--full-history`를 사용 (자동 30분 감사가 무한히 느려지는 것 방지).
- 스프레드·깊이 데이터는 **표시된 잔량**일 뿐 대기열 위치·취소·지연·숨은 유동성·타인 선행 체결까지 검증할 수 없다.
- 인증/API 키가 필요 없고 `POST`/주문 API 미사용. 실제 투자·자동 승격·기존 운용 모델 변경 없음.

## 10분 간격 배포

운영 실서버는 현 WSL. user systemd timer는 10분마다 **KST/UTC 분 00,10,20,30,40,50의 :30초**, 정확히 한 번 수집 시도한다. `OnStartupSec=90s`와 `Persistent=true`로 **WSL이 다시 시작한 뒤 수집을 재개**하지만, PC가 꺼져 있는 사이의 Level 2 호가 데이터는 백필 불가능하다. Windows 자체가 꺼져 있다면 아무 서비스도 돌지 않는다는 제한도 동일하다.

- `deploy/systemd/freequant-microstructure.service`: `flock -n` 중복 프로세스 방지, `NoNewPrivileges`, user 서비스, 엄격한 파일시스템 보호, 전용 상태 경로만 쓰기.
- `deploy/systemd/freequant-microstructure.timer`: 매 10분 + 시작 후 재개.
- `deploy/systemd/freequant-microstructure-audit.service` / `.timer`: 매 30분 예외·SHA·누락 슬롯 및 최근 30분 유효 관측 확인. 최근 자료가 30분보다 낡으면 exit 2로 실패를 남겨 journald/서비스 상태에 노출.
- 수집기 파일은 `quantlab/microstructure_observer.py`, 시험은 `tests/test_microstructure_observer.py`.

배포 후 상태 확인:

```bash
systemctl --user status freequant-microstructure.timer freequant-microstructure-audit.timer
systemctl --user list-timers '*microstructure*'
journalctl --user -u freequant-microstructure.service -n 25 --no-pager
journalctl --user -u freequant-microstructure-audit.service -n 25 --no-pager
cd /home/hong/apps/FreeQuantLab-microstructure-20261021
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.microstructure_observer \
  --audit --require-fresh-seconds 1800 \
  --db /home/hong/apps/FreeQuantLab/state/microstructure/observations.sqlite3
```

### 검증 수준

- 테스트: 성공한 5개 원본 응답의 원자적 기록, 재실행 중복 억제, 인위적인 API 실패 후 재시도, 오래된 호가 거부, 해시 변조 거부, 매칭 시각/체결 ID 중복, 오프라인 누락 슬롯, 가짜 24시간 실험의 승격 차단, 관측이 오래 끊긴 경우 watchdog 실패 경로.
- 실서버 최초 수집: 실제 Upbit 공개 API의 4시장 호가와 각 200건 시장 거래를 하나의 SQLite 원장에 기록. 이후 10분 새 슬롯 실저장 및 중복 억제, 실제 해시 감사 수행.
- 저장·계산 코드는 **과거 기본/Exaone/Qlib 실전 전략의 신호 생성·원장·자동 실행 타이머를 전혀 변경하지 않음**.
- 장기간 관측의 실제 적합성은 다음 날 이후의 **서로 다른 시간대** 데이터가 쌓여야 평가할 수 있다. 테스트 데이터로 24시간 운영 성공을 가장하지 않음.

## 배포/재현 참고

```bash
cd /home/hong/apps/FreeQuantLab-microstructure-20261021
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python -m pytest -q
# 새 슬롯 한 건 read-only 조회 (기존 슬롯이면 idempotent)
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.microstructure_observer \
 --db /home/hong/apps/FreeQuantLab/state/microstructure/observations.sqlite3
# 상세 전체 기간 검증 (분석 부담이 큼)
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.microstructure_observer \
 --audit --full-history --db /home/hong/apps/FreeQuantLab/state/microstructure/observations.sqlite3
```

추가 개선 과제: 실제 orderbook WebSocket + trade WebSocket 구독으로 시간 정렬 범위를 촘촘하게 만들고, 누락률·지연·재연결을 검증. 데이터 보유량이 커지면 immutable 압축 아카이브·파티션·장기 감사 범위를 추가. 실제 호가 수집은 **특정 시점의 가격 깊이 관측**이지 주문의 최종 체결 사실을 증명하지 않는다.
