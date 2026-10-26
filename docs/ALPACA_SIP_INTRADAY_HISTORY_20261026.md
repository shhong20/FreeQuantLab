# 주식 5분봉 실데이터 연구: Alpaca SIP 원본 수집 (2026-10-26)

## 목적

FreeQuantLab `main`의 독립적인 ORB(5/15/30분) · 갭 하락 평균회귀 · 장 초반 30분 모멘텀 비교 엔진에 **검증 가능한 실제 5분봉 원본**을 연결한다. 이 단계의 소스는 Alpaca **미국 주식** 과거 시세이며, 한국 KIS 분봉은 별도의 시간라벨·누적거래량·최대 1년 보관 정책 검증이 필요하다.

- API: `GET https://data.alpaca.markets/v2/stocks/{symbol}/bars`
- 요청: `timeframe=5Min&feed=sip&adjustment=raw&sort=asc`, 모든 페이지 `next_page_token` 처리.
- 출처: [Alpaca 단일 종목 과거 Bars](https://docs.alpaca.markets/us/reference/stockbarsingle-1), [Alpaca 시장데이터 FAQ](https://docs.alpaca.markets/us/v1.1/docs/market-data-faq).
- SIP는 전체 미국 거래소 결합 체결 기반 과거 데이터. Alpaca의 실시간 플랜 제약과 과거 조회 권한은 **사용자 계정·시점별로 다를 수 있으며** 실제 응답 확인 전 무료 사용이 보장되지 않는다.
- IEX는 거래량 커버리지가 제한되어 ORB 상대 거래량 연구에 부적합. `--feed iex` 명시 허용은 **자료 검사 전용**으로, 백테스트 수익률 산출 자체를 차단한다.

## 준비 및 실행 (키는 절대 채팅·저장소에 올리지 않음)

운영 WSL에 인증된 Alpaca **시장데이터 API 키**가 있는 경우:

```bash
cd /home/hong/apps/FreeQuantLab
git pull --ff-only
source .venv/bin/activate

# 환경변수는 각자의 안전한 비밀 관리 경로에서 주입.
# APCA_API_KEY_ID / APCA_API_SECRET_KEY 를 셸·로그에 노출하지 말 것.
python -m quantlab.intraday_alpaca \
  --symbol SPY \
  --start 2024-01-01 --end 2025-12-31 \
  --feed sip \
  --out-dir /home/hong/apps/FreeQuantLab/state/intraday_research
```

실행 전 키가 없으면 명확히 실패한다. **주문권한이 필요하지 않으며 주문 API는 호출하지 않는다.** GitHub CI는 모의 응답만 사용한다. 데이터 수집본은 Git에 커밋하지 않는다.

> 2024–2025 기간은 예시. 2026년 10월 기준 안전한 과거 완료 기간이며 시장 데이터를 확보했음을 의미하지 않는다.

## 불변 원본 및 처리 규칙

`state/intraday_research/SPY_2024-01-01_2025-12-31_sip/`

- `raw/alpaca_page_0000.json` 등: 받은 HTTP 응답 **원문 바이트**, 페이지별 SHA256은 `manifest.json`에 보존.
- `bars_5m.csv`: 미국 정규장(뉴욕 시간 09:30–16:00) 78개 5분봉이 정확히 존재하는 세션만 남긴 검증 데이터. 원문에서 없는 분봉을 0거래량이나 전일가로 **절대 채우지 않는다**.
- `manifest.json`: 원본 건수, 페이지 수·각 SHA, CSV SHA, 공급원, 조회 범위, 원래 수신된 세션 중 버린 부분 거래일과 사유, IEX 차단 게이트, 거래기능 비활성 상태.
- `report.json`: SIP 검증 데이터에서만 기존 평가기를 실행한 결과. **실주문 수익률이나 순자산으로 해석하지 않는다**.
- 한번 성공적으로 생성한 경로는 **자동 덮어쓰기하지 않는다**. 재조회·소스 수정·조회범위 변경은 기존 스냅샷과 해시 비교 후 새 디렉터리에 별도로 남긴다.
- 요청일은 과거 완결된 미국 날짜만 허용하고 요청 범위는 최대 1096일. 과거 기간 최소 90일, 시장 완결 5분봉 60세션 이상, 원본 바 이상·범위 밖·중복·정렬오류·페이지순환·인증실패·부분 전송은 오류로 차단한다. 장 전·장 후 분봉은 버리며 현물 정규장 외 시세로 백테스트하지 않는다.
- 공휴일이거나 누락된 거래일은 이 단계에서 자동 생성되지 않는다. 조기 폐장 세션은 분봉이 78개 미만이므로 **전체 제외**되고 명시적으로 기록된다. 공급원·거래소 캘린더 간 날짜 대조, 거래정지, 주식분할 및 배당 검증은 후속 관문이다.

## 실제 수익률 해석 제한

실행되는 전략 엔진은 거래량 필터·다음 봉 시가 진입·마지막 봉 시가 청산·수수료와 슬리피지를 반영한 **단일 종목·단일 거래 가정 백테스트**다. 매수/매도 체결 가능량·호가잔량·시장충격·기업행동 검증은 제공하지 않는다. 현행 엔진의 갭 전략은 `corporate_actions_reviewed=False`로 **의도적으로 봉쇄**한다. 데이터만으로 모든 단타 기법의 우월성과 과거 전략의 일반화 성능을 입증할 수 없다.

CI `python -m pytest -q tests/test_intraday_alpaca.py`는 합성 데이터 페이징·키 누설 방지·결측/중복/시간대·불변 저장·IEX 차단을 검증할 뿐 실제 외부 API에 접속하지 않는다.

## 한국 5분봉 후속 기술 관문

한국투자증권 오픈 트레이딩 API의 `inquire-time-dailychartprice`는 최대 1년 동안 보관된 과거 분봉 조회를 지원하며, 1회 최대 120건 형태로 페이지를 나눌 수 있다. 그러나 과거 분봉이 체결 **종료 시각**을 의미하는지, 해당 `cntg_vol`이 실제 분별 거래량인지, 장 마감 15:30분 데이터와 누적값 보정이 맞는지 먼저 실제 샘플 1일을 받아 확인해야 한다.

공식 예제: https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/domestic_stock/inquire_time_dailychartprice

그 전에 1분봉을 5분봉으로 임의 변환하거나 미국 5분봉 규칙을 KRX 장 마감 체결에 그대로 적용하지 않는다.
