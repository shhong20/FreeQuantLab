# Upbit 공용 WebSocket 호가·체결 동시 관측 (2026-10-21)

## 판단

**공개 실시간 WS 구독·원본 저장·시간 정렬 검증: PASS. 독립적 체결 정확성/실행가능성 검증: NOT ESTABLISHED.** 기존 REST 10분 관측 및 Qlib/Exaone/baseline 가상 운용은 변경하지 않는다.

공식 사양:
- [호가 웹소켓](https://docs.upbit.com/kr/reference/websocket-orderbook), [체결 웹소켓](https://docs.upbit.com/kr/v1.6.4/reference/websocket-trade), [WebSocket 가이드](https://docs.upbit.com/kr/v1.6.4/reference/websocket-guide)
- Public: `wss://api.upbit.com/websocket/v1`. 인증 토큰, 고객 계좌, `myOrder`/private subscription 및 거래소 주문 호출 없음.
- 메시지: 한 연결에서 BTC/ETH/XRP/ADA 네 쌍의 `orderbook` 30호가 + `trade` 실시간 구독. JSON DEFAULT; 거래 체결 `sequential_id`는 고유 식별자이나 **정렬 시퀀스가 아님**.

## 수집·무결성

`quantlab/websocket_microstructure.py`:
1. 시작·종료가 정해진 한 수집 세션(5~60초, 운영 설정 **30초**) 동안 공개 websocket을 구독한다. WS 실제 연결 실패/재연결/오류는 세션에 보존하고, 3회 이상 재연결 실패 시 실패를 반환한다. 서버 재부팅, 이웃 세션 및 비관측 시간의 과거 L2/tape는 절대 합성 복원하지 않는다.
2. REST 관측 원장 `state/microstructure/observations.sqlite3`와는 완전 별도인 `state/microstructure/websocket.sqlite3` SQLite WAL+FULL sync 원장에 수신한 원본 공개 메시지 바이트, SHA256, **클라이언트 수신 시각·거래소 시각·수신 순번**, 스트림 종류, 체결의 고유 ID, 직전 호가 참조를 저장. SQLite transaction commit에 실패한 이벤트는 정상 저장으로 간주하지 않는다.
3. 공개 `SNAPSHOT` 및 `REALTIME` 호가를 수신하나 체결은 `REALTIME`만 채택. 호가 30단계·0 aggregation·가격 정렬·bid/ask 비역전·음수/0 잔량 차단, 거래량·가격 비정상·거래소 시각 너무 오래됨·거래소 에러·비공개 종류를 차단. 동일 페어 `sequential_id` 중복 저장 방지.
4. `trade` 이벤트에 과거 `orderbook`을 연결하려면 **그 호가가 이미 먼저 수신·저장되어 있고**, 호가 거래소 timestamp가 체결보다 미래가 아니며, **거래소 timestamp 차이 <=2초**이어야 한다. 네트워크가 끊겨 새 연결이 시작되면 과거 book cache를 폐기한다. 이 조건을 만족하지 못한 체결은 `NO_CAUSAL_BOOK`으로 그대로 보관; 허위 인과관계·불리한 데이터만 선택해 보정하는 것을 금지.
5. 수신하는 프레임이 너무 많으면 상한 12,000개에서 `TRUNCATED_MAX_FRAMES`, 스냅샷이 낡거나 일부 메시지에 문제 있는 경우 세션 상태를 `DEGRADED`로 남긴다. API 중단, 중복 및 질 나쁜 구간을 성공 100%로 간주하지 않는다.
6. `--audit`은 **최근 48시간**의 원본 SHA·메타데이터, 미래 book 참조 및 거래소 timestamp와 클라이언트 시계 인과관계, 중복 체결 ID, 세션별 원장 프레임 수를 재검증한다. 실제 저장된 메시지는 48시간 뒤에도 DB에 보존한다. `--require-recent-seconds 2400`은 최근 정상 세션이 40분 초과 오래됐으면 exit 2를 반환한다.

## 실제 공개 WS 수신 성공 (첫 독립 관측)

2026-10-21 KST 실서버 WSL에서 약 **12초** 구독한 결과:

| 항목 | 실측 |
|---|---:|
| 원본 WebSocket 프레임 | **298** |
| BTC/ETH/XRP/ADA 30호가 프레임 | 264 |
| 실시간 공개 체결 프레임 | 34 |
| 먼저 수신된 동종목 호가가 2초 이내인 체결 | **9** |
| 유효한 앞선 호가와 인과관계 없음 | 25 |
| 연결된 9개 표본의 호가-체결 거래소 시간차 중앙값 | **44ms** |
| 연결된 표본 최대 시간차 | 234ms |
| 실시간 네트워크 재연결·중복·거절 | 모두 0 |
| 서버에 저장한 원본 검증 | SHA256 PASS |

**9개는 우리 주문의 체결이 아니라 공개 거래소 체결과 공개 book의 관측 순서만 맞았다는 뜻.** 이는 매수/매도 1천만 원 시뮬레이션이 실제 체결됐다는 보증이 아니고, 12초의 데이터로 수익률·시장충격 보정도 금지.

실제 첫 단독 실험 증거: `/home/hong/apps/FreeQuantLab-ws-20261021/state/microstructure/websocket-research-smoke-20261021.sqlite3`. 운영 자동 관측 원장은 `/home/hong/apps/FreeQuantLab/state/microstructure/websocket.sqlite3`로 **분리**.

## 운영 자동화

운영용 detached Git worktree: `/home/hong/apps/FreeQuantLab-observer-runtime`. 세션은 간격 중 복구할 수 없는 공백이 생기므로 이를 **종일 연속 WS 운영이라고 주장하지 않는다**. 먼저 저장량·메시지 무결성·실제 지연·운영 리소스를 검증하는 보수적 수집 창으로 시작한다.

- `freequant-websocket.service` 및 timer: 매 KST 분 **02,12,22,32,42,52**에 30초간 독립 WebSocket 공개 구독. 기존 REST **00,10,20,30,40,50** 예약과 충돌하지 않게 분리. `flock` 동시 실행 방지, 75초 시간 상한, user systemd 재부팅 자동 복구.
- `freequant-websocket-audit.service` 및 timer: 매 30분 **07,37분** 지난 48시간 raw bytes·이벤트 인과관계·최근 40분 이내 정상 수집 여부 확인. 검증 미달은 실패(종료코드2) 기록.
- **30초/600초=관측 duty cycle 약 5%.** 미관측 95%에 공개 호가·체결 스트림이 없었다고 주장하지 않는다. REST는 기존 10분 스냅샷의 24시간 독립 품질 기준을 계속 유지.
- 나중에 지속 WS로 전환하려면 24시간 운영/저장량, clock skew, 재연결 및 잠재적 거래소 rate-limit, 스토리지 용량과 파티션/압축을 검증한 뒤 별도 개선이 필요하다.

설치 준비 검사:

```bash
cd /home/hong/apps/FreeQuantLab-observer-runtime
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.websocket_microstructure \
 --db /home/hong/apps/FreeQuantLab/state/microstructure/websocket.sqlite3 --seconds 30
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.websocket_microstructure \
 --db /home/hong/apps/FreeQuantLab/state/microstructure/websocket.sqlite3 --audit
systemctl --user list-timers --all '*websocket*'
journalctl --user -u freequant-websocket.service -u freequant-websocket-audit.service -n 30 --no-pager
```

## 수용 기준과 한계

- 공개 API 연결, 원본 SHA, 실시간 stream type, 30호가 형태, 고유 trade id, 시간 역전/재연결 캐시 초기화, 서비스 권한·timers 검증.
- 녹화된 실물 원본에 대한 오프라인 감사 결과가 동일해야 하고, 서비스 restart 후 중복 trade ID를 다시 체결로 저장하지 않아야 함.
- 주문장에 표시된 bid/ask가 실제 주문 대기열에서 받을 가격이라는 보증은 **불가능**. `myOrder`는 사용하지 않으므로 실제 부분 체결도 0건이며, 고유 거래 체결 프린트를 자신의 체결로 취급하지 않는다.
- baseline, Exaone, Qlib 가상 관측·기존 90일 실험 보호. 어떤 전략도 자동 배포·실거래 승격하지 않는다.
