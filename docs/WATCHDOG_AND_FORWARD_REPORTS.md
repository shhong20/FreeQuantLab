# FreeQuantLab 일일 관측 보고서·지속 감시 운영 안내

**작성 기준: 2026-10-20 · 현재 실제 계좌 주문/전략 자동 승격 없음.**

Shadow Paper 원장(`state/shadow.db`)을 **읽기 전용**으로 분석하여 매일 KST 날짜의 관측 보고서를 생성하고 운영상 장애/복구를 별도 알림 원장에 기록합니다. 기존 모의투자 서비스·타이머 및 전략은 변경하지 않습니다.

## 기능

1. **일일 관측 보고서**: `reports/forward/YYYY-MM-DD.json`, `.md`. 실제 미래 일봉에서 기록된 BTC·ETH의 각 독립 가상계좌별 누적 관측 수익률, 관측 MDD, BUY/SELL 횟수, 누적 가상 수수료·슬리피지, 관측 누락일을 기록합니다. 매수·보유 비교는 처음/마지막 관측일 및 관측 횟수가 동일할 때만 표시합니다.
2. **경고/복구 이력**: `state/watchdog.db`에 `active_alerts` 및 `notification_events`를 별도로 생성합니다. 원장의 활성 장애가 처음 나타날 때 알림, 해결되면 복구 알림을 기록합니다. 같은 지속 장애는 매 실행마다 중복 기록하지 않습니다. 중대 장애만 24시간 이상 지속되면 재안내합니다.
3. **Windows 데스크톱 알림(최선 노력)**: WSL의 Windows PowerShell/Windows Runtime Toast API로 이상 알림만 전달합니다. 로컬 Windows 권한/알림 설정 때문에 API 호출이 성공해도 실제 화면 표시를 보장하지는 않습니다. API/전달 실패 시 최대 세 번 재시도하고 오류 종류를 SQLite 원장에 남깁니다. Windows 알림 센터에 보이지 않더라도 로컬 원장 및 대시보드에서 경고를 확인할 수 있습니다.
4. **대시보드**: [http://localhost:8788](http://localhost:8788)에서 기존 전략별 자산 현황에 더해 일일 보고서 및 최근 경고/복구를 표시합니다. `/api/latest-report`와 `/api/alert-history`는 로컬에서만 이용할 수 있는 **읽기 전용 GET** API입니다.
5. **실거래 차단**: 보고서·알림은 전략 점수에 따른 자동 고정/승격 또는 주문 API를 사용하지 않습니다.

## 매일 자동 실행 (KST)

`freequant-watchdog.timer`는 **매일 10:30 / 12:30 / 18:30 KST**에 실행됩니다. 기존 `freequant-shadow.timer`는 09:15 / 09:25 / 09:35 / 10:00에 실행되므로 독립적인 읽기 프로세스끼리만 공존합니다. 대시보드 이상 및 미관측 경고는 마지막 Shadow 시도 이후 10:20이 지나면 발생할 수 있습니다.

```bash
cd /home/hong/apps/FreeQuantLab
.venv/bin/python -m pytest -q
mkdir -p ~/.config/systemd/user reports/forward
cp deploy/systemd/freequant-watchdog.{service,timer} ~/.config/systemd/user/
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
systemctl --user daemon-reload
systemctl --user enable --now freequant-watchdog.timer
systemctl --user list-timers --all freequant-watchdog.timer
```

시작 직후 보고서를 한번 생성해볼 때:

```bash
# Windows 팝업 없이도 보고서와 알림 원장은 계속 동작
.venv/bin/python -m quantlab.watchdog \
  --shadow-db state/shadow.db \
  --state-db state/watchdog.db \
  --report-dir reports/forward

# 조회 전용
cat reports/forward/$(date +%F).md
journalctl --user -u freequant-watchdog.service -n 40 --no-pager
```

시스템 서비스는 `--windows-toast`를 사용하므로 **경고가 새로 발생하거나 복구되면** Windows에 알림 API를 호출합니다. 장애 없이 평온한 경우 알림을 보내지 않습니다. 인위적인 장애를 운영 가상계좌에 발생시켜 시험하지 마세요. 오프라인 회귀 테스트에서만 장애/복구를 시뮬레이션합니다.

## 운영 내역 확인

```bash
.venv/bin/python - <<'PY'
import sqlite3
with sqlite3.connect('state/watchdog.db') as c:
    print('Active alerts:',c.execute('SELECT code,severity,last_seen FROM active_alerts').fetchall())
    print('Recent notifications:',c.execute(
        'SELECT event_time,code,event_kind,delivery_status,attempts,last_error '
        'FROM notification_events ORDER BY id DESC LIMIT 10'
    ).fetchall())
PY
```

- `delivery_status='submitted'`: Windows Toast API 호출을 성공적으로 마침. **화면 표시 성공을 뜻하지는 않음**.
- `delivery_status='failed'`: Windows 호출 실패, 최대 3회 재시도. 실패 횟수/사유 기록.
- `delivery_status='not_requested'`: Windows 알림 비활성으로 실행됨. 로컬 이력은 유지.
- `delivery_status='stale_skipped'`: 서버 재시작으로 24시간 이상 지난 알림은 팝업으로 지연 발송하지 않음.
- 장애로 보고서를 읽거나 저장할 수 없다면 정상 보고서를 지어내지 않고 **DEGRADED**로 기록합니다.

## 데이터 해석 유의

기존 연구 백테스트 수익률을 일일 보고서에 합산하지 않습니다. 미관측 날짜를 직전/가상 가격으로 채우지 않습니다. 가상계좌마다 **서로 독립적인 100만원**이 있으므로 원화 잔고/수익률을 전체 포트폴리오처럼 더해서는 안 됩니다. 관측 1~몇 일에 연율화 Sharpe 또는 '향후 승리 확률'을 계산하지 않습니다. 장기적으로 실제 체결 가능가격·기업행동·미체결·시장 충격 등을 해결하기 전에는 실거래 승인 근거로 사용할 수 없습니다.

### Windows 알림 제한

사용자 로그온 세션, Windows 알림 사용 여부, PowerShell 알림 AUMID 등록 상태 및 WSL interop 제한에 따라 실제 화면 알림이 보이지 않을 수 있습니다. API 호출 경로가 가동 가능하더라도 물리적 화면 표시 검증은 별개입니다. 이메일/텔레그램 등 외부 알림 전달은 별도 인증 자격증명과 사전 설정 없이는 자동으로 사용하지 않습니다.
