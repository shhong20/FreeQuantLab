# FreeQuantLab | 국내 KRX + 미국 주식 일봉 읽기 전용 자동 관측 (2026-10-21)

## 왜 주식이 운영에서 빠져 있었나?

현재 FreeQuantLab의 shadow 90일 평가, Exaone, Qlib의 실시간 운영 자동화는 BTC/ETH로만 **사전 고정된 가상 코호트**이다. 별도로 삼성자산운용 KODEX 200(069500)·인버스(114800)·골드선물(H)(132030)을 대상으로 과거 NAV/가격 연구가 있었지만, 국내 ETF의 분배금·기업행동·수정종가·총수익률 및 휴장달력을 공식 근거로 검증하지 못했다. 그래서 전략 자동 거래/수익률 승격은 **의도적으로 차단**했으며 주식 자체가 기술적으로 불가능했던 것은 아니다.

## 이번에 직접 구축한 주식 전용 연구 원장

- KRX **삼성전자(005930), SK하이닉스(000660), KODEX 200(069500)**: 네이버 금융 공개 비공식 siseJson 일봉 (API key 불필요).
- 미국 **Apple(AAPL), Microsoft(MSFT), SPY ETF**: Yahoo Finance 비공식 공개 chart API (API key 불필요).
- 별도 저장소: `/home/hong/apps/FreeQuantLab/state/stock_daily/observations.sqlite3` (BTC/ETH `state/shadow.db` 또는 `market_history.db`와 독립).
- Naver는 최대 이전 120일, Yahoo는 3개월을 **원본 바이트 및 SHA-256 포함**하여 수집. *날짜별 원본 OHLCV*를 Append-only 보관하고 과거 데이터가 소스에서 수정되면 기존 봉을 덮어쓰지 않고 revision_alerts에 증거를 남김.
- `PRAGMA quick_check` 및 최근 12건 원본 바이트 SHA 감사, 날짜 중복·OHLC 범위/음수 거래량/진행 중 시장 미확정 봉 차단. 불완전 응답/리다이렉트/종목 변경 시 해당 종목 오류 기록; 전체 서비스가 degraded로 종료하여 무조건 PASS하지 않음.
- Naver 공개 데이터는 공식 KRX 가격 테이프가 아니고 Yahoo는 미국 공식 거래소 원시 호가·체결 자료가 아니다. 무상 공개 endpoint의 사용 조건·쿼터·가용성은 제공자가 바꿀 수 있다. 투자 전략/상업적 실시간 주문에는 공식·허가된 데이터 제공사가 필요할 수 있다.

## 실제 2026-10-21 21:52 KST 서버 관측

| 거래시장 | Symbol | 독립 일봉 세션 | 마지막 관측된 완성 일봉 |
|---|---|---:|---|
| 한국 | 005930 | 81 | 2026-10-21 |
| 한국 | 000660 | 81 | 2026-10-21 |
| 한국 ETF | 069500 | 81 | 2026-10-21 |
| 미국 | AAPL | 65 | 2026-10-20 |
| 미국 | MSFT | 65 | 2026-10-20 |
| 미국 ETF | SPY | 65 | 2026-10-20 |

6종목 438개 완성 일봉, 원본 6개 소스 SHA PASS, 가격/날짜 유효성 PASS, 과거 가격 변경 경고 0개, 데이터 수집 실패 0개. 미국 날짜는 한국 기준 10월21일에 이미 장 마감된 최신 세션이 전일(10월20일)인 것이 정상이다.

## 스케줄 / 실행

시각은 KST이다. `09:30`은 미국 전일 장 종료 후, `21:30`은 국내 야간 거래 관련 시세가 완전히 반영될 시간을 두었다. 미국 뉴욕 기준 `18:00` 전에 진행 중인 미국 일봉은 제외한다. 한국은 `21:00` 전까지 당일 일봉을 제외한다. 휴장·주말 데이터는 임의로 생성하지 않는다.

사용자 systemd 서비스(유저 권한만 필요):

- `freequant-stock-observer.service`: 비공식 공개 API read-only GET만 수행. 저장소 경로만 쓰기 가능하며 모델·paper 거래·주문·계좌 권한 없음.
- `freequant-stock-observer.timer`: `OnCalendar=*-*-* 09:30:00`, `OnCalendar=*-*-* 21:30:00`, `OnBootSec=20min`, `Persistent=true`.

```bash
cd /home/hong/apps/FreeQuantLab-observer-runtime
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python

# 읽기 전용 상태
$PY -m quantlab.stock_daily_observer --audit

# 실제 신규 완성 일봉을 append-only 수집
$PY -m quantlab.stock_daily_observer --capture

# CI 통과/머지 후 사용자 시스템 서비스로 운영 반영
mkdir -p ~/.config/systemd/user
cp deploy/systemd/freequant-stock-observer.service ~/.config/systemd/user/
cp deploy/systemd/freequant-stock-observer.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now freequant-stock-observer.timer
systemctl --user start freequant-stock-observer.service
systemctl --user list-timers --all freequant-stock-observer.timer
journalctl --user -u freequant-stock-observer.service -n 30 --no-pager
```

## 사용자가 해야 할 일은?

**이번 비공식 일봉 관측에는 아무것도 필요하지 않다.** API Key, 증권 계좌, 은행 계좌, 실거래 권한, `sudo` 모두 사용하지 않는다.

추후 **허가된 국내/미국 시세 API, 증권사 실시간 호가·체결 데이터, 모의투자(브로커 제공)**를 원한다면 한국투자증권 KIS Developers 등 증권사 API 신청이 도움이 된다. 공식 안내: https://apiportal.koreainvestment.com/about-howto . 해당 안내에 따르면 계좌 개설/ID 등록/오픈API 신청 후 APP Key·APP Secret을 발급받는다. **개인키·비밀키를 채팅/Git 커밋에 붙여넣지 말고 서버의 별도 비추적 비밀정보 파일로 제공해야 한다.**

주식 종목 추가, 시장(코스피/코스닥/미국 NYSE/Nasdaq) 확장, 공식 가격원 확보, 각 종목 배당/분할·ETF 분배금의 총수익률 검증, 거래일 달력/수정주가 독립 대조, 완전히 격리된 앞으로 90일 주식 가상관측 코호트 등록은 **별도 단계**다. 이번 단계에서 채워진 438개 과거 봉은 라이브 모의매매 이력을 만들지 않는다.

## 통제 조건

No authorized brokerage calls, account positions, synthetic executions, automated model retraining, retrospective paper trades, or claimed total-return alpha. User-visible database is raw price observation only. Historical OHLCV can be provider-adjusted or revised; total-return evaluation remains blocked until exchange/issuer data and distributions/corporate actions are fully verified.
