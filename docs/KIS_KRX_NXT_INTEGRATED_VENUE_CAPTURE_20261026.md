# KIS 과거 시세: KRX·NXT·통합 분리 수집 및 교차 감사 (2026-10-26)

## 왜 3개 데이터셋인가?

2026-10-23 삼성전자 데이터 확인:
- 기존 KIS `J`(KRX) **15:30 정규장 종가** = 215,500원, 정규장 분봉·종가 단일가 후보 거래량 15,505,124주.
- 기존 네이버 비공식 일봉은 213,500원 / 16,019,854주였다. 이 숫자를 NXT 거래량이라고 **단정할 수 없다**.
- 2026년 9월 14일부터 KRX **16:00~20:00 애프터마켓** 거래를 운영하며, NXT는 08:00~08:50 프리마켓 / 09:00:30~15:20 메인마켓 / 15:40~20:00 애프터마켓이다. 실제 시장별 장 시작/끝과 단일가 시간, 그리고 **시세 공급원 라벨의 뜻**을 구분해야 한다.

KIS 공식 **과거 일자 분봉** 예제는 `FID_COND_MRKT_DIV_CODE`로 `J`(KRX), `NX`(NXT), `UN`(통합)을 문서화한다. 공식 **일별 기간시세** 예제도 같은 코드를 지원한다. 그러나 API 문서가 수집 가능하다고 해도 특정 과거 날짜·종목에 데이터가 있는지, 15:30과 20:00 기준의 정확한 범위인지, 통합 거래량이 단순 합산과 일치하는지는 **실제 공급사 응답으로 검증해야 한다**.

출처:
- 한국투자증권 공식 과거 분봉 예제: https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_time_dailychartprice/inquire_time_dailychartprice.py
- 한국투자증권 공식 일별 기간시세: https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_itemchartprice/inquire_daily_itemchartprice.py
- 한국투자증권 2026-09-08 공지: https://www.truefriend.com/main/customer/notice/Notice.jsp?cmd=TF04ga000002&num=47532
- NXT 공식 거래시간: https://www.nextrade.co.kr/menu/transactionSys.do

## 아키텍처

기존 `quantlab.intraday_kis_probe`는 KRX 15:30 **단일일자 연구** 원본을 보존한다. 새 모듈은 독립 확장 계층이다.

`quantlab.intraday_kis_venues` — `200000` 시작으로 3시장 **독립 과거 분봉**을 조회하고, 동일 시장/종목/거래일에 대해서만 여러 페이지를 병합한다. 최대 120개 응답/페이지와 커서 역방향 단조성·중복 동일성·일자·OHLCV·SHA-256을 검증한다. 단일 일자 KIS 공식 일봉 API `FHKST03010100`도 **시장별로 따로** 읽으며, 조회가 거절되면 분봉 원본 자체는 보존하고 실패 상태만 기록한다.

`quantlab.intraday_kis_venue_compare` — **오프라인**에서 저장된 `J/NX/UN`의 시장코드·종목·날짜·파일 해시를 검증하고, 동일 시각라벨 교집합 수, 통합가격과의 종가 일치 수, KIS 공식 일봉 응답의 거래소별 값을 독립 표시한다.

- `J`: 한국거래소, 09:00~15:20 연속매매 / 15:30 종가 단일가 / 16:00~20:00 애프터마켓. 정규장 이후 분봉이 KIS 과거 API에 실제 존재하는지 별도 확인.
- `NX`: 넥스트레이드, 08:00~08:50 프리 / 09:00:30~15:20 메인 / 15:40~20:00 애프터. 단순 시간대 분류만 수행, 실제 체결 확정 아님.
- `UN`: 통합 시세. **가격 시각라벨로 실제 체결 거래소를 알 수 없고**, `UN 거래량 = J + NX 거래량`을 입증하지 못했다. 따라서 임의로 거래량을 합치거나 차액을 NXT 체결량이라고 간주하지 않는다.

결과는 `usable_for_backtesting=false`, `automatic_5min_conversion=false`로 유지하고, 별도 가상·실매매/운영 원장에 접근하지 않는다.

## KIS 키가 저장된 로컬 WSL에서 최초 1일 실행

> ChatGPT 원격 도구의 API 인증키 전송은 보안 검사로 차단될 수 있다. 해당 경우 사용자 본인이 WSL 셸에서 아래 **읽기 전용** 명령을 실행한다. 키·토큰은 절대 대화나 GitHub에 붙여 넣지 않는다.

```bash
cd /home/hong/apps/FreeQuantLab-intraday-20261026

set -a
source ~/.config/freequantlab/kis.env
set +a

/home/hong/apps/FreeQuantLab/.venv/bin/python \
  -m quantlab.intraday_kis_venues \
  --symbol 005930 \
  --session 2026-10-23 \
  --markets J,NX,UN \
  --out-dir /home/hong/apps/FreeQuantLab/state/intraday_research/venues
```

**수집 경로** (시장별 파일과 해시가 분리됨):

```text
state/intraday_research/venues/
  KIS_J_EXT_005930_2026-10-23/
    raw/minute_000.json ... raw/daily.json
    minute_observations_UNVERIFIED.json
    audit.json
  KIS_NX_EXT_005930_2026-10-23/
    ...
  KIS_UN_EXT_005930_2026-10-23/
    ...
```

일부 시장 API가 날짜·종목 지원을 거절할 경우, 이미 성공한 시장의 저장본은 유지된다. 같은 경로를 다시 덮어쓰지 않으며 `--markets NX,UN`처럼 **실패한 시장만** 재시도할 수 있다. 원본이 없거나 반환 건수가 적으면 운영 추정이나 결측 분봉 자동 생성으로 대체하지 않는다. 대량/병렬 호출은 하지 않는다.

실제 3시장 스냅샷을 모두 확보한 후 오프라인 비교:

```bash
/home/hong/apps/FreeQuantLab/.venv/bin/python \
  -m quantlab.intraday_kis_venue_compare \
  --root /home/hong/apps/FreeQuantLab/state/intraday_research/venues \
  --symbol 005930 \
  --session 2026-10-23 \
  --out-dir /home/hong/apps/FreeQuantLab/state/intraday_research/venue_comparisons
```

이 비교는 같은 **시간라벨이 있는지** 검사하는 것이며, 거래 체결이 중복되었는지 입증하는 것은 아니다. 실제 중복 여부는 거래소별 체결 ID·독립 체결 원장 또는 제공사 공식 유동성 집계 규칙의 추가 근거가 필요하다.

## 후속 관문

1. 실계정에서 J/NX/UN의 각 **2026-10-23 원본** 수집 성공/거절 여부를 확인하고 원본 전체 SHA를 보존한다.
2. KIS 과거 분봉 시간라벨이 시가/종료시각 중 무엇인지, 누락 분봉과 비거래 종목·상장폐지·유상증자/분할의 의미를 공급사 일봉과 일치시킨다.
3. **정규장 ORB**는 KRX 정규장 데이터만 먼저 검증하고, **애프터마켓/NXT**는 별도의 체결·슬리피지·가격충격 모델을 거친다. 통합 분봉을 KRX 09:00~15:20 76개 봉과 섞지 않는다.
4. 모든 누락·분기·비교 결과를 감사로 남긴 뒤 여러 거래일과 여러 종목의 실제 과거 자료를 모아서 시간순 워크포워드 테스트를 진행한다.
