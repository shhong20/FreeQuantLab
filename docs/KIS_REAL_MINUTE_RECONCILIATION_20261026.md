# KIS 실제 1분봉 정합성 → 5분봉 후보 감사 (2026-10-26)

목적: **실제 삼성전자 KIS 2026-10-23 분봉 원본을 보존**하고, KRX 연속매매 1분봉 380건을 5분봉 76건으로 합친 **감사 후보 자료**와 기존 네이버 비공식 일봉의 불일치를 독립 기록한다. 분봉 시각의 시작/끝 의미 및 KIS 거래량의 시장 범위를 공식적으로 승인하기 전에는 **실제 전략 성과 산출에 쓰지 않는다.**

## 확인된 사실 (로컬 실제 증거)

- KIS `FHKST03010230` API 실수집: 2026-10-23 / 삼성전자(`005930`), 원문 4페이지, SHA-256 전부 일치, 정상 대상일 381개 분봉, 경계 중복 3개 값 동일.
- 09:00~15:19 시간라벨 380개 존재, 15:30 종가 단일가 라벨 1개. 15:20~15:29는 연속매매 분봉으로 취급하지 않음.
- 원본 시각을 해당 1분 **시작으로 가정**해 09:00~15:20 미포함 마지막 구간까지, 연속 5분씩 묶어 76봉 후보 생성. **출처의 시각 해석은 아직 공식 검증되지 않음.**
- 5분봉 후보 시가 206,000원, 고가 215,500원, 저가 203,500원, 마지막 연속매매가 215,500원. 연속매매 거래량 합 14,273,417주.
- 별도 보관하는 15:30 경매 체결 가격 215,500원, 수량 1,231,707주. 단순 합산 후보 15,505,124주.
- 현재 기존 `state/stock_daily/history.sqlite3`의 네이버 비공식 일봉 원장은 같은 날 시가 206,000 / 고가 215,500 / 저가 203,500 / **종가 213,500원** / 거래량 **16,019,854주**. 거래 범위 차이 또는 원장 수정 여부 확인 전까지 일치로 간주하지 않음.
- 차이: 종가 **+2,000원**, 분봉 거래량 합 **−514,730주**. 왜 다른지는 공식 일별/거래소 범위 데이터 교차확인 필요. **원본 일봉 수정·대체 금지**.

## 실행 (네트워크·인증키 미사용, 오프라인)

```bash
cd /home/hong/apps/FreeQuantLab-intraday-20261026
/home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.intraday_kis_reconcile \
  --snapshot /home/hong/apps/FreeQuantLab/state/intraday_research/kis/KIS_005930_2026-10-23 \
  --daily-db /home/hong/apps/FreeQuantLab/state/stock_daily/history.sqlite3 \
  --out-dir /home/hong/apps/FreeQuantLab/state/intraday_research/derived_candidate
```

출력: `KRX_5M_CANDIDATE_005930_2026-10-23/candles_5m_CANDIDATE_ONLY.csv`, `audit.json`.

프로그램은 SHA-256 원본 일치, 날짜, 380개 1분 라벨 연속성, 15:30 단일가 분리, OHLCV 숫자·가격 일치, 5분 집계와 후보 해시를 검사한다. 기존 경로 재실행 시 덮어쓰지 않고 중단한다. 기존 일봉 SQLite는 `mode=ro`로만 조회하고 절대 수정하지 않는다.

## 승격 금지 조건

`usable_for_backtesting=false`, `usable_for_paper_or_live=false`이며 상태는 `KIS_5MIN_CANDIDATE_UNVERIFIED_NO_BACKTEST`. 자료 해시가 맞아도 거래 가능 수량/정규장 범위, 호가단위, 거래정지, 기업행동, 1분봉 시각의 의미를 인증하는 것은 아니다.

60일 이상 전략 비교 전 필요한 관문:
1. 원본 시간라벨이 체결이 속한 1분의 **시작**인지 독립 검증. 공식 KIS 문서는 `stck_cntg_hour`를 주식 체결 시각으로 표기함.
2. 분봉 `cntg_vol`과 일봉 거래량 사이 KRX·NXT·시간외/정규장 범위 차이 확인. 미확인 상태에서 거래량을 억지로 맞추거나 미체결 분봉을 0으로 보정 금지.
3. KIS 공식 **일별 가격·거래량** 응답을 동일 시장 코드/날짜로 보존해서 네이버 일봉과 비교. 불일치가 있으면 공급원별 값을 별도 보존하고 실거래 승격 금지.
4. 수십 거래일 독립 원본을 안전한 속도로 수집해 완결된 세션만 검증하고, 다음 봉 체결 비용·슬리피지·기업행동을 검토.
5. 그 이후에도 워크포워드 미사용 테스트와 가상·실주문 승격은 **별도 검증 및 별도 승인**.

이번 단계에서는 외부 인증 API를 원격으로 호출하거나 운영 서비스 설정을 변경하지 않았다.

## 공급원·거래소 시간 범위 교차 검증 (2026-10-26 추가)

실제 KIS `FHKST03010230`을 `FID_COND_MRKT_DIV_CODE=J`로 조회했으므로 원본의 시장 범위는 **KRX**다. KIS 공식 API 스키마는 `J=KRX`, `NX=NXT`, `UN=통합`을 명시한다. NXT/통합 분봉은 별도 수집과 거래시간 계약이 필요하다.

- KIS 2026-10-23 삼성전자 KRX 15:30 정규장 단일가: **215,500원, 15,505,124주**.
- 다른 금융 시세 사이트의 **당일 15:30** 기록이 **215,500원, 15,505,124주**로 원본과 완전히 일치한다.
- 기존 FreeQuantLab 네이버 비공식 일봉: **213,500원, 16,019,854주**. 거래량의 KRX 대비 차이는 **514,730주**, 그러나 이를 *NXT 거래량이라고 단정할 수 없다*.
- 다른 사이트의 **당일 19:59:59** 기록: **213,500원, 16,145,972주**, KRX 15:30 대비 누적 차이 **640,848주**, 네이버 원장 대비 **126,118주** 많다. KRX 정규 종료 이후 다른 범위·시점 데이터와 종가가 혼합될 가능성을 뒷받침한다.
- Npay증권 정책상 지표별 기준(정규장 종가 대 최종 체결가, 시간외/대체거래소 거래량 합산)이 다르다. KRX 및 NXT, 장후 거래의 **각각의 체결량을 분해한 원본은 확보하지 않았으므로**, 어느 거래소가 514,730주를 기여했는지 지금 확정하지 않는다.
- 같은 DB의 `long_bars.market='KRX'`는 **내부 데이터 시장 분류**에 불과하며, 공급자가 실제 `KRX 15:30`의 가격과 거래량만을 제공했다는 의미가 아니다.

공식·외부 근거:
- KIS 시장코드: https://github.com/koreainvestment/open-trading-api/blob/main/MCP/Kis%20Trading%20MCP/configs/domestic_stock.json
- 2026년 Npay 증권 시간/가격 및 거래량 범위 안내: https://help.pay.naver.com/faq/content.help?faqId=18245
- KRX 15:30 시세 스냅샷: https://mbnmoney.mbn.co.kr/stock/item/chart?code=005930
- 2026-10-23 19:59:59 현물 시세: https://alphasquare.co.kr/home/stock-summary?code=005930

이제 독립 감사의 `requested_vendor_venue_code=J`, `comparison_is_like_for_like=false`, `reference_venue_time_scope=UNVERIFIED_KRX_NXT_EXTENDED_HOURS_MIX_POSSIBLE`, `difference_attribution=NOT_DETERMINABLE_FROM_TWO_TOTALS_ALONE`를 기록한다. 숫자 불일치는 지우거나 강제로 보정하지 않는다. 같은 값을 기록하더라도 실제 거래소와 거래시간 일치가 독립 검증되지 않으면 연구/거래 승격을 차단한다.

**다음 실제 자료 검증**은 동일 날짜 KIS 공식 일봉을 시장코드 `J` 및 별도 `NX`, `UN`으로 읽기 전용 조회하고, 제공되는 정확한 거래시간 계약과 시장별 거래량 범위를 검사하는 것이다. 공식 확인 전에는 NXT/통합 1분봉을 KRX 76봉으로 합치지 않는다.
