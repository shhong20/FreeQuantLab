# FreeQuantLab 세 런타임 안전 배포 지문 검증 (Issue #65, 2026-10-22)

## 목표와 운영 범위

실행 중인 역할별 세 소스 트리(Core/Exaone-Qlib/대시보드, Observer/L2/WebSocket/주식, Clean/90일 청정)는 **같은 Git 커밋일 필요가 없다**. 잘못된 것은 임의의 서비스가 승인하지 않은 작업 디렉터리·Git 커밋·systemd 커맨드·프로토콜/설정·SQLite 스키마에서 실행되는 것이다.

새 `quantlab.deploy_contract` 명령은 운영 중인 **18개 systemd 서비스**(청정 백업 포함)를 조회해 다음 배포 증거를 읽기 전용으로 기록한다.

- 3개 worktree의 Git HEAD 40자리 커밋과 `git status --porcelain` 청정 상태
- 서비스별 `WorkingDirectory`, 디스크의 `FragmentPath` 내용 해시, 해당 worktree의 `deploy/systemd` 원본 unit SHA
- 실행 중인 systemd `ExecStart`의 **PID·시각을 제외한 실제 커맨드 지문**
- 청정 동결 6계좌 프로토콜과 핵심 사전등록 파일(Exaone/Qlib 정책·프로토콜), 관련 crypto 설정의 파일 SHA
- 실제 core 및 청정 SQLite의 **DDL/스키마 SHA**(실시간 장부 데이터, 계좌 잔액 및 사용자 인증정보는 기록하지 않음)

모델 바이너리 전체의 해시, 각 테스트 성공 이력, 시장 데이터 신뢰도, 실제 체결과 알파 검증은 본 명령이 인증하지 않는다. 출력에 `models_full_binary_sha256_verified=false`를 유지한다.

## 사용법

운영 완료 직후, 배포 담당자가 검증된 상태를 한 번만 기록한다.

```bash
cd /home/hong/apps/FreeQuantLab-clean-runtime
/home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.deploy_contract \
  --capture /home/hong/apps/FreeQuantLab/reports/deployment/approved-20261022.json
```

현재 상태의 변조·버전 드리프트 검사:

```bash
/home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.deploy_contract \
  --verify /home/hong/apps/FreeQuantLab/reports/deployment/approved-20261022.json
```

`--verify`는 실제 서비스 오류(exit=2 등)를 별도 운영 상태로만 출력하고, 배포 변경과 구분한다. worktree가 dirty이거나 서비스 WorkDir/ExecStart/unit파일/프로토콜/DDL SHA가 기록과 달라지면 종료코드 2를 반환하며 **매매 중단, 자동 롤백, 원장 변경 등 어떠한 조치도 수행하지 않는다.**

`--capture`는 이미 존재하는 기록을 덮어쓰지 않고 `O_EXCL`로 파일을 생성한다. 파일 지문은 우발적인 변조를 감지하는 SHA-256이며 독립 신뢰 서명·원격 WORM·외부 공증이 아니다. 동일한 저장소에 접근할 수 있는 공격자가 파일과 지문을 모두 바꿀 수 있으므로 별도 접근권한·독립 백업이 필요하다.

## 운영 원칙

1. PR 전체 CI 성공 + 코드/DB 스키마/프로토콜 변경 범위 검토
2. 각 역할별 배포 후보 worktree 및 실행 명령을 실제 운영과 비교
3. **기존 활성 서비스/청정 원장은 그대로 유지한 상태로** 드리프트 검사 수행
4. 명시적으로 검증한 서비스만 별도 배포·재검증, 필요시 담당자가 이전 고정 SHA로 수동 롤백
5. 신규 baseline은 배포 결과가 검증된 후에만 생성, 기존 baseline을 덮어쓰지 않음
6. 독립 L2/WS coverage FAIL은 이 검증기가 지우지 않음. 실주문·모델 승격은 계속 OFF

## 남은 P1 작업

현재 구현은 **배포 상태 고정과 드리프트 탐지**이며, 시스템 전체의 무중단 원자적 전환/롤백 실행 엔진이 아니다. 기능별 단계적 롤아웃·복원 실패 처리·모델 바이너리 해시·멀티호스트 증거 보관은 [Issue #65](https://github.com/shhong20/FreeQuantLab/issues/65)에서 후속 개발해야 한다. 이미 시작한 90일 평가 규칙은 소급 변경하지 않는다.
