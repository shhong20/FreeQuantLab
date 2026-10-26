# KRX 장중 서킷브레이커 인식형 5분봉 및 워크포워드 안전 게이트 — 2026-10-26

## 목표

기존 FreeQuantLab `quantlab.intraday_research`는 **거래일당 76개의 완전한 정규장 5분봉**과 최소 60거래일을 요구한다. 과거 KIS KRX J 1분봉 원본 120개(삼성전자 005930·SK하이닉스 000660 × 60일)를 실제 수집했지만, 2026-07-28, 07-29, 10-15에는 코스피 시장 전체 거래중단(서킷브레이커)이 보도됐고 해당 시각에 분봉 공백이 존재한다.

정규장이 중단된 분봉을 **거래량 0 / 직전 종가 반복 / 가상 5분봉**으로 채우면 실제 거래 가능했던 것처럼 보이게 된다. 또한 거래중단일을 검증 기간에서 몰래 제거하면 하락장 위험이 과소평가된다.

이번 모듈은 전략 엔진을 변경하거나 백테스트를 실행하는 대신, **76개의 고정 시간 슬롯에 5분 OHLCV가 실제로 존재하는지와 해당 일의 거래 가능성 불확실성**을 기록한다.

## 외부 사실과 신뢰 수준

2026년 KOSPI 1단계 서킷브레이커 발동 보도:
- **7월 28일 10:13:43 KST** — https://v.daum.net/v/20260728101810442
- **7월 29일 12:32:32 KST** — https://v.daum.net/v/20260729125639247
- **10월 15일 12:38:12 KST** — https://ir.newspim.com/news/view/20261015000760

KRX 1단계 CB 처리 규칙은 **20분 거래 중단 + 이후 10분 단일가매매**(유안타증권 안내): https://www.myasset.com/myasset/customer/notice/CU_0201000_P2.cmd?SEQ=202609101421050000000004

**중요:** 발동 시각은 보도로 확인한 값이다. 공식 거래소 실행 원장/분별 실제 재개 체결시각을 검증한 것은 아니다. 코드의 발동 시각부터 30분까지는 *보수적 연속매매 불가 후보 구간*이며, 단일가매매 주문 수락·체결 시간이나 종료 시각을 공식 인증한 것이 아니다.

## 구현

`quantlab.intraday_krx_halt_5m`

- 기존 원본 검증 `_read_one`을 사용하여 KIS J 1분봉 원문·해시를 검증, 전체 60일×2종목을 대상으로 거래일당 **76개** 5분 시간 슬롯을 반드시 보존한다.
- 각 슬롯을 구성하는 5개 원본 분봉이 **전부 존재**하면 5분 시가/고가/저가/종가/거래량을 계산한다.
- 하나라도 분봉이 빠졌으면 해당 슬롯의 `ohlcv = null`. 1~4개의 부분 체결은 독립 보존·합계 확인하되 **부분 체결만으로 5분 봉을 만들지 않는다**.
- 분봉이 빠진 시간과 보도된 CB 시간대가 겹치는지 **1분 단위**로 판단해 `INCOMPLETE_CB_CORRELATED_UNVERIFIED`, `INCOMPLETE_CB_AND_UNEXPLAINED_GAPS`, `INCOMPLETE_SOURCE_GAP_UNEXPLAINED`로 구분한다.
- 15:30 분봉이 없고 15:32 라벨만 있으면 `END_MARKER_UNVERIFIED`로 표시. 15:32가 확정 종가라고 추정해 대입하지 않는다.
- **CB가 발생한 날짜 전체에 `ENTIRE_DAY_STRATEGY_PNL_BLOCKED_MARKET_HALT_RISK`**를 붙인다. 이 날짜를 학습·테스트 수익률 산정에서 자동으로 버리지 않는다.
- 원본에 5분 OHLCV가 모두 있더라도 `vendor_timestamp_start_end_contract_verified=false`, `backtest_pnl_enabled=false`, `continuous_order_execution_allowed=false`. 종목 분할·배당/비용/호가/재개경매의 독립 검증은 아직 되지 않았다.
- 저장물: 종목별 60일×76개 JSON, 요약 JSON, 각각 SHA-256. 원본을 덮어쓰지 않으며 원자적인 새 디렉터리로 저장한다.

## 실제 WSL 120개 원본 전수검증

입력:
`/home/hong/apps/FreeQuantLab/state/intraday_research/venues_cohort_20261026_run1`

처리 결과:
- 종목·거래일 **120개** 모두 raw SHA 검증
- 5분 시간 슬롯 **9,120개** (60 × 2 × 76)
- 5개 1분봉을 전부 확보한 슬롯 **9,074개**
- 일부 1분봉이 없어 OHLCV `null`로 남긴 슬롯 **46개**
- **110개 종목·거래일**은 기존 기준의 380개 정규장 라벨과 15:30 라벨을 모두 보유
- **6개 종목·거래일**(7/28, 7/29, 10/15 × 2종목) 시장 CB 보도와 일치하는 시간대 존재 → 위험일 전체 차단
- **2개**(7/31 × 2종목)는 원인을 모르는 개장 직후 분봉 누락
- **2개**(8/13, 10/08, 삼성전자)는 정규장 380개를 확보했지만 15:32 종료 라벨
- 정상 가용 완전 일수는 삼성전자 54일, SK하이닉스 56일. 따라서 단순히 CB 날짜를 제외한 `intraday_research.validate/evaluate` 수행은 **60일 조건도 충족하지 않으며 하방 위험 선택편향**을 만든다.

실제 수집 후 품질 감사 참고 파일:
`/home/hong/apps/FreeQuantLab/state/intraday_research/krx60_source_quality_20261026_r2/quality_report.json`

## 오프라인 실행

```bash
cd /home/hong/apps/FreeQuantLab-intraday-20261026
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
R=/home/hong/apps/FreeQuantLab/state/intraday_research
DAYS=$("$PY" - "$R/krx60_backfill_plan_20261026_r1/plan.json" <<'PYCODE'
import json,sys
print(",".join(json.load(open(sys.argv[1]))["sessions"]))
PYCODE
)

"$PY" -m quantlab.intraday_krx_halt_5m \
  --source-root "$R/venues_cohort_20261026_run1" \
  --out-dir "$R/krx60_halt_5m_20261026_r2" \
  --sessions "$DAYS"
```

*주의:* 동일 불변 출력 폴더가 이미 존재하면 덮어쓰지 않는다. 이 단계에서 OAuth, HTTP 요청, 계좌/운영 원장, 매매 주문은 어떤 것도 사용하지 않는다.

## 다음 게이트

1. KIS 제공사에 `stck_cntg_hour` 1분봉 시간 라벨 시작/끝 의미와 장 종료 경매 이벤트(15:30 vs 15:32)의 시각 정의 문의.
2. 공식 KRX CB 발동·해제·재개경매의 개별 체결원장 또는 기계 판독 공시 확보해 보도 기반 마스크와 대조.
3. 7월 31일 개장 직후 09:00~09:03 미확인 분봉 공백의 독립 재조회. **기존 원본 수정 금지**.
4. 워크포워드 평가에서는 `HALT_RISK`일을 단순 삭제하는 대신 포지션을 보유한 채 거래가 멈췄을 때의 평가·미체결·재개 이후 가격 위험을 따로 모델링해야 한다. 현재 코드는 해당 모델이 없어 **수익률/샤프/최대낙폭을 계산하지 않는다**.

현재 운영 중인 90일 평가와 매매 서비스 코드는 변경하지 않는다.
