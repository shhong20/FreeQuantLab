# FreeQuantLab | 공개 WebSocket 증분 원본의 검증된 SSH 전송/독립 복구 (2026-10-21)

## 검증 결과: 로컬 실제 SSH/SFTP 통신 PASS, 실제 외부/NAS 백업 NOT CONNECTED

2026년 10월 21일 WSL에서 독립된 전송 프로토콜과 pinning된 OpenSSH test daemon을 구축하여 실제 네트워크 스택의 **127.0.0.1:22461 loopback SSH/SFTP**로 공개시세 압축 세그먼트를 전송하고, 별도 수신 아카이브 트리로부터 SQLite를 전체 재구성했다.

| 항목 | 검증 결과 |
|---|---:|
| 대상 | 테스트 전용 동일 WSL 로컬호스트 127.0.0.1 (다른 물리 서버가 아님) |
| SSH Ed25519 서버 호스트키 pinning | PASS |
| 외부 SSH config/proxy, password, agent forwarding | 기본 차단 |
| 실제 SFTP 디렉터리 전송 | PASS |
| 전송 gzip 증분 세그먼트 | 6개 |
| 수신 체인 전체 SQLite 복원 | PASS |
| 복원 세션 / 원본 프레임 | **13 / 8,629** |
| 프레임 SHA256·이전 수신 호가의 인과적 참조 | PASS |
| 중복 송신 (이미 상대가 동일한 prefix 보유) | **0개** |
| 다른 서버 host key를 사용한 실제 접속 시도 | SSH가 **Host key verification failed**로 거부 |
| 실서버 NAS·외부 SSH host | **설정 안 됨. 실제 외부 전송하지 않음** |

원본 수집기를 멈추거나 SQLite를 수정하지 않았다. 공개 시장 trade print는 고객 주문의 체결 근거가 아니다. 다른 물리 머신의 성공적인 재해복구로 분류하지 않는다.

## 구현

1. `quantlab/websocket_remote_backup.py`
   - 송신 측 `--prepare`: 로컬 source archive를 **전체 SQLite로 복원한 뒤** 원본 프레임 SHA·호가 참조를 검증. 목적지의 기존 최종 세그먼트 번호/manifest SHA를 참고하여 **아직 없는 세그먼트만** 포함한 압축 묶음을 별도 `transfer.json`과 함께 원자 생성한다.
   - 수신 측 `--status`: 현재 받은 segment count 및 압축 원본 SHA로 확인한 manifest head 정보만 전달 (읽기 전용, 전체 복원은 하지 않았다는 상태 포함).
   - 수신 측 `--receive`: 받은 폴더 내부의 파일·manifest/gzip SHA·이전 세그먼트 해시·세그먼트 순서를 검증. 기존 보관 파일은 **절대 덮어쓰지 않고**, 새 세그먼트만 staging 후 원자적으로 발행. 이후 **반드시 수신 체인을 독립 SQLite로 전체 재구성**, frame SHA, temporal FK, 세션 count를 전수 감사해 `RECEIVER_INDEPENDENT_SQLITE_RESTORED_AND_VERIFIED`를 반환. 실패 시 이번 호출에서 새로 쓴 세그먼트만 rollback하며 기존 수신 보관본은 수정하지 않는다.
   - `scp`로 임시 inbox에 자료가 전달되다 실패해도 수신 측 `--receive`가 검증하지 않으면 정상 아카이브로 발행하지 않는다. 손상되거나 출처가 다른 체인, 이미 수신한 세그먼트를 위조·교체하려는 작업은 거부한다.
2. `quantlab/websocket_ssh_transport.py`
   - 사용자 명시 JSON profile에 host,user,port,실제 **신원 확인된 known_hosts** 경로, 허가된 개인 키 경로, remote inbox/backup root, 그 서버에 이미 설치된 receiver 전용 Python executable 경로를 지정한다.
   - `--transfer`가 **명시적으로 존재할 때만** SSH 키 기반 preflight → 원격 manifest head 확인 → 소스 full restore·새 bundle 준비 → strict SSH/SFTP 전송 → 원격 수신기가 전체 SQLite를 복원하여 SHA/세션 수 일치를 보고하는 전체 프로토콜 실행.
   - 기본 명령은 **preflight only**, 네트워크 데이터 전송 없음. 호스트 자동 탐색/SSH key 자동 생성 및 외부 서버 연결 암묵 허용 없음.
   - `ssh -F /dev/null`, `StrictHostKeyChecking=yes`, `UserKnownHostsFile=<explicit>`, `GlobalKnownHostsFile=/dev/null`, `BatchMode=yes`, `PasswordAuthentication=no`, `ForwardAgent=no`, `ClearAllForwardings=yes`, `IdentitiesOnly=yes`로 ambient credential/proxy/ssh-config 혼입을 방지. `scp` 기본 SFTP 모드만 사용.
   - 송신 후 SSH 응답의 segment 수뿐 아니라 **수신 측 복원 세션 수·프레임 수를 송신 원본 manifest 합계와 비교**하여 거짓 성공 응답을 방지한다. 다만 서버 OS 자체가 손상·악의적일 경우 SSH 응답의 진위까지 독립적으로 보장하는 신뢰앵커는 아니다.
   - 모듈은 SSH 로그인에 성공해도 그 장비가 다른 물리 장치라는 걸 원격 암호학적 방법으로 증명하지 않는다. 시스템 스케줄러, API 키, 원본 변경, trade orders 없음.

## 실제 독립 NAS/서버 도입 시 사전 준비

이번 WSL에는 NFS/CIFS/SSHFS 원격 공유와 `~/.ssh/config`의 승인 서버 별칭, 등록된 사용자 SSH 공개키가 없었다. 따라서 회사 NAS/다른 운영 서버에 임의 접속하거나 임의 credential을 생성해 배포하지 않았다.

독립 장비 관리자가 미리 수행할 일:

- 네트워크에서 접근 허가된 **서버의 DNS/IP와 포트**, 원격 전용 사용자 계정, 권한이 제한된 inbox 및 수신 archive 루트 경로를 고정. 상대는 임의 파일 삭제 권한/실거래 주문 권한을 받지 않아야 한다.
- 서버의 SSH **host key fingerprint를 신뢰할 수 있는 별도 채널로 확인**해 로컬 `known_hosts`를 미리 구성한다. `ssh-keyscan` 결과를 네트워크에서 곧바로 신뢰하도록 코드는 구현하지 않았다.
- 사용자 승인 SSH identity를 로컬에 설치(비밀번호/개인키를 Git에 커밋하지 않음). 원격 서버에 Python 앱 `quantlab.websocket_remote_backup`과 필요한 종속성, SQLite를 설치한다.
- 실제 장비의 수신 원장 이외 별도 폴더에 최초 복원 테스트를 수행해 세션/프레임 바이트 일치를 확인한 뒤, 오프사이트 백업 여부를 별도 판정한다.

설정 파일 *필드 이름 예시* (실제 개인정보/호스트·키를 유추해 채우지 말 것):

```json
{
  "host": "backup.example.test",
  "user": "backup",
  "port": 22,
  "identity_file": "/absolute/path/to/approved-key",
  "known_hosts_file": "/absolute/path/to/pinned-known_hosts",
  "remote_inbox_parent": "/srv/quantlab/incoming",
  "remote_archive_parent": "/srv/quantlab/archives",
  "remote_python": "/srv/quantlab/venv/bin/python"
}
```

실제로 승인된 외부 프로필을 준비한 뒤에만:

```bash
cd /home/hong/apps/FreeQuantLab-observer-runtime
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
mkdir -p /home/hong/apps/FreeQuantLab/state/microstructure/transfer-outbox

# 인증/원격 백업 head 확인만 (읽기 전용)
$PY -m quantlab.websocket_ssh_transport --profile /secure/location/approved-ssh-profile.json

# 사용자가 명시적으로 실제 외부 백업을 승인한 뒤에만 전송
$PY -m quantlab.websocket_ssh_transport --profile /secure/location/approved-ssh-profile.json --transfer
```

원격 사용자는 root/sudo가 아니라 전용 비특권 계정 사용을 권장한다. 현재 source segment가 최신 수집을 아직 보관하지 않았다면 이 도구는 source archive에 이미 담긴 시점까지만 전송한다. 새 세션은 별도의 수동 `websocket_incremental_archive` 명령으로 먼저 보관해야 한다.

## 테스트/안전

19개 신규 테스트: 누락 세그먼트/압축 파일 변조/이상한 파일·symlink 차단, 상대 백업 prefix·출처 불일치, 재연결 후 새 세그먼트만 수신, 원본 경로와 수신 inbox 격리, 실제 host-key pinning을 통한 SSH 접근, 틀린 host-key 실제 접속 거부, 받은 SQLite raw/metadata/인과성 전수 검사, 조작된 원격 성공 프레임 count 거부, 비밀번호/agent forwarding/ambient SSH config 비활성 규칙 포함.

실제 물리 NAS/오프사이트 백업은 **아직 미실시**. 별도 장비를 승인·설정하면 원격에서 복원·검증 후에야 재해복구 조건을 평가한다. 원본 삭제·자동 백업 타이머·전략 자동 승격·실거래 주문은 그대로 비활성.
