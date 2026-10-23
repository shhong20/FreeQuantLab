# FreeQuantLab 청정 90일 첫날 최종 대사 — 읽기 전용 (2026-10-23)

## 검사 목적

10월 23일 09:24 첫 실제 가상관측, 09:34·09:44·10:10 재시도, 10:50 독립 Watchdog/첫날 SHA 영수증, 11:30 D 드라이브 온라인 스냅샷은 **각각 따로 실행**된다. 단순 서비스 종료코드 0이나 파일 존재만으로 승인해서는 안 된다.

`python -m quantlab.clean_first_day_closeout`은 *기존 원장, 첫날 SHA 영수증, 복원된 최신 로컬 백업*을 직접 읽어 서로 동일한 첫날 관측을 참조하는지 점검한다. 실주문·가상 이벤트 생성·영수증 생성·데이터 소급 보충·원본 수정·systemd 타이머 변경이 없다.

## 검사 항목

- 동결 90일 등록 프로토콜·6계좌 정보, Watchdog와 원장 회계·호가 증거 감사 상태 PASS
- 기존 `first-day-2026-10-22.json`의 프로토콜 SHA, 6계좌 이벤트/2호가 해시를 실제 원장과 재계산·대조
- 11:30 이후 백업 디렉터리의 매니페스트가 같은 SHA 영수증을 포함하는지 확인
- 백업 SQLite 원시 SHA, `integrity_check`, 복원 원장 회계 대사, 복제 영수증 SHA 및 복원 DB 이벤트·호가 해시를 `verify_backup`으로 다시 확인
- **더 새 시점의 백업에 영수증 누락이나 변조가 있으면** 과거 정상 스냅샷으로 되돌려 PASS하지 않음

예정 시점 전의 상태는 대기로 분류한다. 10:20 KST 이전에는 첫날 증거 승인 대상일이 아직 성숙하지 않아 `WAITING_FIRST_DAY_MATURITY`. 10:50 전 영수증이 없으면 `WAITING_INDEPENDENT_WATCHDOG`. 10:50 이후 영수증이 없으면 `BLOCKED_FIRST_DAY_RECEIPT_MISSING`. 영수증만 있고 11:30 예정 백업이 없으면 `WAITING_POST_AUDIT_BACKUP`, 예정 백업 시각 이후에도 누락되면 `BLOCKED_POST_AUDIT_BACKUP_MISSING`으로 처리한다.

모두 확인된 경우에만 `LOCAL_FIRST_DAY_RECEIPT_AND_BACKUP_MATCH` / exit 0. 나머지는 exit 2. 이 명령은 로컬 SQLite·SHA 무결성만 검증하므로 **체결 정확도, 90일 투자 성과, 독립 백업, 외부 시각 공증을 인증하지 않는다.**

## 운영용 명령

```bash
cd /home/hong/apps/FreeQuantLab-clean-runtime
/home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.clean_first_day_closeout
```

GitHub CI 성공 후에도 이미 시작한 청정 코호트의 기존 전략·원장·수집기·독립 감사·백업 서비스는 변경하지 않는다. 새 검증기는 기본적으로 **독립적인 수동 읽기 전용 감사 도구**이며 자동 승인/롤백과 연결하지 않는다.

## 증거 보관

첫날 영수증: `/home/hong/apps/FreeQuantLab/reports/clean-daily/first-day/first-day-2026-10-22.json`

원장: `/home/hong/apps/FreeQuantLab/state/clean_shadow_90d.db`

로컬 백업: `/mnt/d/FreeQuantLab-Backups/clean-90d/snapshot-<UTC>`

세 파일 모두 사용자의 동일 PC 저장소이며, 원격 독립 호스트에 보관된 백업이나 WORM 증거는 아니다. 기존 백업 NTFS ACL 제한은 유지한다.


## 2026-10-23 승인 순서 강화

첫 버전은 10:20 이후 수동으로 생성된 영수증과 조기 백업만으로 10:50 독립 감사와 11:30 자동 백업을 건너뛰어 통과할 여지가 있었다. 이를 방지하기 위해 다음의 검증을 추가했다.

- 10:50 KST 이전에는 영수증 파일이 존재하더라도 WAITING_INDEPENDENT_WATCHDOG 상태만 허용한다.
- 10:50 이후 독립 Watchdog의 해당 날짜 JSON 보고서에서 검사 시각, 명시적 시간대, PASS 상태, 프로토콜 SHA, 6/6 완료 계좌일, 별도 원장·호가 검증 성공을 대조한다.
- 영수증의 동결 계좌 집합 SHA, 실주문 금지 및 자동 승격 금지 값을 재검증한다.
- 11:30 KST 이전에는 유효한 조기 수동 백업이 있어도 WAITING_POST_AUDIT_BACKUP을 유지한다.
- 11:30 이후 실제 생성된 백업만 대상으로 동일한 영수증·원장 SHA와 복원 회계 검사를 수행한다. 시간대가 없는 보고서나 백업 시각은 거부한다.

로컬 보고서에 기록된 시각은 외부 신뢰 타임스탬프나 독립적 WORM 보관을 뜻하지 않는다. 현재 작업은 실주문·원장·기존 90일 전략을 변경하지 않는다.
