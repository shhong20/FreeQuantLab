# Agent·Qlib 전략 검증 강화 (2026-10-20)

FreeQuantLab의 `agent-compare` 및 `qlib-compare` 출력은 이제 과거 수익률 표 외에 **중복 팩터 제거, 미래 데이터를 보지 않는 시장 국면 분해, 동일 기간 상대 성과의 블록 부트스트랩**을 제공합니다.

**중요: 이 자료는 연구 진단 정보이고 통계적으로 검증된 알파 또는 실거래 허가가 아닙니다.** 전략이 매우 많거나 같은 OOS 기간을 반복해서 확인했다면 다중 비교·선택 편향은 남습니다. 분리해 놓은 OOS를 이미 여러 번 보았다면 다음 새로운 선행 데이터로 검증해야 합니다.

## 1. 학습 구간에서만 중복 팩터 제거

모델이 제안한 최대 5개 팩터를 먼저 기존 `FactorProposal` 허용 변수·연산자·임계값으로 검증합니다. 다음으로 **학습 시점 이전에 알려진 데이터만** 사용하여 신호의 매수 비중이 2% 미만 또는 98% 초과인 거의 상수 팩터, 그리고 먼저 유지한 팩터와 매매 신호 일치율이 98% 이상인 팩터를 제거합니다.

- 제안 순서를 보존하며 먼저 나온 후보를 유지합니다.
- 단순한 신호 일치율 기준은 수익률 최적화가 아니라 불필요한 검색 공간을 줄이는 장치입니다.
- **검증/테스트 성과를 이용해 팩터를 제거하지 않습니다.**
- 모든 팩터가 걸러지면 B는 현금 보유(CASH)로 남을 수 있습니다. 임의의 대체 모델·실행 코드가 자동으로 활성화되지 않습니다.
- 매 구간별 `factor_screening`에 사유와 학습 구간 매수 비율을 기록합니다.

## 2. 시장 국면별 기여도

종료된 캔들 **t의 종가까지** 이용해 60개 거래일 수익률 기준 ±5% 이상 상승/하락/횡보를 분류합니다. 변동성은 지난 20개 봉의 수익률 표준편차와 과거 90개 봉의 20일 표준편차 중앙값을 비교하여 high/low로 표기합니다.

t가 끝난 이후 다음 시가에 포지션을 잡고, 그다음 시가에서 관찰된 P&L을 **t에 이미 알려져 있던 국면**에 귀속합니다. 미래 가격으로 과거 시장 국면을 분류하지 않습니다.

- `regime_attribution`에는 `days`, `positive_days`, `mean_daily_net_return`, `log_equity_contribution`이 포함됩니다.
- 비연속적인 국면별 `log_equity_contribution`의 합은 전체 OOS `log(1 + 누적수익률)`과 일치합니다.
- 국면별 과거 성과를 바탕으로 앞으로 국면별 전략을 선택하면, 해당 OOS는 모델 선정에 사용된 것이므로 **새로운 기간으로 재평가**해야 합니다.

## 3. 상대 성과의 블록 부트스트랩

4개의 비중첩 OOS 구간에서 **전략 일일 순수익률과 동일 최대자본노출(20%) 매수·보유 순수익률의 로그 차이**를 계산합니다. 각 폴드 내에서 길이 10일의 순환 이동 블록을 1,000회 재표본추출합니다. 평균 수익률 대신 총 `observed_log_excess`, 재표본의 2.5~97.5% 구간, 양수 비율을 기록합니다.

```json
{
  "diagnostics": {
    "benchmark": "buy_hold_same_exposure",
    "comparisons": {
      "B_agent": {
        "observed_log_excess": -0.04,
        "bootstrap_95pct_interval_log_excess": [-0.23, 0.16],
        "exploratory_only": true,
        "multiple_testing_corrected": false,
        "eligible_for_automatic_promotion": false
      }
    }
  }
}
```

예시는 설명용 반올림 값이며 실제 보고서 값은 실행 시 산출합니다.

- 이 구간은 *보고서의 불확실성 진단*이지 다중 후보·시장 검색에 대한 유효한 통계 검정이나 손실확률이 아닙니다.
- 낮은 거래 횟수, 의존적인 수익률, 전략을 이미 테스트 결과로 재조정한 이력은 블록 부트스트랩으로 해결되지 않습니다.
- `bootstrap_positive_fraction`은 향후 수익 확률로 해석하면 안 됩니다.

## 4. 위험 비중에 맞춘 비교

C 위험관리 Agent는 `exposure_scale=0 / 0.5 / 1`을 선택합니다. C가 현금 비중을 늘려 단순히 손실이 적어 보이지 않도록 **동일 폴드에서 buy-and-hold에 같은 scale을 적용**한 `buy_hold_riskmatched_C`와 C의 로그 초과수익 부트스트랩을 추가합니다. `diagnostics.C_vs_riskmatched_buy_hold`를 확인합니다. 이것도 전략 수익의 독립적인 검증은 아닙니다.

## 실행

```bash
cd /home/hong/apps/FreeQuantLab
.venv/bin/python -m pytest -q

# 유료 API 없음, 테스트용 합성 데이터 / Mock Agent
.venv/bin/python -m quantlab --config examples/demo_config.json agent-compare --agent-mode mock

# 실제 시장과 로컬 모델; 실거래 주문은 보내지 않음
.venv/bin/python -m quantlab --config examples/market_research_long.json agent-compare \
  --symbol KRW-BTC --agent-mode local \
  --llm-url http://127.0.0.1:11434/v1 --llm-model exaone3.5:latest

# Qlib LGBModel의 별도 비교에도 부트스트랩/국면 분석 포함
.venv/bin/python -m quantlab --config examples/market_research_long.json qlib-compare --symbol 005930
```

보고서는 `reports/*__agent_compare.json`, `reports/*__qlib_compare.json`에 기록합니다.

## 아직 미구현 및 다음 검증

1. 실제 선행 모의운용(30~90거래일 이상)에서 동일 시점 체결 품질 및 위기 국면 검사
2. 전략 후보 생성 자체를 포함한 다중 비교 보정/다중 구간 선택 편향 평가 및 완전히 독립된 후속 테스트 기간
3. 더 많은 국내·미국 주식의 생존자 편향을 통제한 다종목 및 공식 Qlib Alpha158 벤치마크
4. 주문 승인·계좌 대사·비상정지 등 실거래 안전장치는 아직 별도 개발 대상

**본 실험은 Qlib LGBModel 및 간단한 로컬 전략 제안 Agent를 사용하지만, 원본 Microsoft RD-Agent 전체 프레임워크가 통합된 것은 아닙니다. 실제 계좌 주문 API는 여전히 없습니다.**
