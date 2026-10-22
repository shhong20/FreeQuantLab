# FreeQuantLab — 청정 90일 첫 대상일 해시 봉인 게이트 (2026-10-22)

## 목적

기존 `freequant-clean-shadow.timer`는 2026-10-23 **09:24 KST**부터 첫 마감 일봉에 따른 가상관측을 처리하고, 별도 `freequant-clean-watchdog.timer`는 **10:50 KST**에 90일 전체 누적 관측의 원장·호가·누락을 검사한다.

추가로 **첫날 사건 6개·증거 호가 2개를 하나의 불변 해시 영수증으로 봉인**한다. 이것은 사전등록과 인과성 확인을 위한 운영 증거일 뿐, 실거래 체결·알파·90일 전략 성능 승인이 아니다.

## 실행 흐름

`freequant-clean-watchdog.service`를 기존 단독 실행에서 아래의 두 연속 실행(oneshot)으로 확장한다.

1. 기존 `quantlab.clean_cohort` — SQLite `mode=ro`, 사전등록 프로토콜/전략 지문/원장 포지션 재구성/인과적 timestamp/공공 ticker/누락 및 사전 주문 검사. 기존 감사 JSON·알림 DB는 계속 유지. `INCOMPLETE`면 exit2 → 다음 단계 실행하지 않음.
2. 신규 `quantlab.clean_first_day_attestation` — 현 시각이 **2026-10-23 10:20 KST 전**이면 `WAITING_FIRST_DUE_NOT_YET_MATURE`, 별도 첫날 승인 영수증은 생성하지 않고 exit0. 해당 시각 이후에만 첫날 6개 `shadow_events`·KRW-BTC/KRW-ETH 각각 하나의 `shadow_quote_evidence`·6개 사전등록 계좌 ID를 독립 read-only 트랜잭션으로 대조해 SHA256 해시 봉인.

첫날 최초 승인 영수증:

`/home/hong/apps/FreeQuantLab/reports/clean-daily/first-day/first-day-2026-10-22.json`

내용은 사전등록 protocol SHA, 대상 UTC 일자, 6 account-event canonical SHA, 2 quote canonical SHA, 6개 동결 계좌 지문 SHA, 시간대/공공 ticker 출처/실주문 불가 선언만 포함한다. 현금·잔고·수익률 숫자는 별도 영수증에 표시하지 않는다.

### 안전 조건

- 파일은 exclusive `O_CREAT|O_EXCL`, mode 0600, 원문+디렉터리 fsync. 동일 원본을 다시 확인하면 기존 파일 내용과 비교하고 중복 생성하지 않는다.
- 이미 생성된 영수증이 다른 바이트로 변했거나 증거가 수정됐다면 **덮어쓰지 않고 fail-closed**. 의심스러운 기록은 관리자가 별도 사고 분석을 해야 한다.
- 손상된 시장가·불완전한 첫날·비정상 전략/계좌·호가 시간대·뒤늦은 재부팅·중복 데이터 등은 승인 금지. 누락이라면 기존 Watchdog 알림과 `INCOMPLETE` 보고는 남기되 소급 주문 생성은 절대 하지 않는다.
- 운영 SQLite는 `mode=ro`로 읽기만 한다. 쓰기 가능한 경로는 기존 감사 보고서 `reports/clean-daily` 아래 별도 첫날 영수증뿐이다.
- 기존 `freequant-clean-watchdog.timer`의 10:50/12:50 + systemd startup 스케줄, `freequant-clean-shadow.timer`의 09:24/09:34/09:44/10:10 스케줄, 사전등록 프로토콜, 기존 계좌, Exaone/Qlib/주식/WebSocket 타이머는 변경하지 않는다.
- `ProtectSystem=strict`, `ProtectHome=read-only`, `NoNewPrivileges=true` 유지, `sudo` 불필요.

## 2026-10-22 실제 운영 근거

- 10:50 KST·12:50 KST 청정 Watchdog **둘 다 실행 성공**. `WAITING_FIRST_DUE`, 활성 경고0, 현재 누락0.
- WSL 실제 청정 원장 6개 계좌, 최초 현금 각 100만원, 가상 사건0, ticker 증거0.
- 정식 첫 대상 `2026-10-22 UTC`는 `2026-10-23 09:00 KST`에 일봉 마감, 최초 관측 09:24 KST, 관측 기한 10:19 KST, 독립 누락 검사 10:50 KST.
- 이날 이전에 `python -m quantlab.clean_first_day_attestation`은 정상 WAITING만 출력하고 **영수증 파일을 생성하지 않아야 한다**.
- 개발 시 오프라인 합성 데이터 코호트에서 6 이벤트·2 호가를 기록하여 테스트하면 첫날 승인 SHA 작성 및 재실행 무변경에 성공한다. 사전 이벤트·변조 호가·원본 파일 변조·심볼 누락·시간대 이상은 실패.

## 명령

```bash
cd /home/hong/apps/FreeQuantLab-clean-runtime
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
$PY -m quantlab.clean_first_day_attestation

systemctl --user show freequant-clean-watchdog.service \
  -p Result -p ExecMainStatus -p ExecStart
systemctl --user list-timers --all freequant-clean-shadow.timer freequant-clean-watchdog.timer
journalctl --user -u freequant-clean-watchdog.service -n 25 --no-pager
```

**2026-10-23 10:50 KST 이후**에는 아래를 반드시 실데이터로 검증해야 한다: 실제 사건 수 6, 실제 Upbit 공공 ticker 원본 종목별 하나씩, `shadow_events`·`shadow_quote_evidence`의 원장/인과성 품질, SHA 봉인 영수증 생성, 동일 자료 재실행 무변경, 실주문0/모델승격 불가, 별도 일반 코인 원장 불변.

90일 평가는 **2027-01-20 09:00 KST** 전에 산출·승격하지 않는다.

## 첫날 SHA 승인 실패의 독립 경고 채널

SHA 봉인 단계만 실패해도 독립 `clean-first-day-alerts.db`에 `CLEAN_FIRST_DAY_SHA_ATTESTATION_BLOCKED` 경고가 저장된다. 24시간 이내 재실행 중복은 제거하고, 검증을 통과한 후에만 자동 해제한다. 일반 `clean-watchdog.db`의 활성 경고는 수정하지 않는다. 별도 감사 이력은 `reports/clean-daily/first-day/first-day-alert-history.json`에 출력한다. 이는 Windows 토스트 성공을 주장하지 않는 로컬 경고 원장이다.
