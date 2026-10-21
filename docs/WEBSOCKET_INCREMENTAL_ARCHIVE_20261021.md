# FreeQuantLab — 날짜별 증분 WebSocket 공개시세 아카이브 (2026-10-21)

## 검증 요약

이번 개발은 전체 SQLite 스냅샷을 계속 복제하지 않고, 이미 아카이브된 세션을 제외한 **새롭게 완료된 세션만 증분 gzip 압축**해 append-only로 보관한다. 개별 증분 세그먼트는 한 **UTC 날짜**에 속하는 세션만 포함하고, 1회 최대 100세션으로 크기를 제한한다. 세그먼트를 순서대로 결합해 원래의 SQLite `sessions` 및 `frames`을 원래 `seq`·`raw BLOB`·원본 raw SHA·`prev_book_seq` 그대로 복원한다.

**읽기 전용 운영 원장, 실제 거래 없음, 신규 타이머 없음, 자동 아카이브/삭제 없음.** 복원 결과도 live DB에 덮어쓸 수 없다.

### 실제 업비트 운영 데이터 전수 비교

2026-10-21 오후 실제 `/home/hong/apps/FreeQuantLab/state/microstructure/websocket.sqlite3`에 대해 날짜를 명시적으로 지정한 수동 실험을 실시했다.

| 항목 | 실측 |
|---|---:|
| 증분 세그먼트 | **3개** (`seg-000001`, `seg-000002`, `seg-000003`) |
| 총 보관 완료세션 | **9개** (4+4+1) |
| 압축 보관 프레임 | **5,809개** |
| 호가 / 공개 시장 체결 프레임 | 5,206 / 603개 |
| 동일 세션 과거 호가에 인과적으로 연결된 시장체결 | 267개 |
| 첫 증분 압축 | 649,085 bytes |
| 두 번째 증분 압축 | 760,568 bytes |
| 세 번째 증분 압축 | 162,593 bytes |
| 합계 압축 | **1,572,246 bytes (1.57 MB)** |
| 세그먼트 JSONL 압축 전 합계 | 21,774,286 bytes |
| 복원 검증 | 원본 대비 세션 모든 메타데이터 및 **5,809개 프레임 전체 행/원본 바이트 완전 동일 PASS** |

세그먼트는 약 **92.78%**를 줄였지만, 이는 SQLite 페이지 단위 원본과 포맷이 다른 JSONL 보관 파일의 단편적 비교다. 기존 전체 DB gzip과도 동일한 분모가 아니므로 실제 live SQLite가 해당 비율만큼 줄었다는 뜻이 아니다.

## 동작 안전장치

- 한 **SQLite WAL MVCC 읽기 트랜잭션**에서 완료된 세션을 선택하고 해당 세션 프레임만 읽는다. 운영 수집기가 동시에 새 데이터를 저장해도 과거 스냅샷에 미래 데이터를 섞지 않는다. 미완결 `RUNNING` 세션이 있으면 그 세션부터 뒤는 보관하지 않는다.
- 기본 `--through-date`는 **어제 UTC**이므로 아직 진행 중인 오늘 날짜를 잘못 닫지 않는다. 이번 실제 초기화에서는 명시적으로 `--through-date 2026-10-21`을 지정했으며, 이 경우에도 완료된 세션만 포함했다. 하루 날짜의 세션이 여러 세그먼트에 나뉠 수 있다.
- 각 세그먼트 `manifest.json`에는 날짜, 처음/마지막 세션 키, 누적 세션 수, 전체 바이트 및 SHA-256, 이전 세그먼트의 매니페스트 SHA-256을 봉인한다. 파일 전체와 각 공개시세 프레임 자체의 SHA-256을 모두 검증하고, 인과적 호가 참조·시각 역전·중복 trade ID도 검사한다.
- `seg-000001`부터 하나라도 누락되거나 순서가 바뀌면 거부한다. 이전 매니페스트가 변조되면 뒤쪽 체인과 맞지 않아 거부된다. 별도 키/원격 앵커로 서명한 구조는 아직 아니므로 **공격자가 모든 파일과 해시를 동시에 교체할 수 있다면 위·변조 방지의 최종 증거는 아니다**.
- 아카이브를 만들 때 기존 체인을 먼저 전수 독립 복원해 검증하고, 새 세그먼트까지 임시 SQLite로 실제 재구성한 뒤에만 `seg-00000N`을 원자적으로 공개한다. 날짜·세션 순서, 원본 watermark 변경, gzip 원본 변조, 불완전한 구간, 이전 호가 외래키 훼손을 방지한다.
- `.archive.lock` 배타 잠금으로 증분 작업 중복 실행 차단. 실패 시 임시 staging 삭제. 복원 파일을 덮어쓰거나 **실운영 WebSocket DB에 직접 복원하는 것은 거부**한다.
- 운영 SQLite에는 어떤 `DELETE`, `VACUUM`, WAL 체크포인트, 서비스 일시정지도 실행하지 않는다. 원본 및 기존 증분 파일은 그대로 보존하며 아직 **저장 공간을 자동 회수하지 않는다**.

## 실행

```bash
cd /home/hong/apps/FreeQuantLab-observer-runtime
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python

# 기본: 어제 UTC까지의 완료된 세션만, 기존 증분 다음부터 최대 20개 추가
$PY -m quantlab.websocket_incremental_archive

# 이미 생성된 증분 체인 전체 무손실 복원 감사
$PY -m quantlab.websocket_incremental_archive --verify

# 실제 독립 복원 SQLite 만들기: 반드시 사전에 존재하는 빈 디렉터리의 새 파일 이름
mkdir -p /tmp/fql-incremental-restore
$PY -m quantlab.websocket_incremental_archive \
  --verify --restore-to /tmp/fql-incremental-restore/recovered.sqlite3

# 오늘 진행 중인 데이터 일부를 수동으로 보존하는 연구 실험에만 날짜 명시
$PY -m quantlab.websocket_incremental_archive \
  --through-date 2026-10-21 --max-sessions 4
```

기본 아카이브 경로: `/home/hong/apps/FreeQuantLab/state/microstructure/incremental/`.

## 검증/운영 구분

- 합성 테스트: 동일 날짜 분할/새 세션 추가 후 재개, 날짜 경계 분리, 24시간 경계, 생방송 쓰기 중 WAL snapshot 정합성, hash chain 변조·세그먼트 누락·원본 watermark 훼손, 중단된 세션 보류/완료 후 재개, 복원 경로 안전, live source 불변성.
- **실제 운영 데이터:** 현재 원본 9세션과 모든 메타데이터 및 원문 프레임 5,809행을 복원본과 바이트 단위 비교해 완전히 일치.
- 실제 24시간 관측 성공률은 별도의 REST/WebSocket watchdog이 실제 경과 시간에 따라 판정한다. 세그먼트 다수 생성은 24시간 관측 증명이 아니다.
- **아카이브 자동화, 보관 주기, 증분 파일 삭제/이전/동기화, 실거래/전략 자동 승격은 비활성**.

## 추가 개선 과제

현재 신규 증분 발행 전 이전 전체 체인을 재구성해 검사하기 때문에 세그먼트가 수천 개가 되면 검증 시간이 계속 증가한다. 장기 운용 전에 독립된 immutable checkpoint 인덱스, 버전별 proof, 전체 검증 및 빠른 점검을 분리하고 장기 스토리지 비용을 측정해야 한다. 아카이브는 단일 서버의 동일 디스크에 보관 중으로, 장비/디스크 고장에는 취약하다. 외부 백업 등 별도 보존 정책과 수동 복구 모의훈련이 뒤따라야 한다.
