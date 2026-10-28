# KRX 동일 시각에 근접한 3종 데이터 수집·대조 실험 (2026-10-28)

## 목적과 시장 데이터 계약

삼성전자(005930)·SK하이닉스(000660), KRX J 시장만 대상으로 **호가(10단계), 최근 거래소 체결 틱 30건, 당일 1분봉 30건**을 세 개의 공식 KIS 읽기 전용 기본시세 API에서 순차 조회한다.

- 호가: `inquire-asking-price-exp-ccn` / `FHKST01010200`
- 최근 시장 체결: `inquire-ccnl` / `FHKST01010300`
- 당일 1분봉: `inquire-time-itemchartprice` / `FHKST03010200`
- 참조: https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_time_itemchartprice/inquire_time_itemchartprice.py
- 참조: https://github.com/koreainvestment/open-trading-api/blob/main/legacy/rest/kis_api.py

**거래소 체결 틱은 사용자의 주문 체결 내역이 아니다.** 세 API를 연속으로 읽는 것은 원자적으로 완벽히 동기화된 스냅샷이 아니며, 그 사이 호가/가격/거래량이 바뀔 수 있다.

`quantlab.intraday_krx_joint_evidence`는 09:00~15:20 KST의 평일 장중에만 API를 조회한다. 요청 시작·응답 수신 UTC, 종목, 소스, TR ID, 조회 분봉 커서와 시장 데이터 응답 원문 해시를 저장한다. 인증 정보·브로커 계좌 정보·주문 API 사용 없음. 응답 원문 및 정규화 데이터가 서로 일치해야 후속 연구를 허용한다. 요청을 실패하거나 장을 종료하면 불완전 스냅샷은 최종 보고서로 승격하지 않는다.

## 2026-10-28 실제 시험

**15:17:50 KST**경 KIS API 각 2종목에 대해 체결 틱 30개 및 당일 1분봉 30개 반환을 확인했다.

- 삼성전자 최근 시장 체결가격 241,750원, 15:17 라벨 분봉 종가 241,750원.
- SK하이닉스 최근 시장 체결가격 1,490,000원, 15:17 라벨 분봉 종가 1,490,000원.
- 이후 10호가·최근 거래 틱·분봉을 **연속 수집해 고정된 원본으로 저장하는 단계**에서는 KIS 토큰 새 발급이 HTTP 403을 반환했다. 이 실패는 API 데이터 수집 성과로 계상하지 않았다. 당시 인증된 토큰이 다른 단일 프로세스에서 재사용되는 구조가 아니었고 `data._kis_token`은 프로세스 로컬 lru 캐시라 프로세스를 종료한 뒤에는 토큰 재사용이 안 된다. 원인(발급 제한/권한 등)은 공급사 확인 없이는 확정할 수 없다.
- 실행 실패 시 생성된 빈 시험 폴더는 사용하지 않으며, **KIS 합동 조회 실제 원본 12개가 확보됐다는 보고는 하지 않는다**.
- 이전 PR #108 실측 24개 L2 스냅샷은 13:20/13:23의 호가일 뿐 15:17의 틱과 동시가 아니므로 실제 체결 시각의 호가라고 결합하지 않았다.

## 현재 코드가 할 수 있는 것
- `--capture-dir ... --count 2 --execute`: 2종목×3시장 데이터×2회 = 최대 12개 응답을 조회하고 원본/정규화 응답/체크섬을 불변 저장한다.
- `--source-dir ... --output-dir ...`: 이미 실제로 저장된 동일 회차의 세 자료를 SHA 대조하고, 최근 거래소 체결 틱의 HH:MM 라벨이 당일 분봉의 HH:MM과 일치하는 후보 수 및 가격이 해당 분봉의 OHLC 범위 내인지 검사한다.
- KIS 공식 당일분봉 API는 장중 **미완성 최신 분봉의 거래량이 첫 체결까지 이전 분봉 값으로 표시될 수 있다**고 설명한다. 진행 중 분봉과 최근 체결 30건을 합산해 정확한 분봉 거래량이라고 주장하지 않는다.
- 시간 일치 판정이나 분봉 레이블은 **최종 확정하지 않는다**. 15:32 종가 라벨, VI/CB, 거래소 전체 체결 체인, L2 대기열·취소·숨은 잔량·매매 주문 확인은 별도 증거 필요.
- 공식 증권사 체결증빙이 없으므로 거래율·실제 체결가·100거래일 전략 기대수익·MDD·샤프 산출은 모두 미승인.

## 다음 운영 연구 일정

공급사 OAuth 제한을 확인하고 발급된 토큰은 **인증 정보가 파일/로그에 노출되지 않도록 안전한 토큰 재사용 정책**을 별도 설계해야 한다. 다음 거래일 장중에는 각 시점의 증권사 시세를 동일 회차로 수집하여 현재 비교에서 확인된 5bp의 편차가 변동성·시간대·수량에 따라 달라지는지 평가해야 한다.

명령 예:
```bash
set -a; source ~/.config/freequantlab/kis.env; set +a
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
R=/home/hong/apps/FreeQuantLab/state/intraday_research

# 반드시 실시간 정규장; OAuth 발급 제한 해결 후에만 실행
$PY -m quantlab.intraday_krx_joint_evidence \
  --capture-dir "$R/krx_joint_quote_tick_min_NEXT_SESSION_r1" \
  --count 2 --execute

# 실제 수집 성공 후에만 실행; 현재는 캡처가 없으므로 실행하지 않음
$PY -m quantlab.intraday_krx_joint_evidence \
  --source-dir "$R/krx_joint_quote_tick_min_NEXT_SESSION_r1" \
  --output-dir "$R/krx_joint_quote_tick_min_NEXT_SESSION_audit_r1"
```

이미 승인받은 운영용 90일 평가 `d6b531e`, 실거래/가상주문, 기존 100거래일 5분봉·서킷브레이커 원본은 전혀 변경하지 않는다.
