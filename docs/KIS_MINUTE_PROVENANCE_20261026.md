# 한국투자증권 KIS 과거 1분봉: 원본 1일 감사 전용 (2026-10-26)

**아직 5분봉으로 변환하거나 수익률을 산출하지 않는다.**

KIS 주식일별분봉조회 API `FHKST03010230`은 날짜·시간으로 최대 120행씩 과거 분봉을 조회할 수 있다(일반적으로 최대 약 1년 보관). 단, `stck_cntg_hour`가 개별 1분봉의 **시작시각인지 종료시각인지**, `cntg_vol`의 누적/체결 의미, 15:20~15:30 종가 단일가 표시 규칙은 실제 제공사 응답과 일봉을 독립 대조해야 한다.

공식 예제: https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/domestic_stock/inquire_time_dailychartprice

## 기존 KIS 인증 환경의 한 날짜 읽기 전용 진단

```bash
cd /home/hong/apps/FreeQuantLab
git pull --ff-only
source .venv/bin/activate

# 기존 quantlab.data._kis_token()이 읽는 KIS_APP_KEY,
# KIS_APP_SECRET은 기기 내 보안 환경에서만 주입.
python -m quantlab.intraday_kis_probe \
  --symbol 005930 \
  --session 2026-10-20 \
  --out-dir /home/hong/apps/FreeQuantLab/state/intraday_research/kis
```

- 실제 KIS 연결과 인증된 접근권한이 있을 때만 실행 가능. 비밀키·토큰은 원본/감사 파일에 저장하지 않는다.
- `KIS_005930_2026-10-20/raw/page_000.json` 등 원본 응답을 바이트 그대로 저장하고 SHA-256을 계산한다.
- `minute_observations_unverified.json`은 제공사가 반환한 **원래 시각 문자열**과 원래 가격·거래량을 유지한다. 절대 1분봉의 표기 시각을 임의로 1분 빼거나 5분봉으로 합치지 않는다.
- `audit.json`에 페이지 수, 중복 일치/충돌, 날짜 일치, 시간대 검사, 09:00~15:19의 380개 **시간라벨** 대비 수신된 라벨 개수, 종가 단일가 구간의 라벨, 조기 종료/페이지 상태를 남긴다. 시간라벨 380개가 있다고 의미 검증이 완료된 것이 아니다.
- KRX 15:20~15:30은 종가 단일가로, 매수·매도 가능 연속 5분봉으로 취급하지 않는다.
- `automatic_5min_conversion=false`, `backtest_or_orders_allowed=false`로 영구 구분한다.
- 동일 종목·일자 기존 성공본이 있으면 자동 덮어쓰기하지 않고 거부한다.
- 주말·당일·미래·보관기간을 넘은 날짜, 인증실패, 날짜 오류, 음수거래량, 가격 오류, 동일 시간 충돌은 거부한다.
- KRX 주식 전용 `J`을 사용하고 NXT 통합 호가·거래 데이터는 섞지 않는다.

## 후속 수행 조건

1. KIS 원본 실표본(장 개시, 접속매매, 15:20~15:30, 무거래 1분 포함)을 공급사 설명 및 한국거래소 체결데이터와 교차 검사해 분봉 **시작/종료 라벨**을 확정한다.
2. `cntg_vol`이 개별 1분 체결량이라는 독립 증거를 확인하고 일별 누적값·거래정지·단일가 구간을 구분한다.
3. 완결된 연속접속 1분봉을 올바르게 묶어 **76개 5분봉 09:00~15:15**로 생성한다. 누락 1분은 0으로 채우지 않고 해당일을 제외한다.
4. 별도의 **공식 15:30 종가** 및 기업행동/ETF 분배금 근거를 확보한 후에만 한국 갭 전략을 평가한다.

이 PR은 기존 90일 평가, 코인 시세, 주식 일봉 DB, paper/live 거래를 변경하지 않는다. CI는 전부 **가짜 제공사 응답**으로만 검증한다.

## 요청일 이전 분봉이 섞이는 API 페이지 경계 (2026-10-26 수정)

- 주식일별분봉조회 `FID_PW_DATA_INCU_YN`은 이제 공식 예제의 기본값과 같은 `N`으로 요청한다. 이전 `Y` 설정은 조회 구간이 요청일의 09:00 이전으로 넘어가면 이전 영업일 분봉을 포함할 수 있다.
- 제공사가 `N`에도 이전 영업일 분봉을 반환하면 **원본 HTTP 페이지는 SHA-256과 함께 보관**하고, 1일치 검증 대상에는 `stck_bsop_date`가 지정 날짜와 같은 행만 포함한다. 이전 영업일 행 수를 `excluded_previous_session_rows`로 기록하고 페이지네이션을 종료한다.
- 더 미래 날짜나 잘못된 날짜는 오류로 차단하고, **지정 날짜 분봉이 한 건도 없으면 오류**로 종료한다.
- 결측 1분봉을 보간하지 않으며, 다른 날짜의 시간 라벨이 충돌해도 지정 날짜 데이터에 덮어쓰지 않는다.
- 이 단계의 출력은 원본 감사 전용으로, 여전히 `automatic_5min_conversion=false`, `backtest_or_orders_allowed=false`이다.

공식 공급사 예제: https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_time_dailychartprice/inquire_time_dailychartprice.py
