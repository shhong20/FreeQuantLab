# FreeQuantLab — Windows 로그인 후 WSL 관측 전 기동 및 90일 청정 코호트 준비 상태 점검 (2026-11-03)

## 실제 운영 진단 (2026-11-03 11:45 KST)

- Windows PC `DESKTOP-S51Q473`에서 기존 사용자 예약 작업 `Remote-MCP-WSL-Autostart`가 **Running**, 트리거는 사용자 로그온, 동작은 `wsl.exe --exec /usr/bin/sleep infinity`. 삭제·수정하지 않는다.
- 기존 `FreeQuantLab-WSL-Resume-Recovery`는 이벤트 `Power-Troubleshooter ID 1` 20초 후 `wsl.exe --exec /usr/bin/true`; 실제 PowerShell Audit 결과 `RESUME_TASK_CONFIG_MATCH`. 삭제·수정하지 않는다.
- WSL Ubuntu에서 `[boot] systemd=true`, `loginctl hong Linger=yes`, 시간대 KST, `freequant-clean-shadow.timer`/ `freequant-shadow.timer`/ `freequant-exaone.timer`/ `freequant-qlib-lab.timer`/ `freequant-clean-watchdog.timer` 모두 **active/enabled**, 다음 실행 예약 확인.
- 운영 청정 90일 코호트의 실제 결측은 2026-11-03 기준 누적 72 예정 계정-일 중 42건. 7개 UTC 날짜의 실행 영수증에는 `OUTSIDE_OBSERVATION_WINDOW`가 확인되었다. 이는 전원 차단 사유를 확정하지는 않으며 기존 결정이나 가상 체결을 소급 보완할 근거도 아니다.
- 종료된 Windows는 자동 작업을 실행할 수 없다. 절전 중에는 WSL 실시간 관측이 진행되지 않으며, Windows 대화형 사용자 로그인이 없을 경우 신규 예약 작업의 실행이 보장되지 않는다.

## 1. Windows 보조 WSL 기동 (운영 변경 전 감사가 기본)

`ops/windows/wsl_preopen_warm_task.ps1`의 기본은 **Audit**이고, `-Mode Install`을 명시했을 때 없는 작업만 새로 만든다.

- 신규 작업명: `FreeQuantLab-WSL-PreOpen-Warm`
- 매일 Windows 현지 시간 **08:40 / 09:05 KST**
- 수행: `C:\Windows\System32\wsl.exe --distribution Ubuntu --exec /usr/bin/true`
- 현재 사용자의 **Interactive / Limited** 토큰으로 실행. 관리자 권한·Windows 암호 수집·서비스 계정 불필요. Ubuntu 시작 요청만 수행.
- **WakeToRun=false**: Windows를 수면에서 강제로 깨우거나 켜지 않는다. 기존 전원 정책 변경 없음. 배터리 사용 여부와 무관하게 Windows가 깨어 있을 때 실행하도록 설정.
- 기존 `Remote-MCP-WSL-Autostart` 로그인 작업 및 `FreeQuantLab-WSL-Resume-Recovery` 절전 복귀 작업을 변경하지 않는다.
- 이미 이름이 같은 작업이 있을 경우 설정을 일일이 대조하고, 일치하지 않으면 **CONFIG_DRIFT** 오류로 종료한다. 덮어쓰지 않는다.
- 실제 Windows 재부팅/절전 복귀 이벤트의 **E2E 검증은 진행하지 않았다**. 예약·현재 구성 확인만 성공한 상태와 혼동하지 않는다.

PowerShell 5.1에서는 보안 정책이 파일 실행을 막을 수 있으므로 신뢰할 수 있는 저장소의 파일을 확인한 뒤 해당 **프로세스에 한해** `-ExecutionPolicy Bypass` 옵션으로 Audit/Install을 실행할 수 있다. 시스템 전체 실행 정책을 변경하지 않는다.

## 2. WSL 자체 사전 준비상태 검사

신규 Git 버전 소스:
- `quantlab.clean_preopen_readiness`
- `deploy/systemd/freequant-clean-preopen.service`
- `deploy/systemd/freequant-clean-preopen.timer`

WSL user-systemd 예약 시간:
- **08:46 KST** — Windows 08:40 WSL 시작 요청 이후 준비 상태
- **09:07 KST** — Windows 09:05 재기동 요청 이후
- **09:12 KST** — 09:15 첫 일반 Shadow, 09:18 Exaone, 09:21 Qlib, **09:24 청정 90일** 실행 이전
- `OnStartupSec=3min`, `Persistent=true`: WSL이 오후에 다시 켜진 경우에도 **늦었다는 영수증만 보존**하며 종료된 UTC 당일 가상 결정은 추가하지 않는다.

검사:
1. Git SHA로 동결된 90일 프로토콜·6개 계정 identity와 설정 확인.
2. 기존 `audit_shadow_ledger` 및 `execution_proxy_quality`를 읽기 전용 실행. 과거 42건 결측은 앞으로의 준비 상태와 별개의 평가 문제다.
3. 현재 사용자 systemd 5개 타이머가 active/enabled이고 다음 실행 예정이 있는지 확인. 로그인 셸이 아닌 Windows/서비스 환경을 대비해 per-user D-Bus 버스를 명시.
4. 비인증 **Upbit 공개 `/v1/ticker`** HTTPS GET으로 KRW-BTC와 KRW-ETH 두 시장의 API 응답 구조/양수 가격을 확인. **가격 자체를 영수증에 보관하지 않는다.** 실제 주문 API·과거 가격 백필·실제 가상매매 판단은 호출하지 않는다.
5. 조회한 시각 기준 KST `PREOPEN`/`OBSERVATION_WINDOW`/`AFTER_OBSERVATION_WINDOW`를 분류. 인프라 준비 여부와 실제 09:24 실행 성공 여부를 명확히 분리한다.
6. 날짜+UTC timestamp 단위 JSON 보고서와 SHA-256을 새로 저장. 폴더 0700, 파일 0600, 기존 파일 덮어쓰기 금지. Upbit 연결 실패/타이머 비활성 등은 `INFRA_READINESS_FAILED_NO_AUTOMATIC_ORDERS`로 기록한다.

운영 보관 위치:
```
/home/hong/apps/FreeQuantLab/state/preopen_readiness/
    readiness-YYYYMMDDTHHMMSSffffffZ.json
    readiness-YYYYMMDDTHHMMSSffffffZ.json.sha256
```

자체 준비 검사 결과의 `READY_FOR_FUTURE_OBSERVATION_ONLY`는 **현재 연결·예약이 정상**이라는 뜻일 뿐, 미래 Windows 전원/장동작/실제 주문 체결 보장은 아니다.

## 3. 설치 절차

Git PR CI 통과·병합 후, 고정된 주식 연구 감사 작업트리를 `origin/main`에만 업데이트한다. 운영용 `/home/hong/apps/FreeQuantLab` (청정 90일 기존 경로, 커밋 `d6b531e`)의 브랜치나 DB·프로토콜은 건드리지 않는다.

Windows:
```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\windows\wsl_preopen_warm_task.ps1 -Mode Audit
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\windows\wsl_preopen_warm_task.ps1 -Mode Install
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\windows\wsl_preopen_warm_task.ps1 -Mode Audit
```

WSL:
```bash
install -m 644 deploy/systemd/freequant-clean-preopen.service ~/.config/systemd/user/
install -m 644 deploy/systemd/freequant-clean-preopen.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now freequant-clean-preopen.timer
systemctl --user list-timers --all freequant-clean-preopen.timer
```

실행 기록은 필요 시 사용자 journald로 확인한다. 작업은 읽기 전용이고 모든 관측·가상 거래 타이머는 기존대로 동작한다.

## 4. 안전 및 남은 조건

- Windows가 **완전히 꺼진 상황**, 로그인 전 작업 실행, 절전 중 24h 시장 관측, 정전/네트워크 불능 중 데이터 생성은 자동 해결할 수 없다.
- **WakeToRun=false**이므로 Windows 절전 중 08:40/09:05에 자동 기상한다고 주장할 수 없다. 절전 자체를 피하거나 별도 상시 실행 장치에서 *미래 시간*의 공개 데이터 관측을 운영해야 완전한 수집 커버리지를 목표로 할 수 있다.
- 최초 90일 프로토콜은 재시작·연장·리셋하지 않는다. 누락된 42개 account-day는 0개의 실제 전향 가상 결정으로 유지한다. 미래 새로 생성되는 기록만 독립적으로 감사한다.
- 코드는 사용자/시스템 로그인, Sleep/Power 정책, NAS, 시장 소스 DB, 주문 파이프라인을 변경하지 않는다. 일정 감지·알림 기록만 추가한다.
