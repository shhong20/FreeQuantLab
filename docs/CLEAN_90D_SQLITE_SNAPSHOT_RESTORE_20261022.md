# FreeQuantLab 청정 90일 SQLite 원장·첫날 영수증 정합 백업 (2026-10-22)

**범위:** 실험 원장·SHA 파일의 동일 PC 로컬 D 드라이브 복제와 실제 임시 파일 재읽기 검증. 사용자의 실거래와 클라우드/타 PC 백업을 생성하지 않는다.

## 절차

`python -m quantlab.clean_backup`은 원본 `clean_shadow_90d.db`를 SQLite `mode=ro`로 열고 온라인 `Connection.backup` API로 동일 시점 일관성 있는 스냅샷을 생성한다. 원본 DB를 직접 파일복사하면 WAL에 기록된 최신 이벤트가 누락될 수 있으므로 사용하지 않는다.

기본 경로는 `/mnt/d/FreeQuantLab-Backups/clean-90d/snapshot-YYYYMMDDTHHMMSSZ`. 매 시각마다 새 디렉터리를 생성하므로 기존 복사본을 덮어쓰지 않는다. `manifest.json`은 복사본 SQLite의 SHA-256, 청정 코호트 공식 protocol SHA-256, 계좌/이벤트/호가 행 수, 첫날 영수증 SHA-256(있을 때만)을 고정한다. 독립 `verify_backup`은 복사본의 해시/무결성 검사/가상 원장 회계 대사와 첫날 영수증의 6이벤트·2호가 본문 해시 일치를 확인한다.

첫 유효 감사 이전에는 `first_day_receipt_attached=false`도 정상이다. **첫날 감사가 이미 완료돼야 하는 시점인데 영수증이 빠졌다면**, SQLite 복사본을 포렌식 증거로 남기되 CLI 종료코드 2 및 `LOCAL_BACKUP_MISSING_DUE_FIRST_DAY_RECEIPT`로 실패 처리한다. 관측 내역을 만들어 채우거나 영수증을 새로 승인하지 않는다.

## 자동 실행

추가 사용자 서비스 `freequant-clean-backup.service/timer`는 **매일 11:30 KST**, 기존 청정 감사 **10:50 KST 이후** 백업을 실행한다. 실제 systemd 활성화는 GitHub CI와 수동 백업/복원 확인 후에만 한다. 기존 청정 shadow/watchdog timer는 어떤 변경도 필요하지 않다.

## 한계 및 후속 개선

- 원본과 D 드라이브 복사본은 **같은 PC에 존재한다**. 원격 저장소·별도 물리 호스트·외부 전자서명 및 제3자 신뢰 타임스탬프 증명이 아니다.
- WAL 일관성은 SQLite 온라인 백업 API와 저장본 SQLite 무결성/회계 감사로 검증한다. 물리적 전원 손실, D 볼륨 장애, 랜섬웨어, 백업 운영권한 탈취를 모두 견딘다는 뜻이 아니다.
- 복사본은 첫날 영수증의 원본 SHA를 보존할 뿐, 실제 공개 ticker의 실행가능 bid/ask나 투자 수익성을 인증하지 않는다.
- 자동 삭제·청소·보관기간 제한은 아직 구현하지 않는다. 자유 디스크 공간 추적과 **다른 장애 영역으로 별도 복제 후 실제 복원 리허설**을 후속 P1 작업으로 유지한다.
- 소스의 실주문·모델 자동 승격·6계좌·90일 최초 결정일·수집 타이머는 변경하지 않는다.

```bash
cd /home/hong/apps/FreeQuantLab-clean-runtime
/home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.clean_backup

# 새 유닛은 CI, 첫 수동 검증 성공 후에만 설치·활성화:
systemctl --user daemon-reload
systemctl --user enable --now freequant-clean-backup.timer
```
