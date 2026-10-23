# Windows 절전 복귀 시 WSL 기동 작업 (2026-10-23)

## 실운영 진단

WSL은 2026-10-22 22:58~2026-10-23 08:31 KST에 종료됐고, L2·WebSocket의 10분 단위 공개 시장 관측이 57슬롯 연속 누락됐습니다. Windows는 재부팅한 것이 아니라 절전 상태로 들어갔습니다.

Windows System 로그의 Kernel-Power **Event 187**는 `winlogon.exe`의 `SetSuspendState`/ `SetSystemPowerState` 호출을, Event 42는 **Application API** 절전 사유를 기록했습니다. Windows AC 유휴 절전 설정은 이미 0분(사용 안 함)이므로 단순한 idle sleep timeout 재설정으로 해결할 수 없습니다. 최초 절전 요청이 사용자 조작인지 외부 프로그램인지는 확정되지 않았습니다.

## 적용된 Windows Task Scheduler 구성

- 이름: `FreeQuantLab-WSL-Resume-Recovery`
- Trigger: System / `Microsoft-Windows-Power-Troubleshooter` Event ID 1 (절전 복귀)
- Delay: 20초
- Action: `C:\Windows\System32\wsl.exe --exec /usr/bin/true`
- Principal: 기존 사용자 대화형 토큰, Limited; IgnoreNew/1분 종료 제한
- 기존 `Remote-MCP-WSL-Autostart` 로그인 트리거는 그대로 유지
- Windows Task Scheduler 수동 시작 결과 2026-10-23 09:11 KST 종료코드 0, 신규 작업 Audit 결과 `RESUME_TASK_CONFIG_MATCH`.

이 작업은 절전으로 정지된 WSL을 Windows 복귀 후 다시 시작하도록 요청하는 것일 뿐, **절전 중 데이터 수집 유지·과거 거래/호가 증거 소급 생성·로그인 전 실행 보증·운영 CI PASS를 의미하지 않습니다.** 실제 Windows 절전 후 *자동* 이벤트 재개는 아직 실험하지 않았습니다.

## 코드로 재현 및 안전 감사

`ops/windows/wsl_resume_task.ps1`는 기본적으로 `-Mode Audit`(읽기 전용)이며, 명시적 `-Mode Install`만 없던 작업을 추가합니다. 기존 같은 이름 작업이 있으면 덮어쓰지 않습니다. 비정상 action/event/권한이면 `RESUME_TASK_CONFIG_DRIFT`, 종료코드 2로 처리합니다.

Windows PowerShell에서:

```powershell
powershell.exe -NoProfile -File .\ops\windows\wsl_resume_task.ps1 -Mode Audit
# 신규 PC에서 새 작업을 명시적으로 등록할 때만:
powershell.exe -NoProfile -File .\ops\windows\wsl_resume_task.ps1 -Mode Install
```

현재 작업은 직접 운영 PC에 등록했고 GitHub 코드만 뒤이어 추적합니다. GitHub PR 병합은 **기존 청정 90일 런타임을 자동 변경하지 않습니다.** 09:24 첫 유효 가상관측, 10:50 독립 감사, 11:30 백업 타이머 역시 유지합니다.

## 남은 근본 과제

컴퓨터를 사용자가 의도적으로 절전시키거나 프로그램이 절전을 요청하면 Windows에서 WSL 기반 실시간 시장 데이터 수집은 중단됩니다. 연속 24시간 공개 주문장 관측이 필요한 경우 Windows 전원을 계속 켜두거나 별도 상시가동 호스트로 L2/WS 관측만 분리해야 합니다. 실제 sleep/resume 자동 작업 실행 검증과 24h 독립 품질 게이트 복귀까지 [Issue #63](https://github.com/shhong20/FreeQuantLab/issues/63)은 미해결입니다.
