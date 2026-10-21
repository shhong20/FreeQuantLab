# FreeQuantLab — WebSocket 공개호가·체결 원장 무손실 압축 아카이브 (2026-10-21)

## 결론

WSL 운영 원본 SQLite를 정지하거나 교체하지 않고, **SQLite online backup → SHA256 → gzip → 독립 복원 → 전체 프레임 SHA256/시간적 인과관계/수량 검증**을 실제 수행했다. 아카이브는 일관된 특정 시점의 *완전한 SQLite 복사본*이고, **현재 운영 시각 이후 들어온 관측 프레임은 포함하지 않는다**. 실거래·실제 주문 체결 증거를 새로 만든 것이 아니다.

### 첫 운영 원장 실제 아카이브

- 수집 원장: `/home/hong/apps/FreeQuantLab/state/microstructure/websocket.sqlite3`
- 보관 위치: `/home/hong/apps/FreeQuantLab/state/microstructure/archives/ws-full-20261021T075636Z-f3aaeac0f201`
- 내용: `snapshot.sqlite3.gz` + `manifest.json`; 디렉터리 mode 0700, 파일 mode 0600.
- 스냅샷 시점 2026-10-21 07:56:36 UTC (16:56:36 KST), 운영 첫 **7개 정상 세션**.
- SQLite 원본 스냅샷 **17,235,968 byte (약 17.24 MB)**, 압축 **1,132,513 byte (약 1.13 MB)**, **93.43% 절감** (6.57%로 축소).
- 보존 프레임: 30호가 **4,017개**, 공개 체결 **466개**, 전체 **4,483개**, 과거에 이미 수신한 호가와의 인과적 참조 **207개**.
- raw frame 총합 **11,819,155 byte**. SQLite integrity, foreign_key_check, **각 원본 raw SHA-256**, 페어/거래소타임스탬프/stream_type/trade 고유 ID, 같은 세션 이전 book 링크, 세션별 frame_count/총량, 복원 이미지 byte-level SHA-256 모두 PASS.
- 독립 `/tmp` 경로로 한 번 더 복원하여 모두 같은 결과가 나오는지 재검증 **PASS**.
- 소스 SQLite는 지우지 않음. 기존 REST·WS 자동 관측, baseline/Exaone/Qlib 서비스가 계속 스케줄되는 상태에서 실행. 이 아카이브는 16:56:36 이후 수신 자료를 포함한다고 주장하지 않음.

**93%대 압축은 매우 반복적인 30호가 JSON 원문과 SQLite 페이지의 특성에 따른 이 스냅샷의 실측이다. 1달/1년 이후에도 동일 압축률·성능을 보증하지 않는다.**

## 소스

- `quantlab/websocket_archive.py`: read-only SQLite online backup API로 WAL을 포함해 완성된 일관된 SQLite snapshot 생성. 원본 `.db` 파일을 단순 `cp`하지 않음.
- SQLite 스냅샷에서 현재 `RUNNING` 세션이 보이면 완결된 프레임 수를 신뢰할 수 없으므로 **fail closed**. 짧은 30초 WS 수집 창이 종료된 뒤 다시 시도. 운영 수집기 중단/재시작을 아카이브 전제조건으로 두지 않음.
- SHA256 해시를 검증한 뒤 reproducible gzip (mtime=0, 원본 파일명 미기재) 압축. 대상 고유 폴더를 만들고 임시 staging에서 독립 복원·전체 원본 검증이 끝나야 **원자적 rename**으로 완료본 공개. 디렉터리 fsync 수행. 실패하면 미완료 staging 제거.
- 원본 프레임 100%를 Python 리스트에 올리지 않고 **SQLite iterator + 인덱스 참조 조회**로 순차 검증. 장기 데이터도 O(전체 수집 프레임) 시간은 필요하지만 프레임 리스트 전체를 메모리에 복사할 필요는 없음.
- 복원 시 gzip byte SHA256·압축 크기·원본 uncompressed SHA256/크기, 복원 SQLite 구조·foreign key, 모든 프레임 raw 해시/수신시각/이전호가 인과 참조·고유 trade ID·세션 count를 **전부 비교**. manifest 및 frame tamper, incomplete gzip, symlink gzip, 오염된 원본 원장은 실패.
- 기존 복원 파일 덮어쓰기 거부, live source DB 경로로 직접 복원 거부, 크기 상한 40GiB, 압축 해제 실패 시 미완료 복원 파일 삭제.
- 수집 원본의 지속적인 append, 기존 timer, WAL/SHM, 모델 상태/종목 신호, 실거래 기능에는 직접적인 변경 없음.

## 실행 방법

```bash
cd /home/hong/apps/FreeQuantLab-observer-runtime

# 별도 아카이브: 수집기 중단·DB 변경 없이 생성. 요청할 때만 실행
/home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.websocket_archive \
  --live-db /home/hong/apps/FreeQuantLab/state/microstructure/websocket.sqlite3 \
  --archive-root /home/hong/apps/FreeQuantLab/state/microstructure/archives

# 원본 아카이브 파일이 그대로 안전하고 독립 복원이 되는지 재검증
/home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.websocket_archive \
  --verify /home/hong/apps/FreeQuantLab/state/microstructure/archives/ws-full-20261021T075636Z-f3aaeac0f201

# 실제 복원 DB를 명확히 분리된 빈 경로로 생성 (기존 파일 덮어쓰기 금지)
mkdir -p /tmp/freequant-ws-restore
/home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.websocket_archive \
  --verify /home/hong/apps/FreeQuantLab/state/microstructure/archives/ws-full-20261021T075636Z-f3aaeac0f201 \
  --restore-to /tmp/freequant-ws-restore/recovered.sqlite3
```

출처 증거 DB와 동일한 경로로 복원하는 것은 **금지**되며, 복구 시에도 실제 수집 서비스 원본을 덮어쓰지 않는다.

## 테스트/운영 안전성

- SQLite WAL에 **아직 체크포인트되지 않은 커밋**을 보존하는지 시험. 읽기 트랜잭션이 과거 snapshot을 고정한 상태에서도 별도 writer의 커밋을 포함해 backup 성공.
- 백업 도중 원본에 새 세션이 커밋되면 백업은 이전 순간의 일관된 1개 스냅샷으로 보존되고, 원본은 진화하면서 유지되는지 확인.
- 정상 파일 재압축·원본 해시 일치, append-only 2개 아카이브 경로 분리, 오래된 아카이브 독립 복원, WAL·원본 무변경.
- RUNNING 세션을 완료로 간주하지 않음. gzip 변조/manifest 조작, restored frame raw를 변조한 뒤 겉의 gzip·SQLite 해시만 재계산해도 **프레임 해시 검사에서 차단**. 복원 파일 덮어쓰기/라이브 원본 경로 재지정/경로 외부 symlink/복원 크기 상한 차단.
- 실거래 API·API 키·주문 없음. 모델 자동 승격 및 운영 paper 원장 변경 없음.

## 제한 사항 및 다음 개발

이번 방식은 **전체 SQLite DB 스냅샷을 별도 gzip으로 보관**하며, live SQLite 테이블/인덱스 크기를 줄이지 않는다. 매일 전체 복사를 자동화할 경우 보존 용량은 누적 SQLite 증가분 이상으로 계속 증가할 수 있다. **정기 전체 스냅샷 자동화·데이터 삭제·압축 후 live 원본 정리는 아직 비활성**.

다음 단계는 날짜별 **증분 아카이브 세그먼트**와 immutable 메타데이터/참조 무결성, 연속 저장 구간 인덱스, 구버전 원본·성공적인 복구를 먼저 검증한 뒤 보존 정책을 결정하는 것. 실제 호가·시장 print가 본인 주문의 체결률을 증명하지 않으므로 모델 수익률 검증이나 실거래에는 아직 사용할 수 없다.
