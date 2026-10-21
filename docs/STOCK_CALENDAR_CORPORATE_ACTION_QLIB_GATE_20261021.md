# FreeQuantLab — 주식 참고 거래일·기업행동·90일 Qlib 사전 적격성 감사 (2026-10-21)

## 이번에 해결한 작업

기존 국내·미국 주식 6종목의 `stock_daily/observations.sqlite3`는 공개 비공식 제공사(Naver Finance / Yahoo Finance) OHLCV 가격·SHA256을 저장하지만, 배당/분할/휴장 자료가 주식 총수익률이나 Qlib의 forward 전략에 사용 가능한지 개별적으로 검증하지 않았다.

추가한 `quantlab/stock_evidence_quality.py`는 **읽기 전용**으로 원본 데이터베이스와 공개시세 스냅샷의 SHA256을 읽고 두 거래소의 거래일 **참고 달력**과 비교한다. 미국 주식은 Yahoo 원본의 배당·분할 이벤트와 adjclose 필드의 존재 및 유효성만 검사한다. 이는 공식 발행사에서 검증된 실제 배당 지급 기록이나 수정 가격의 정확성을 입증하지 않는다.

- 한국 참고 달력: Python `exchange_calendars` XKRX; 미국 참고 달력: XNYS. 거래소 공식 완전 캘린더라고 주장하지 않는다. 없는 일봉/휴장일은 자동 생성·forward-fill하지 않는다.
- KRX 2026-07-17은 참고 달력에는 있으나 네이버 원본 3종목에는 없는 날로 조사됐다. [KRX 모의거래 공지의 제헌절 임시휴장](https://strn.krx.co.kr/corebbs5/BHPSTRN0401/list)과 정합하는 **참고 달력 반영 지연 사례**로 분류하고, 공식 거래일 전체를 인증한 것으로는 취급하지 않는다.
- 미국 AAPL, MSFT, SPY 각각 65개 완성 거래일은 XNYS 참고 달력과 일치. 최신 Yahoo 원본에는 **종목당 공개 배당 이벤트 1건 및 adjusted close 65포인트**가 포함됐다. 제공사 메타데이터와 원본 raw SHA를 같이 증명하되, 공식 배당 지급/원천징수·분할/ETF 전체 분배 내역은 여전히 별도 증명이 필요.
- 국내 005930/000660/069500은 각각 81개 완성 일봉. ETF는 KODEX 200 분배금·지수/결제·corporate action 기록이 공식적으로 완결되지 않아 `KRX_ISSUER_EVENT_HISTORY_NOT_VERIFIED`.
- 기존 코드의 Qlib cross-market은 학습 600세션 + 검증/가드 60 + 미래 관측 여유 2 = **최소 662개 실제 일봉** 필요. 지금 보존된 주식 81/65개로는 **앞으로 90일 주식 Qlib 모델을 등록할 수 없다.**
- 기본 상태 `BLOCKED_STOCK_TOTAL_RETURN_AND_QLIB_FORWARD_REGISTRATION`은 사전 등록 불가능의 **정상적, 보수적 판정**. 가격 수집 서비스 자체가 고장났다는 뜻이 아님. `--strict-eligible`은 90일 가상 관측/종합수익률 승격 시도 게이트에서 exit2.
- 우발적으로 비공식 원본 가격 변경(revision_alerts), 데이터 공급 실패(fetch_failures), 종목이 다른 원본, 스냅샷 SHA 불일치 등을 통과시키지 않는다.
- `freequant-stock-observer.service` 끝에 읽기 전용 품질 검사 추가: 하루 09:30/21:30 KST 주식 데이터 수집 → SQLite raw SHA 감사 → XKRX/XNYS 참고달력·기업행동·주식 Qlib 적격성 감사. 마지막 감사 결과를 `/home/hong/apps/FreeQuantLab/state/stock_daily/quality_reports/`에 **append-only, SHA256이 확인 가능한 JSON 영수증**으로 저장한다.

## 실제 2026-10-21 운영 데이터 품질 결과

| 코드 | 완성된 일봉 | 참고 거래일 차이 | Yahoo 공개 배당 이벤트 | 총수익률·Qlib forward 승인 |
|---|---:|---|---:|---|
| KR 005930 | 81 | 2026-07-17 | 국내 발행사 검증 아직 없음 | BLOCKED |
| KR 000660 | 81 | 2026-07-17 | 국내 발행사 검증 아직 없음 | BLOCKED |
| KR ETF 069500 | 81 | 2026-07-17 | ETF 분배금 공식 이벤트 미검증 | BLOCKED |
| US AAPL | 65 | 없음 | 1개, 출처 Yahoo | BLOCKED |
| US MSFT | 65 | 없음 | 1개, 출처 Yahoo | BLOCKED |
| US ETF SPY | 65 | 없음 | 1개, 출처 Yahoo | BLOCKED |

**Yahoo event가 1개 검색됐다는 사실은 해당 기간 공식 현금 배당 사건이 모두 확보됐다는 뜻이 아니다.** `adjclose`가 존재하는 것도 공개 가격·분배금의 총수익률 정확성을 보장하지 않는다. 거래비용·환율·세금·배당 지급일까지 검증해야 한다.

## 검증·안전

- 신규 `tests/test_stock_evidence_quality.py`: 실제와 동일 구조의 독립 원본 SQLite 생성 후 출처별 reference calendar, 배당 이벤트 존재/부재, issuer 인증 false, SHA 변조, 등록 불가, 2026-07-17 참고 달력 예외, JSON 영수증 기존 파일 불변/원장 경로 거부를 검증.
- 기존 `tests/test_stock_daily_observer.py`에 user systemd 3단계 capture→audit→evidence audit 계약 추가.
- CI 일반 테스트와 실제 Qlib 통합 테스트에도 회귀 테스트를 넣음.
- **실거래, 주식 가상매매, 모델 학습/재훈련/자동 승격, 이전 주식 90일 평가 등록은 모두 활성화하지 않음.**
- 기존 코인/Exaone/Qlib/WS 타이머·원장과 기존 주식 수집·저장된 438개 원본 일봉은 그대로 보존.

## 명령 (관리자/sudo 불필요)

```bash
cd /home/hong/apps/FreeQuantLab-observer-runtime
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python

# 읽기 전용; 부적격도 JSON으로 자세히 알려주며 기본 exit0
$PY -m quantlab.stock_evidence_quality

# 실제 모델/거래 등록을 위한 엄격한 게이트: 데이터 검증 전이면 exit2
$PY -m quantlab.stock_evidence_quality --strict-eligible

# 정상 운용 09:30·21:30 KST user service 3단계 audit 결과
journalctl --user -u freequant-stock-observer.service -n 30 --no-pager
ls -lat /home/hong/apps/FreeQuantLab/state/stock_daily/quality_reports/
```

## 다음 개발

1. 거래소 공식(승인·라이선스에 적합한) 현물 일자·수정주가/기업행동·현금배당/ETF 분배 이벤트를 검증 가능한 원본/라이선스와 묶어야 한다. 한국거래소 [증시일정·휴장일](https://open.krx.co.kr/contents/MKD/01/0110/01100305/MKD01100305.jsp) 조회 기능이 있으며 조회 결과를 임의로 상상하지 않는다.
2. KRX와 미국의 추가 과거 일봉 662세션 이상을 **검증 가능한 가격원**에서 확보하고, 배당/분배·환율·시장 휴장·기업행동을 재현해야 한다. 기존 3개월짜리 공개 API만으로 승인하지 않는다.
3. 역사적 백테스트와는 다른 미래 90일 시험은 당시 고정된 모델/라벨/학습기간/계정/거래비용/가상 주문 시각을 등록·해시 고정한 뒤, 신규 정보만 순차적으로 처리해야 한다. 현재는 이 조건 미충족으로 **가상 거래 시작 불가**.
