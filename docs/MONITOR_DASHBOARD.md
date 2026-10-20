# FreeQuantLab Shadow Paper 운영 대시보드

> **읽기 전용 연구 모니터링.** 거래소 주문 전송, 전략 변경, 원장 수정, 전략 자동 승격은 지원하지 않습니다.

## 위치와 기능

- WSL 서버: `DESKTOP-S51Q473`
- 설치 디렉터리: `/home/hong/apps/FreeQuantLab`
- 대시보드: [http://localhost:8788](http://localhost:8788) (동일 컴퓨터에서만 접속, WSL Windows localhost 전달 설정에 따라 달라질 수 있음)
- 실제 바인딩: `127.0.0.1:8788` (외부 LAN 접속 차단)
- 읽을 파일: `state/shadow.db` — `mode=ro` + `PRAGMA query_only=ON`; 기존 `freequant-shadow.timer`를 조작하지 않음

외부 CDN이나 유료 계정·클라우드가 필요 없고 표준 Python의 HTTP 서버와 로컬 HTML/CSS/JS만 사용합니다. UI는 60초마다 실시간으로 **조회만** 합니다.

## 대시보드 표시

- BTC·ETH **6개 독립 가상 전략**: 시작 가상자본, 이후 미래 날짜의 기록 기준 관측 손익, 최대 관측 낙폭, 가상 주문 횟수, 마지막 관측 날짜
- 선행 일일 원장에 기록된 가상 평가액의 추이 (관측값만 연결, 과거 가격으로 소급 계산 금지)
- 최신 가상 거래 상세 (BUY/SELL/HOLD/HALT, 관측 가격과 기간)
- 최근 스케줄 실행 이력, 최근 7일의 실패 실행 횟수(최근 100회 표본 기준)
- 자동 모니터링 경고: `NO_TRACKS`, `RISK_HALTED`, `DECISION_MISSING`, `NO_CYCLE_TODAY`, `LATEST_RUN_FAILED`
- KST **10:20 이후**에만 신규 일봉 기록 누락을 경고합니다. 전략 등록 직후에는 새 일봉이 도착하기 전까지 `incubating`으로 표시해 가짜 운영 장애를 만들지 않습니다.

**중요:** 아직 미래 시장에서 기록된 거래가 없다면 표시되는 시작 가상 평가액 100만 원은 초기값일 뿐이며, 수익률은 0%가 아닌 **관측 대기(—)** 입니다. 시장가격이 없는 날의 평가액을 만들어내지 않습니다.

## 설치 및 자동 재기동

```bash
cd /home/hong/apps/FreeQuantLab
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
mkdir -p ~/.config/systemd/user
cp deploy/systemd/freequant-dashboard.service ~/.config/systemd/user/
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
systemctl --user daemon-reload
systemctl --user enable --now freequant-dashboard.service
systemctl --user status freequant-dashboard.service
```

WSL에서 이미 `loginctl show-user hong -p Linger`는 `yes`로 확인했습니다. WSL 자체가 꺼져 있으면 서비스도 실행되지 않으며, 시작되면 user systemd가 다시 실행합니다.

명령으로 수동 실행(서비스와 동일 포트를 동시에 열 수 없음):

```bash
.venv/bin/python -m quantlab.dashboard --db state/shadow.db --bind 127.0.0.1 --port 8788
```

## 조회 및 이상 감시

```bash
curl --fail http://127.0.0.1:8788/healthz
curl --fail http://127.0.0.1:8788/api/status
systemctl --user status freequant-dashboard.service
journalctl --user -u freequant-dashboard.service -n 50 --no-pager
systemctl --user list-timers --all freequant-shadow.timer
```

API는 **GET /api/status**, **GET /healthz** 이외에 정적 화면만 제공합니다. POST/PUT/DELETE 및 임의 파일 경로는 거절합니다. Content Security Policy와 no-store 헤더를 사용하며, 기본적으로 localhost 요청 Host만 허용합니다. 서비스에서 API 키 및 주문 기능을 직접 사용할 수 없습니다.

### 데이터/보안 주의

`http://localhost:8788`에는 사용자 계정 로그인 기능이 없습니다. **localhost 바인딩을 해제하거나 0.0.0.0에 공개해서는 안 됩니다.** 원격 외부 접속이 필요하면 개인 인증 SSH 터널 등의 별도 인증 경로를 먼저 구성하세요.

현재 경고는 웹 화면과 SQLite에 기록된 운영 상태에 표시되며, SMS·이메일·메신저 자동 통지는 지원하지 않습니다. 대시보드에 경고가 없더라도 시장에 유리한 전략이라는 뜻은 아닙니다. 데이터 수집·서비스 장애의 최종 알림을 원하면 별도 읽기 전용 알림 송신 시스템을 구성해야 합니다.

[Shadow Paper 원장 사양](FORWARD_SHADOW_PAPER.md)
