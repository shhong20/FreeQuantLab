# FreeQuantLab — WebSocket 공개시장 증분 아카이브 체크포인트와 복제 지연(RPO) 분석 (2026-10-21)

## 범위·현재 한계

- WSL의 Linux 로컬 증분 아카이브와 Windows D: 별도 볼륨의 증분 미러를 대상으로 한다.
- 해당 WSL에서 **NFS/CIFS/SSHFS 원격 저장소 마운트는 확인되지 않았고**, SSH 공개 Host 별칭도 없었다. SSH·rsync 프로그램은 설치돼 있다. NAS 또는 물리적으로 독립된 서버에 보내는 백업은 **진행하지 않았으며 성공으로 표시하지 않는다**.
- Windows D:는 다른 파일시스템 마운트이지만 **동일 PC**의 볼륨이다. BitLocker, NTFS ACL, 물리적 디스크의 분리 및 외부 백업 성공은 증명되지 않았다.
- 모든 관측·매매 시스템과 실시간 REST/WS 타이머를 유지한다. 고객 주문 API, 계좌 비밀, 실거래, 원본 자동 삭제와 자동 아카이브·복제를 사용하지 않는다.

## 실제 2026-10-21 KST 17:49경 검증 결과

**실데이터 라이브 원본 12개 세션**, 증분 WSL 아카이브 5개 세그먼트 및 D 미러 5개 세그먼트에 전체 **7,737개 공개 WebSocket 프레임**이 기록됐다. 호가 7,011개·공개 시장 체결 프린트 726개이고, 인과적으로 이전에 수신된 호가 참조는 325개다. gzip 합계 2,077,204바이트.

실서버에서 체크포인트가 이전 4세그먼트 상태인 채 새 `seg-000005`가 생성되었을 때, `--max-unverified 0`이 **exit 2**로 종료됨을 검증했다. WSL 원본 전체 SQLite를 다시 재구성해 5세그먼트 체크포인트를 생성했고, 그 뒤 `--use-verified-source-checkpoint` 옵션으로 원본 쪽 **중복 전체 복원만 생략**, D 볼륨에서는 세그먼트를 새로 복사해 **전체 SQLite를 다시 복원해 SHA·세션 FK·호가–체결 시간 인과관계를 전수 검증**했다.

D 볼륨에서도 독립 전체 복원 체크포인트를 생성했다. 같은 시각 `websocket_backup_gap`은 완료 12세션 / WSL 증분 보관 12세션 / D 미러 12세션, **미보관 0 / 미복제 0**으로 관측했다. 이후 새 세션이 생기면 값이 증가한다.

## 기능

### 1. 회복 체크포인트: quantlab/websocket_archive_checkpoint.py

- `--create`: 압축/매니페스트 체인과 원본 JSON 프레임 전체 SHA 및 종목별 호가–체결 참조를 **독립 SQLite로 모두 재구성한 다음**, 마지막 세그먼트 번호·원본 경로·마지막 manifest SHA·전체 prefix fingerprint·총 세션·프레임 수를 담은 불변 JSON 체크포인트를 원자적으로 발행한다. 기존 `verified-through-00000N.json`을 덮어쓰지 않는다.
- (기본 read-only audit): 압축된 모든 세그먼트의 바이트 길이와 SHA, 매니페스트 해시 체인 및 기존 체크포인트 prefix fingerprint를 빠르게 점검한다. **이 호출 자체는 SQLite 전체 복원이나 프레임 내부 인과관계 재검증이 아니다.** 출력에 `full_recovery_performed_in_this_audit=false`를 명시한다.
- `--max-unverified 0`: 가장 최근 전체 복원 체크포인트 이후 추가된 세그먼트가 **단 1개라도 있으면 exit 2**. 누락·매니페스트 변조·해시 불일치 시 예외. 빠른 점검에 "전체 원본 복원 완료"라는 잘못된 인장을 붙이지 않는다.
- 많은 체크포인트를 가진 장기 체인에서 prefix digest를 한 번만 순차 계산하도록 설계해 기본 중복 검증 계산을 **O(세그먼트 수 + 체크포인트 수)**로 유지한다. 압축 SHA 계산에 필요한 I/O 자체는 모든 보관 파일 용량에 비례한다.
- **같은 PC에 존재하는 서명되지 않은 검사 기록**이므로 공격자가 gzip, manifest, 모든 checkpoint를 동시에 교체할 수 있는 상황에는 진본성 보증이 아니다. 별도 독립 원격 키/원격 오브젝트 버전 고정 앵커가 필요하다.

### 2. 복제 최적화: quantlab/websocket_archive_mirror.py

- 기존 기본값: 소스 전체 복원 + 목적지 전체 복원.
- `--use-verified-source-checkpoint` **명시적 선택**: 기존 소스의 전체 복원 체크포인트가 지금 모든 세그먼트를 포함하고 있고, gzip/manifest/prefix SHA가 변하지 않은 경우에만 **소스 중복 복원을 건너뛴다**.
- **D 목적지 전체 SQLite 복원은 모든 경우 필수**이다. 복원한 원본 JSON SHA와 과거 호가 참조를 실제 검증한다. 원본에 새로운 세그먼트가 생겨 체크포인트가 오래됐으면 이 옵션은 거부되므로, 다시 `--create`로 전수 복원을 실시하거나 기존 기본값의 전체 복제 경로를 사용한다.
- 최적화는 고객 주문의 실제 체결 증거를 만들지 않는다. 프레임 이외의 경제적 인과성, 실거래 슬리피지·미체결률은 아직 관측하지 않았다.

### 3. 백업 공백 감시: quantlab/websocket_backup_gap.py

- 실운영 SQLite의 **완료된 세션 수**를 WSL 증분과 D 복제의 누적 보관 세션 수와 비교한다. 마지막 보관 세션의 식별자·시작 시각과 기록된 소스 watermark가 일치하는지 확인한다.
- WSL 미보관 세션, D 미복제 완료 세션, 새로 복제해야 할 세그먼트 수, **복원 시점 경과 시간/RPO 추정**을 JSON으로 출력한다. `--max-unmirrored 0`을 주면 누적 차이가 하나라도 있을 때 exit 2.
- gzip·manifest 원본 바이트와 sha256 체인 점검을 수행하지만, 과거 live sessions 전체 행과 개별 프레임을 매번 다시 비교한 결과가 **아니다**. 빠른 gap 검사는 이 차이를 `archived_prefix_session_rows_individually_revalidated=false`로 명시한다.
- SQL `COUNT`·watermark 2건·최신 1건만 조회해, 장기간 수백만 세션을 Python 리스트로 읽지 않는다. source SQLite에 변경하지 않는다.

## 운영 수동 명령

```bash
cd /home/hong/apps/FreeQuantLab-observer-runtime
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python

# 현재 WSL 체인에 전체 SQLite 복원 체크포인트 생성 (가벼운 작업 아님)
$PY -m quantlab.websocket_archive_checkpoint --create

# 빠른 증분·압축 해시 감사: 새로 추가된 미복원 세그먼트는 허용하지 않음
$PY -m quantlab.websocket_archive_checkpoint --max-unverified 0

# D 백업이 정상인 경우 소스 중복 full-restore만 생략; 목적지는 반드시 full-restore
$PY -m quantlab.websocket_archive_mirror --use-verified-source-checkpoint

# D 별도 볼륨 전체 복원 체크포인트
$PY -m quantlab.websocket_archive_checkpoint \
  --root /mnt/d/FreeQuantLab-Backups/websocket-incremental --create

# 실제 수집 대비 보호되지 않은 신규 세션을 확인
$PY -m quantlab.websocket_backup_gap

# 엄격한 수동 공백 판정. 새로운 미복제 1세션만 있어도 exit 2
$PY -m quantlab.websocket_backup_gap --max-unmirrored 0
```

## NAS/원격 복원 남은 단계

이 서버에서는 NFS/CIFS/SSHFS 원격 마운트와 명시적 백업 서버 SSH 별칭이 확인되지 않았다. 임의 회사 NAS·다른 업무 서버의 파일에 접근하거나 임의 원격지로 공개 자료라도 복제하지 않았다.

독립 백업 경로가 명시적으로 준비되면: 전용 읽기/쓰기 최소 권한 계정·고정 호스트키·도착 경로를 확인 → **증분 파일만 추가 전송** → 대상 NAS/서버에서 원본 WSL과 무관하게 `--verify` 및 SQLite 프레임 SHA·외래키 복원 검증 → 새 장비에서의 복구 연습 → 백업 주기/RPO를 결정한다. 현재는 원본·D 세그먼트 삭제·자동 백업·운영 거래 변경이 모두 비활성이다.
