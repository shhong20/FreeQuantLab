# FreeQuantLab | KRX·미국 장기 가격 증거 원장 및 자동 갱신 (2026-10-22)

## 범위

주식 자동 수집 원장 `state/stock_daily/observations.sqlite3`를 그대로 두고, **장기 가격 증거 전용** `state/stock_daily/history.sqlite3`에 6개 종목의 공개 공급원 장기 응답을 별도 기록한다. 이는 비공식 공급원 조회를 통한 **연구 데이터**이지 허가된 공식 시세 테이프, 기업행동·배당·분배금의 확인, Qlib 전략의 실전 성능 검증을 의미하지 않는다.

| 시장 | 종목 | 관측일봉/종목 | 날짜범위 | 출처 |
|---|---|---:|---|---|
| 국내 | 005930 / 000660 / 069500 | 각 1,420 | 2021-01-04~2026-10-21 | 비공식 Naver Finance `siseJson` |
| 미국 | AAPL / MSFT / SPY | 각 1,254 | 2021-10-21~2026-10-20 | 비공식 Yahoo Finance Chart `range=5y` |
| 합계 | 6종목 | **8,022** | | |

실제 기존 stock daily 원장과 중복된 **KR 종목별 81세션, US 종목별 65세션**에 대해 각 OHLCV를 비교했고 기존 원본 대비 가격 충돌 **0건**, 누적 소스 응답 SHA256 **PASS**이다. 각 제공사 raw JSON/응답 원문을 실제 바이트와 SHA256로 보존한다.

Yahoo 제공사 원본 응답에서 5년 범위의 **미국 종목별 현금배당 이벤트 20건(합계 60)**과 **미국 수정종가 시계열 3,762포인트**를 따로 보관했다. 이 금액·수정 가격은 Yahoo의 게시 이벤트이며 발행사/ETF 운영사/거래소에서 독립 증명된 것이 **아니다**. 국내 배당/ETF 분배금은 해당 비공식 Naver OHLCV만으로 확인하지 않았다. `stock-paper orders`와 실주문 기능은 **계속 비활성**이다.

## 2026년 KRX 휴장일 달력 버전 차이

설치된 `exchange_calendars 4.13.2`의 KRX(XKRX) 참고 세션과 2021~2026 국내 일봉 날짜를 비교할 때 2026-06-03 지방선거일, 2026-07-17 제헌절 두 날짜에서 달력에는 개장일이 있으나 실제 네이버 주식 일봉은 없다. 이 **참고 라이브러리의 예외 기록**은 한국거래소의 2026-05-20 증권·파생 시장 휴장 안내에 근거한다.

공개 기사: https://v.daum.net/v/20260520103802540

해당 두 날에 임의 0거래량 호가/일봉을 생성하거나 forward-fill하지 않는다. 그 외 조회 범위에서 XKRX 참고 달력 대비 알 수 없는 누락 0건; 미국 XNYS 참고 달력 대비 누락·초과 날짜 0건이다. 참고 라이브러리는 공식 전체 시장 달력에 대한 감사 서명이나 본 거래소 전체 기초 데이터를 뜻하지 않는다.

## 재조회에서 발생한 미세 수정종가 재계산

동일 Yahoo 5년 자료를 다시 조회했을 때 기존 저장된 3,762개 미국 수정종가 일부가 Float32 수준에서 미세하게 달라졌다.

- 기존 버전 대비 변경 2,187포인트는 모두 기존 수정종가 대비 약 **0.0053bp 이하**.
- 모든 raw 응답을 별도 `raw_history`에 보존하고, 재수집 과거 값이 다르면 무조건 기존 `vendor_adjusted` 행을 덮어쓰지 않는다.
- 비교 허용 경계 `relative_tol=1e-5` (0.1bp)은 공급원의 부동소수점 정밀도 흔들림을 **품질 감사상 비중요 변경**으로 분리하는 역할이다. 기존에 기록된 2,187건의 비교 기록은 수정하지 않고, `retained_vendor_adjustment_precision_jitter_events=2187`로 남아 있다. 상대 기준을 넘으면 `material_price_or_adjustment_conflicts`로 보고 및 실패(exit2).
- 어떠한 경우에도 **제공사 수정종가가 총수익률의 발행사 검증 증거인 것은 아니다**.

## 스케줄 및 운영 분리

- **기존 주식 일일 수집**: 09:30 / 21:30 KST `freequant-stock-observer.timer`.
  기존의 공개 일봉 수집→SHA감사 뒤에 **장기 원장 전체 소스 SHA 및 참고달력 감사**를 끼우고, 마지막으로 공식 기업행동/총수익률 차단 상태를 출력·주식전용 JSON 영수증으로 발행한다.
- **새 장기 가격 주간 자동 갱신**: `freequant-stock-history.timer` 토요일 **11:40 KST**, `Persistent=true`(WSL/user systemd 종료 중 누락 시 재기동 이후 따라잡음). 캡처→장기 SQLite 전체 raw SHA/거래일 감사 후 종료. 기존 `daily_bars`와 독립 `long_bars` 저장, 원본 SHA, 과거 수정 충돌 검증, 서버 원본 백필이나 임의 휴장 봉 생성 없음. 주말에 6종목을 조회하며 예측 주문·포트폴리오 거래·Qlib 모델 학습 실행 **없음**.
- 둘 다 `systemd --user`, `NoNewPrivileges=true`, `ProtectSystem=strict`, `ProtectHome=read-only`, 주식 전용 `ReadWritePaths`. 주간 작업은 `Nice=10` / idle I/O 우선순위.
- 주간 캡처는 **장기 시장기록 5년을 재요청**하므로 공개 공급원의 사용 제한과 변경 가능성, 네트워크 실패/다운타임을 고려해야 한다. 시간대와 미국 시장 토요일 오전(직전 금요일 미국 완성 일봉) 기준으로 예약했다.
- 코인/Exaone/Qlib/기존 90일 코호트/WS 증분 백업 타이머는 이 변경으로 전혀 수정하지 않는다. NAS/물리적 다른 서버 원격 백업과도 무관하다.

## 실제 개발·검증 결과

2026-10-22 오전 개발 시점:

- WSL 장기 `history.sqlite3` 8,022일봉·raw 응답 누적 12건; 과거 일봉 가격 충돌 0, 유의미한 수정종가 차이 0, 미세 정밀도 보존 기록 2,187개; 모든 raw payload SHA256 PASS.
- 국내 1,420 / 미국 1,254 세션 모두 기존 Qlib code의 **길이 기준 662개를 초과**. 하지만 공식 KRX/미국 주식 corporate action 전체 원본, KODEX ETF 분배금, 거래비용·세금·통화환산·수정주가 변환 및 실제 **미래 정보 누출 방지 Qlib 모델 평가**는 미검증.
- `stock_evidence_quality`에서 장기 가격 충분 여부를 표시하되 종목별 `eligible_for_qlib_forward_paper=false`, `90_day_forward_qlib_registered=false`, `total_return_performance_publishable=false` 고정.

## 사용법 (일반 사용자 권한만 필요)

```bash
cd /home/hong/apps/FreeQuantLab-observer-runtime
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
$PY -m quantlab.stock_long_history --audit
$PY -m quantlab.stock_evidence_quality

# 공개 장기 가격을 명시적으로 한 회 최신화
$PY -m quantlab.stock_long_history --capture

# CI 검증 후 별도 user unit 설치
mkdir -p ~/.config/systemd/user
cp deploy/systemd/freequant-stock-history.service ~/.config/systemd/user/
cp deploy/systemd/freequant-stock-history.timer ~/.config/systemd/user/
cp deploy/systemd/freequant-stock-observer.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now freequant-stock-history.timer

# 수동 운영 테스트; 성공하면 inactive(dead), Result=success 정상
systemctl --user start freequant-stock-history.service
systemctl --user start freequant-stock-observer.service

systemctl --user list-timers --all freequant-stock-history.timer freequant-stock-observer.timer
journalctl --user -u freequant-stock-history.service -n 40 --no-pager

# 사용자가 새 주간 자동 갱신만 중단하려면
systemctl --user disable --now freequant-stock-history.timer
```

`sudo`는 필요하지 않다.

**남은 관문:** 공식·허가된 전체 일봉 및 분배금/배당/분할·ETF 기업행동을 원문 SHA로 재검증하고, 학습 600일+가드60일+2일의 과거 이용 가능 시점만 사용한 frozen Qlib 모델/워크포워드·누설 방지 검증을 거쳐야 미래 주식 가상관측 등록을 검토할 수 있다. 8,022개 가격 확보만으로 90일 평가나 수익률을 소급 생성하지 않는다.
