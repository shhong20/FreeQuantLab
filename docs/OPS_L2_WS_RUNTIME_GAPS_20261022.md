# FreeQuantLab 감사 P1 개선: 실시간 관측 공백 + 운영 런타임 증거 (2026-10-22)

## 확인된 실제 장애와 원인

2026-10-22 21:18 KST 검증 기준, L2(REST) 최근 144개 중 83개 확보/61개 공백(57.6%), WebSocket 최근 144개 중 84개 확보/60개 공백(58.3%). 공백의 주요 59개 연속 슬롯은 2026-10-21 22:40 KST~2026-10-22 08:20 KST 시작 슬롯이며, WSL 세션 종료(2026-10-21 22:38 KST)~재시작(2026-10-22 08:32 KST)와 일치합니다. 그 밖에도 일부 불완전·실패 슬롯이 있습니다. 외부 Upbit 실제 과거 주문장·체결 정보를 복구한 것으로 간주할 수 없습니다.

**새 읽기 전용 CLI:**

```bash
cd /home/hong/apps/FreeQuantLab-clean-runtime
/home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.ops_integrity
```

출력: 실제 DB의 L2와 WS 최근 24시간 성공/누락 슬롯, 연속 공백 UTC 시작·종료, 사라지는 **최조 가능 시점**(이후 새 공백이 없을 경우), 17개 운영 service의 실제 systemd WorkingDirectory/마지막 Result/exit code, 3개 런타임의 Git commit 해시와 원본 경로 일치 여부.

- 오류 종료코드 2는 데이터 품질 기준 미충족으로 인정하고, 별도 독립 감사의 실제 `PASS`가 없으면 성공으로 보고하지 않습니다.
- 기존 90%/연속 누락 제한/원본 SHA 검증과 24h 품질 기준은 수정하지 않았습니다.
- CLI는 SQLite `mode=ro` + `query_only`와 `git rev-parse`, `systemctl show`만 사용하며 서비스 재시작, 보고서·원장 작성, 호가 소급 복원은 없습니다.
- `--without-runtime`은 Git/systemd 없이 원본 DB만 확인하며, 서비스 정상 상태로 읽으면 안 됩니다.
- **WS 수집은 10분마다 30초 표본**으로 실제 연속 체결·전체 호가창 또는 자기 주문 체결을 증명하지 않습니다.
- 시간창이 지나도 검사가 통과할지 예측할 수 없습니다. 출력의 `historical_gap_exits_rolling_window_not_before_utc`는 오직 해당 시점까지 기록된 **과거 관측 공백**이 계산 구간에서 빠질 가장 이른 시점이며, 새 결측·원본 품질 문제·거래소 불통은 계산하지 않습니다.

## 배포 분리

현재 실제 운영은 `FreeQuantLab`(핵심·Exaone/Qlib/일반), `FreeQuantLab-observer-runtime`(시장/WebSocket/주식), `FreeQuantLab-clean-runtime`(청정 90일) 3개 Git SHA로 분리되어 있습니다. 이는 당장 모두 한 SHA로 `git switch`해 해결할 문제가 아니라, 기능·DB·프로토콜별 사전 호환 확인을 요구하는 배포 관리 과제입니다. 관측 CLI를 최신 청정 런타임에 배포하더라도 다른 두 런타임을 변경하지 않습니다.

## 다음 조치

1. 익일 WSL이 계속 실행된 구간의 새로운 144슬롯을 수집한 후 두 독립 품질 게이트를 다시 검사
2. 독립 24h 연속 관측이 유효하지 않으면 중간 결측/통신/시간 동기화 문제 조사; 실패 표시를 숨기지 않기
3. 다중 worktree 배포 manifest 및 불변 SHA 적용/롤백 절차 별도로 구현
4. 이미 시작한 청정 90일 protocol, live order OFF, auto promotion OFF는 변경하지 않음
