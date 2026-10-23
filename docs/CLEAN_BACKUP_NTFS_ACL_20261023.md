# 청정 90일 D: 백업 NTFS 권한 제한 및 복원 검증 — 2026-10-23

## 감사 근거

2026-10-23 첫 가상관측 직후, 로컬 `D:\FreeQuantLab-Backups\clean-90d`의 상위 폴더에서 상속된 `NT AUTHORITY\Authenticated Users` **Modify** 권한이 기존 11개 개체 모두에 적용됐음을 확인했다. WSL에서 표시되는 DrvFS 파일 모드와 실제 Windows NTFS ACL은 별개다. 이 ACL은 청정 원장 SHA 백업을 다른 일반 Windows 인증 사용자가 변경할 위험을 만든다.

현재 권한 보존본: `C:\Users\HONG\AppData\Local\FreeQuantLab-clean-90d-ACL-before-20261023.txt`. 운영 PC HONG/SYSTEM/Administrators에 필요한 접근은 유지하고, 이 경로 아래의 객체에만 broad Authenticated Users/Users/Everyone 권한을 제거했다. `D:\FreeQuantLab-Backups` 루트나 다른 앱 백업의 권한은 변경하지 않았다.

## 실제 실행 결과

- 최초 11개 디렉터리·파일의 광범위한 수정 권한을 제거한 후 ACL 재조회: 위반 0개.
- 09:30 KST에 WSL의 현 청정 6계좌·이벤트 6·호가 2를 SQLite Online Backup API로 다시 복사해 원장 무결성·SHA를 재검증: `CONSISTENT_LOCAL_BACKUP_VERIFIED`.
- 추가 5개 새 파일은 제한된 부모 NTFS ACL(HONG/SYSTEM/Administrators만)을 정상 상속했음. **자식 파일에 상속 플래그가 있어도 broad group 권한이 없으면 정상**으로 검사하는 검증 로직을 적용했다.
- 추가 생성 파일을 포함해 16개 Windows NTFS 객체 읽기 전용 검사: `BACKUP_NTFS_ACL_RESTRICTED`, broad 권한 위반 0.
- 이 첫날 수집 직후 스냅샷에는 아직 공식 첫날 SHA 영수증이 없으며 10:50 독립 감사 전이라 정상이다. 이후 11:30 새 자동 백업에서 공식 영수증을 확인해야 한다.

## 코드로 재현

`ops/windows/clean_backup_acl.ps1`는 **기본 Audit(읽기 전용)**. 명시적 Apply만 기존 보안 상태에 문제가 있을 때 `icacls /save`로 ACL 백업을 새 타임스탬프 파일에 저장하고, 대상 하위 경로만 제한한다. 실패 시 원본 ACL 복원을 시도하며, 오류 후 성공으로 위장하지 않는다.

```powershell
powershell.exe -NoProfile -File .\ops\windows\clean_backup_acl.ps1 -Mode Audit

# 새로운/권한 드리프트가 확인된 PC에서 운영자 승인 후에만
powershell.exe -NoProfile -File .\ops\windows\clean_backup_acl.ps1 -Mode Apply
```

주의: 해당 D: 경로는 Windows PC **동일 장비**의 로컬 복구본이다. 물리적 PC 고장·랜섬웨어·전체 관리자 계정 탈취에 대한 독립 복구 보장은 없다. 외부 호스트 백업, 주기적인 복원 리허설, backup retention/시스템 장애 복구는 [Issue #64](https://github.com/shhong20/FreeQuantLab/issues/64)의 후속 작업으로 계속 남긴다. 청정 90일 사전등록 전략/타이머/가상거래 원장을 수정하지 않는다.
