# FreeQuantLab — WebSocket 자동 백업의 검증 중 신규 세션 레이스 구분 (2026-10-22)

## 실제 운영 문제

2026-10-22 09:10~09:12 KST user service `freequant-websocket-backup.service`가 신규 2세션, 1,606 public Upbit WebSocket frames를 봉인하고, WSL 및 D: 미러 **양쪽의 전체 복원·원본 SHA 검증**을 수행했다. 그러나 약 2분의 전체 복원 검증 중 새 완료 세션이 등록되어 종료시점 잔여 미러 지연 **2개 세션**이 남았다. 서비스 상태가 `MIRROR_RECOVERED_WITH_NEW_SESSIONS_AFTER_SNAPSHOT`이었고 이전 코드는 이 상태를 무조건 exit2 처리했다.

이는 D: 백업의 미러 증거가 깨진 것은 아니지만 **원본 최신 상태까지 RPO=0은 아니었다**. 기존 코드는 두 사건을 구분하지 못해 자동 백업을 장애로 표시했다. 이 문제를 해결하려고 백업 검증을 생략하거나 세션/프레임을 삭제/덮어쓰지 않는다.

## 이번 코드의 엄격한 상태 판단

`quantlab/websocket_backup_cycle.py`:

1. 주기 시작에 read-only `measure_gap`을 추가하여 **그 시점에 완료된 원본 세션 개수**를 기록한다.
2. 종전처럼 WAL 일관성 증분 봉인, 전체 소스 복원/원문 SHA 검사, D: 미러 전체 복원/원문 SHA 검사, 체크포인트까지 수행한다.
3. 종료 시 잔여 지연이 있으면 **백업 시작 시점에 완료됐던 모든 세션이 D: 미러에서 실제 복원·검증됐는지** 개수/순서/원본 SHA 증거로 판정한다.
4. 잔여 세션이 백업 진행 중 새로 생성된 소량의 증거인 경우에만 `fresh_capture_lag_only=true`로 보고하고 서비스 종료를 성공 처리한다. 적격 요건:
   - `mirrored_completed_sessions_on_second_volume >= completed_sessions_at_cycle_start`.
   - 잔여 `not_yet_mirrored_completed_sessions` **1~4개**.
   - WSL 미러 자체의 다른 미전송 세그먼트가 **0개**, 소스와 미러 compressed SHA **PASS**, 원본 최신 archived watermark가 살아있는 관측 DB와 일치.
   - 미러 마지막 완료 session부터 현재까지 경과 및 최신 완료 세션과의 차이가 **최대 2,400초(40분)**.
   - 모든 체크포인트/독립 복원 검증(종전 guard) 전부 통과.
5. 이 경우에도 원래 `MIRROR_RECOVERED_WITH_NEW_SESSIONS_AFTER_SNAPSHOT` 상태는 유지한다. **최신 원본까지 백업했다고 표시하지 않으며**, `unbacked_finalized_sessions_at_receipt` 숫자를 원문 JSON 영수증으로 남긴다.
6. 오래된 누락, 4개 초과 backlog, 40분 초과 RPO, 원본/Mirror SHA 손상, 비교 원본 바뀜, 추가 미러 미전송 세그먼트, 검증 중 예외는 반드시 **exit2**. 기존 모든 fail-closed 안전장치는 유지한다.

미러 D:는 같은 물리 컴퓨터의 다른 마운트 볼륨이며 **물리적으로 독립된 재해복구 백업이라는 주장을 하지 않는다**. 코인 Exaone·Qlib 가상 관측 및 주식 관측 코드는 무변경, 실주문도 여전히 꺼짐.

## 회귀검증

`tests/test_websocket_backup_cycle.py`에 다음 시뮬레이션 추가:

- 두 독립 아카이브 체인 실제 복원/검증을 수행하는 동안 새 완료 세션이 생겼을 때 **초기 완료 세션이 전체 미러**된 사실을 검증하고, residual=1, `fresh_capture_lag_only=True`, receipt SHA 원장 유지.
- 시작 전에 완료된 세션이 실제로 복제되지 않은 `max_batches=1` 강제 잔여 backlog는 `fresh_capture_lag_only=False`, 종료 strict failure.
- CLI 경고성 신선한 잔여 exit0 / 오래된 backlog exit2 / 잔여 없음 exit0 분리.
- 테스트의 오래된 인공 Unix timestamp는 현실 시계와 다르므로 테스트 고정 clock을 명시적으로 mock; 운영 시계는 mock하지 않음.
- source/mirror 간 전체 raw SHA/독립 복원 검증 및 rollback/역치들은 유지한다.

## 실행/모니터링

```bash
cd /home/hong/apps/FreeQuantLab-observer-runtime
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
$PY -m quantlab.websocket_backup_gap
systemctl --user show freequant-websocket-backup.service -p Result -p ExecMainStatus
journalctl --user -u freequant-websocket-backup.service -n 20 --no-pager
systemctl --user list-timers --all freequant-websocket-backup.timer
```

`status=MIRROR_RECOVERED_WITH_NEW_SESSIONS_AFTER_SNAPSHOT`일 때 `fresh_capture_lag_only=true`는 정상 종료할 수 있지만 **지연 0을 의미하지 않는다**. 해당 중복 프레임과 누락된 원본은 다음 증분 백업 주기에 보완된다. 운영 서비스는 `--run` 모드에서만 실제 append-only 백업을 만들고, 인위적인 주문이나 paper fills은 생성하지 않는다.
