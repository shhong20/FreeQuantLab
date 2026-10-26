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

## 실제 3시장 2026-10-23 삼성전자 검증 및 오류 발견 (10월 26일)

WSL에서 `KIS_J_EXT_005930_2026-10-23` / `KIS_NX_EXT_005930_2026-10-23` / `KIS_UN_EXT_005930_2026-10-23` 세 불변 스냅샷과 페이지별 SHA-256을 읽기 전용 검증했다. **세 시장 모두 과거 1분봉과 KIS 공식 일봉 원본 저장 성공**, 비밀키 미사용.

| 구분 | 정규장 포함 기록된 1분 시간 라벨 | 공식 미수정 일봉 종가 | 공식 미수정 일봉 거래량 | 관측 1분 `cntg_vol` 합계 | 분봉-일봉 거래량 |
|---|---:|---:|---:|---:|---:|
| J=KRX | 621 | 215,500 | 16,145,972 | 15,966,889 | −179,083 |
| NX=NXT | 690 | 213,500 | 10,629,482 | 10,629,164 | −318 |
| UN=통합 | 691 | 213,500 | 26,775,454 | 26,211,542 | −563,912 |

- **공식 일봉 거래량 합계 검증:** 16,145,972 + 10,629,482 = **26,775,454**. 따라서 *이 종목·날짜의 KIS 일봉*에 한해 공급사가 제공한 거래량은 두 시장 합과 일치한다. 이는 **모든 날짜에 대해 항상 참임을 의미하지 않음**.
- **1분 단위 합계 검증:** 통합 스냅샷 시간 라벨 집합은 KRX와 NXT의 합집합과 동일(691개). 이 중 **689개 시간 라벨에서는 통합 1분 거래량 = J+NX**, 단 **2개가 다름**.
  - **09:07**: J=102,331 / NX=41,781 / UN=143,952. 통합 값이 단순 합보다 **160주 적음**.
  - **09:08**: J=157,838 / NX=226,919 / UN=406. 통합 값이 단순 합보다 **384,351주 적음**.
  - 이 두 시간 차의 합은 **−384,511주**로, J+NX 관측 분봉 총합 **26,596,053주**와 UN 관측 분봉 총합 **26,211,542주**의 차이와 같다.
- **09:08 가격 범위도 불일치:** J/NX 시각별 고가를 합친 후보는 **205,500원**, 저가는 **203,500원**. 그러나 UN 09:08 원본은 **고가=저가=205,000원, 거래량=406주**. 해당 값은 JSON 원본 `minute_005.json`에도 실제 존재했으며, 우리 정규화 계산 오류가 아니다. *원인(제공사 과거 통합 분봉 결손, 체결시간 정의 차이, 소급 수정 등)은 미확정*.
- KIS `J` 일봉은 **15:30 정규장 가격 215,500원**을 종가로 표기하면서 거래량은 애프터마켓을 포함하는 것으로 보이는 **16,145,972주**를 제공한다. 정확한 공식 집계 정의는 미확정.
- `NX` 및 `UN` 일봉 종가 **213,500원**은 KRX 15:30 종가와 다르다. 출처/시각 혼동이 이전 네이버 일봉 차이의 원인임을 강하게 뒷받침한다.
- 관측된 모든 분봉을 단순 합산해도 공식 일봉 총수량보다 작다. 누락 또는 공급사 장/시간 집계 정의의 차이 중 무엇인지 독립 검증 전 **미확정**.

새 오프라인 비교 코드 `quantlab.intraday_kis_venue_compare`의 `minute_volume_arithmetic_diagnostic`, `minute_price_envelope_diagnostic`, `official_daily_volume_arithmetic_diagnostic`에 자동 수치, 차이 및 `MISMATCH_HOLD`를 기록한다. 정상 합계를 찾았다고 원본을 고치거나 임의로 빠진 분봉을 채우지 않고, 계속 `usable_for_backtesting=false`다.

실제 결과 저장 경로:
`/home/hong/apps/FreeQuantLab/state/intraday_research/venue_comparisons_v2/KIS_J_NX_UN_005930_2026-10-23/audit.json`

### 남은 객관적 검증

1. **09:07·09:08** 각 시장 `FHKST03010230` 과거 분봉을 동일 원본 파라미터로 **새 경로에 재수집**하고, 기존 3개 원본 해시와 비교해 일시적 응답 불일치인지 재현성 확인. 기존 스냅샷은 덮어쓰지 않는다.
2. 공식 분봉의 체결시간 `stck_cntg_hour` 기준, `cntg_vol` 의미, 20:00 잔여 체결 포함 여부를 KIS/KRX/NXT 문서와 대조한다.
3. 종목·거래일 다변화(여러 종목, 다수 거래일) 후 어느 구간의 체결량·시간라벨에 반복적으로 불일치가 있는지 보고한다.
4. **세 시장 정규/장후 분리, point-in-time 기업행동·유동성·비용·슬리피지**를 충족한 자료만 다음 단계 백테스트에 사용할 수 있다. 운영 90일 평가나 실주문은 이 감사 단계에서 수정하지 않는다.
