# FreeQuantLab 승인 SHA 기반 롤백 안전성 사전검증 (Issue #65)

## 목적

`main` GitHub CI 성공은 이미 실행 중인 시스템의 안전한 원복을 보증하지 않습니다. Core/Observer/Clean의 런타임이 분리돼 있고 90일 청정 평가가 사전등록됐기 때문에, 과거 커밋으로 `git switch`하기 전에 변경 범위와 현재 승인 기준의 일치를 먼저 입증해야 합니다.

추가 CLI `python -m quantlab.rollback_preflight`는 **읽기 전용 드라이런**입니다. 실제 롤백, 코드 전환, 원장 수정, systemd 재시작, 실주문, 모델 자동 승격은 구현하지 않습니다.

## 실제 기존 배포의 사전검증

```bash
cd /home/hong/apps/FreeQuantLab-clean-runtime
/home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.rollback_preflight \
  --lane clean \
  --current-baseline /home/hong/apps/FreeQuantLab/reports/deployment/approved-20261022T132036Z-fd1c9f9.json \
  --target-baseline /home/hong/apps/FreeQuantLab/reports/deployment/approved-20261022T130756Z-b236250.json
```

현재 승인 3런타임·18서비스 스냅샷과 실제 운영 환경을 **먼저 대조**한 후에만, 과거 승인 baseline을 비교합니다. 출력은 Git 변경 파일 목록, 후보 commit 존재·실제 ancestor 관계, 고정 원장 스키마·청정 프로토콜 지문, service unit/ExecStart 차이, 보호 대상 실행 코드 변경, 모델 증거 수집 범위 및 수동 검토 필요 여부를 명시합니다.

과거 baseline은 Qlib 모델 파일 전수 지문 검증 이전에 만든 것이므로 현재와 증거 범위가 다릅니다. **실제 2026-10-22 드라이런은 `MANUAL_REVIEW_REQUIRED`·종료코드 2**를 반환했으며, old `b236250`으로 복귀하지 않았습니다.

### 차단 기준

- 현재 런타임이 승인 baseline에서 변경됐거나 dirty면 차단
- Git 커밋 객체가 없거나 현재 SHA의 조상(commit ancestor)이 아니면 차단
- 고정 프로토콜/설정 또는 SQLite 스키마 변경, 실행 unit/명령 변경 시 차단
- Clean 실행 코드 전반(단순 읽기 전용 관측 모듈 제외)에 변경이 포함되면 차단
- 기존 Qlib 모델 지문 검증 범위가 축소되면 경고 및 수동 검토 유지
- baseline SHA 지문 불일치·파일 누락 등 근거 부족 시 fail closed
- 문서/테스트만 변경됐더라도 롤백 자체는 자동 승인하지 않음

## 중요한 한계

- baseline SHA는 우발적 내용 변경 방지용으로, 저장 파일의 독립 서명·타 시스템 백업/제3자 증명은 아닙니다.
- `git diff` 기반 위험 범주 분류는 사업 규칙의 완전한 동치성을 증명하지 못합니다. 모두 기록하고 수동 리뷰합니다.
- 여러 서비스의 실제 롤백을 하나의 원자적 트랜잭션으로 수행하는 것은 아직 구현하지 않았습니다. 각 lane의 격리/서비스 타이머·데이터베이스 트랜잭션/프로토콜 영향을 검증한 뒤 별도 개발합니다.
- 2026-10-23 첫 실관측 전에는 Clean 코호트의 스키마·전략·실행 타이머와 원장을 변경하지 않습니다.
- L2/WS 감사의 시간 커버리지 실패는 Git 배포 드리프트와 별개의 실제 품질 문제이며, 롤백으로 해결되었다고 표시하지 않습니다.
