# 청정 90일 가상매매: 판단일·증거·전략별 기록 종합 감사 (2026-11-03)

## 운영 관측 증거

2026-11-03 11:2x KST에 실제 WSL에서 등록된 **선행 90일 독립 청정 평가**를 원본 DB·동결 프로토콜과 대조한 결과:

- 등록 코호트: BTC·ETH × 세 전략 `buy_hold_20pct`, `trend_ma20_80`, `factor_r5_gt0` = 고정 6개 계정.
- 90일 의사결정 기준: 2026-10-22부터 2027-01-19까지 90개 **UTC 날짜** (코인은 주말에도 거래됨). 평가 전 발표 금지: 2027-01-20T00:00Z.
- 감사 시각의 마감된 의사결정일: **12일** (10/22~11/02 UTC). 예정된 계정·일자 **72**, 실제 기록된 유효 거래 판단 **30**, 누락 **42**, 유효하지 않은 저장된 판단 증거 **0**.
- **완전 관측일 5개**: 2026-10-22, 10-27, 10-30, 10-31, 11-02. 각 6개 코호트 계정 판단을 원래 UTC 세션(익일)에서 저장, 당시 Upbit 공개 티커 호가와 120초 이내 연결.
- **모두 결측인 7개**: 2026-10-23, 24, 25, 26, 28, 29 및 11-01. 해당 날짜마다 6계정 판단 모두 없음.
- 일곱 결측 날짜 전부 그 **다음 UTC 세션에 늦게 실행된 Shadow Paper run 영수증**이 있으며, 해당 run 결과는 `OUTSIDE_OBSERVATION_WINDOW`. 서비스 최상위 `OK`는 마감 시간을 지난 실행이 안전하게 차단됐다는 뜻이지 **매매 판단 기록 성공을 뜻하지 않음**. WSL/PC 종료 등 실제 OS 레벨 원인은 이 DB만으로 확증할 수 없다.
- 공식 `inspect_clean()`은 `INCOMPLETE`, `protocol_collection_status=BREACH_OR_MISSING`; 독립 원장 감사 `PASS`, 공개 호가 감시 `PASS`, 기록된 계정·일자 증거 오류 0.
- **완성률 30/72=41.7%**는 데이터 관측률이지 승률이 아니다.

## 전략별 관측된 행동 (증거 검증 완료된 다섯 판단일만)

| 자산 | 전략 | 유효/예정 | BUY | SELL | HOLD |
|---|---|---:|---:|---:|---:|
| BTC | buy_hold_20pct | 5/12 | 2 | 0 | 3 |
| BTC | trend_ma20_80 | 5/12 | 2 | 1 | 2 |
| BTC | factor_r5_gt0 | 5/12 | 0 | 0 | 5 |
| ETH | buy_hold_20pct | 5/12 | 2 | 0 | 3 |
| ETH | trend_ma20_80 | 5/12 | 1 | 1 | 3 |
| ETH | factor_r5_gt0 | 5/12 | 0 | 0 | 5 |

모두 원장에 기록된 **가상 판단 액션의 수**이며 실제 현금계좌 주문이 아니다. HOLD는 보유 상태 유지일 수도, 현금 유지일 수도 있으므로 이를 무조건 매수·매도 적중으로 해석하지 않는다. 결측 7일을 앞뒤의 HOLD나 추정한 매매로 이어 붙여 수익률을 계산하지 않는다.

Qlib LightGBM·Exaone 같은 **별도 연구 장부는 이 6개 동결 90일 코호트에 포함되지 않음**. 독립적 모델 평가 추적 시 각각의 운영 장부와 프로토콜을 별도 감사해야 한다.

## 신규 독립 감사 코드

`quantlab.clean_90d_evidence_coverage`:
1. 저장된 Git 버전/원본 SHA와 무관하게 동결 프로토콜 JSON의 SHA와 계정 정체성, 독립 ledger/quote 감사, UTC 의사결정 기간의 예정 여부를 다시 확인.
2. Shadow Paper DB를 `mode=ro`, `query_only=ON`으로 열고 등록 계정·가상 이벤트·공개 호가·실행 영수증을 하나의 **일관된 읽기 트랜잭션**으로 취득.
3. 의사결정 날짜별로 등록된 6개 계정의 `VALID / MISSING / INVALID`를 표시. 유효 판정은 익일 UTC 세션, 익일 00:10~01:20 UTC 범위 내 영수증, 120초 이내 공개 티커 호가, 일치하는 가격·거래 대상, BUY/SELL 방향 및 계정 신원을 검증.
4. 결측 날짜의 run 영수증을 분리해 `AFTER_WINDOW_RUN_RECEIPT_WITH_NO_DECISION`, `NO_RUN_RECEIPT_FOR_EXPECTED_SESSION`, `RUN_RECEIPT_BUT_NO_RECORDED_DECISION` 등 **실제 관측된 기록에만 기반한 진단**을 기록. 최상위 run 상태와 `OUTSIDE_OBSERVATION_WINDOW` 결과 코드를 별도로 표시.
5. 계정별 BUY·SELL·HOLD 총횟수와 결측/검증 실패 건수를 저장하지만 예측 적중률·샤프·수익률은 `null`로 유지.
6. 원본 DB/프로토콜/전략/90일 장부는 **절대 수정하지 않고**, 새로운 비공개 리포트 폴더에 JSON + SHA-256 저장. 중복 출력 경로는 차단한다.
7. 읽기 전용 재검증은 당시 감사 시각까지만 발생한 이벤트·공개 호가·실행 영수증을 재조회한다. 이후 새 날짜의 정상 관측이 생기더라도 **기존 감사 날짜의 결측을 사후 체결로 인정하지 않는다.**

실제 보관 위치:
```text
/home/hong/apps/FreeQuantLab/state/research_audit/
    clean_90d_coverage_20261103_r1/
        report.json       (0600)
        report.sha256     (0600)
```

오프라인 검증:
```bash
cd /home/hong/apps/FreeQuantLab-observer-runtime
P=/home/hong/apps/FreeQuantLab/.venv/bin/python
$P -m quantlab.clean_90d_evidence_coverage --verify-only \
  --db /home/hong/apps/FreeQuantLab/state/clean_shadow_90d.db \
  --protocol /home/hong/apps/FreeQuantLab-clean-runtime/protocols/2026-10-21-btc-eth-clean-90d.json \
  --output-dir /home/hong/apps/FreeQuantLab/state/research_audit/clean_90d_coverage_20261103_r1
```

## 판정과 다음 우선순위

- 현재 신뢰할 수 있는 것은 **등록 계획에 대한 관측률·가상 신호의 생성 여부·수집 타이밍·증거 정합성**이다. 실제 수익률·최종 전략 비교·샤프/승률·미래 유의성은 아직 **검증 불가**.
- 남은 42개 account-day는 결측으로 고정. 서버 재부팅 후 과거의 종가를 이용해 지연 생성하는 것은 별도 *사후 재현/백테스트*일 수 있으나 **기존 전향적 가상거래 기록으로 소급 저장하면 안 됨**.
- 운영 개선은 미래 00:10~01:20 UTC 창 이전의 WSL 접근 가능성을 높이는 것으로 한정하고, 수집 창을 놓쳤을 때는 **원인을 기록하되 모의 체결을 만들지 않는 기존 안전 동작을 유지**한다.
- 제어되지 않은 서버 오프라인 시간 때문에 90일 프로토콜 완료일에 90일 모두 유효할 수 없는 상태다. **지금 새로운 90일 기산일을 자동으로 재설정하거나 성과 검증에 편입하지 않음**. 새로운 코호트 시작은 사전등록과 사용자 승인 후 별도 판단할 것.
