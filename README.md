# FreeQuantLab — 무료 로컬 AI 퀀트 연구·선행 모의투자

**Version 0.2.0 — Research MVP, not an automated live trader.** 

> 수익을 보장하지 않습니다. **실거래 주문 코드 없음 / 실제 주문 차단**. 외부 유료 LLM API 및 유료 클라우드는 필요하지 않습니다. 실거래 수수료·세금·전기료·시장 데이터 약관은 별도입니다.

## 기능과 현재 구현 수준

| 모듈 | 상태 | 메모 |
|---|---|---|
| 코인 OHLCV | 구현 | Upbit KRW 일봉, 페이징·미완성 봉 제외 |
| 국내주식 | 구현 | KIS Open API, API 키 필요. 실환경 호출 미검증 |
| 미국주식 | 구현 | Stooq CSV 무료 공개 데이터, 제한 가능. 실시간 호가 아님 |
| AI 예측 | 구현 | LogisticRegression, HistGradientBoosting; 선택적 LightGBM |
| 전략 탐색 | 구현 | 이동평균 3 + ML 후보 6 |
| 백테스트 | 구현 | 시간순 train/validation/untouched test, 다음 시가, 비용 근사 |
| Agent A/B/C | 실험적 구현 | Agent 없는 기준선 / LLM 팩터 제안 / LLM 위험 비중 수정 |
| Walk-forward | Agent 실험 한정 | 4개 확장형 학습·검증·테스트 구간 |
| 모의매매 | 구현 | SQLite, 종목별 별도 가상계좌 및 중복 신호 방지 |
| 실거래 | **미구현** | 계좌 잔고대사·체결/부분체결·비상정지 검증 전까지 차단 |
| 웹 UI | 미구현 | CLI 및 JSON 보고서 사용 |

**중요:** 원본 Microsoft RD-Agent, Qlib, TradingAgents를 설치한 것이 아닙니다. 이 프로젝트는 해당 연구를 참고한 **작고 통제된 별도 프로토콜**입니다. 코드가 돌아가는 것과 실제 수익성을 입증한 것은 다릅니다.

## 설치

Python **3.11 이상** 환경에서:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\Activate.ps1
python -m pip install -e '.[dev,ml]'
python -m pytest -q
```

LightGBM을 사용할 필요가 없으면 `pip install -e '.[dev]'`만 실행합니다.

## 오프라인 데모 (유료 API 없음)

```bash
python -m quantlab --config examples/demo_config.json research
python -m quantlab --config examples/demo_config.json agent-compare --agent-mode mock
```

두 명령은 **합성 시계열**만 사용합니다. `mock`은 LLM이 아니라 고정 규칙으로 Agent 입출력을 재현합니다. **이 수익률은 실제 시장·LLM 수익률이 아닙니다.** 연구 결과는 `reports/`에 저장됩니다. 데모/CSV/Stooq는 `freeze` 불가합니다.

## 무료 시장 데이터 연구

```bash
python -m quantlab --config config.public.json research
```

`config.public.json`에 KRW-BTC, KRW-ETH(업비트) 및 미국주식 aapl.us, spy.us(Stooq)가 있습니다. 한국 주식(삼성전자 005930)을 연구하려면 KIS 무료 개발자 API를 등록하고, `config.example.json`을 사용합니다.

```bash
export KIS_APP_KEY="..."
export KIS_APP_SECRET="..."
python -m quantlab --config config.example.json research
```

Windows PowerShell에서는 `$env:KIS_APP_KEY='...'` 와 `$env:KIS_APP_SECRET='...'`을 사용하세요. **API 비밀키, 가상계좌, 보고서 등은 GitHub에 커밋하지 마세요.** 인터넷에서 데이터를 가져오는 API 호출은 환경에 따라 실패할 수 있습니다.

## Agent A/B/C 비교

실제 로컬 OpenAI 호환 LLM 서버가 `http://127.0.0.1:8000/v1/chat/completions`에서 실행 중일 때:

```bash
python -m quantlab --config config.public.json agent-compare \
  --agent-mode local --llm-url http://127.0.0.1:8000/v1 \
  --llm-model YOUR_LOCAL_MODEL_ID --with-lightgbm
```

- **A:** 이동평균 10/40, 20/80, HistGradientBoosting(p≥0.55) 및 선택적 LightGBM(p≥0.55). 검증으로 선정.
- **B:** 로컬 LLM이 미리 정의된 9가지 feature에 대한 비교 연산을 이용해 최대 5개 팩터 제안. 임의 코드 생성/실행 금지.
- **C:** 검증 구간 통계만 보고 B 신호의 투자 노출을 0·50·100%로 조정.
- **Benchmark:** 동일한 최대 자본 노출 비율의 Buy & Hold.
- **Walk-forward:** 훈련 50/60/70/80%, 다음 10% 검증, 다음 10% OOS 테스트, 겹치지 않는 4개 테스트.
- **Stress:** 설정된 수수료와 슬리피지를 각각 2배로 적용한 수익률도 기록.
- **Agent 보안:** 루프백 HTTP만 허용. JSON allowlist 검증. 모델 오류가 생겨도 Mock이나 외부 모델로 자동 대체하지 않습니다.
- **실거래 연결:** A/B/C에는 `promotion_eligible=false`, `real_orders_enabled=false` 고정입니다.

결과: `reports/<provider>__<symbol>__agent_compare.json`. OOS 결과를 사용하여 A/B/C를 다시 선택한다면 그 구간은 더 이상 완전히 미사용된 테스트셋이 아닙니다. 새로운 미래 데이터에서 재평가해야 합니다.

## 전략 고정과 선행 모의투자

```bash
python -m quantlab --config config.public.json freeze --symbol KRW-BTC
# 전략을 고정한 시점 이후, 새로 완료된 일봉이 들어올 때만:
python -m quantlab --config config.public.json paper --symbol KRW-BTC
python -m quantlab --config config.public.json status --symbol KRW-BTC
```

`freeze`는 개발용 백테스트 임계치를 통과한 Upbit/KIS 자산에 한정됩니다. `paper`는 해당 신호 이후 **새로운 거래 세션에 조회한 현재 시세**를 사용해 가상 잔고를 업데이트합니다. SQLite 원장을 이용해 동일 날짜의 중복 기록을 방지합니다. Upbit는 시세 신선도를 검사하고 KIS는 거래일·시간 창을 제한합니다. 시세 조회를 제외하고 **실제 주문 API는 사용하지 않습니다.**

기본 설정은 종목별 100만 가상 화폐단위(KRW/USD를 합산하지 않음), 최대 편입 20%, 모의 일일 손실 한도 3%입니다. 단일 종목 모의계좌의 결과를 다중 통화·다중 자산 포트폴리오 수익률로 해석하면 안 됩니다.

## 개발·검증 제한

- 백테스트는 일봉 next-open 한정입니다. 미체결, 호가 스프레드, 시장 충격, 세금·환전비용, 기업행동, 상장폐지 편향 등은 충실하게 모델링되지 않았습니다.
- 기본 수익성 통과 기준(3회 주문, Sharpe 0.25 등)은 **개발용**이므로 실거래 승인 기준으로 사용할 수 없습니다.
- 원본 RD-Agent와 TradingAgents의 실행 코드는 현재 없습니다.
- 실제 모델 서버 및 Upbit/KIS/Stooq 엔드포인트 연동 결과는 아직 기록되지 않았습니다. 테스트는 합성 데이터와 모의 HTTP 응답만 사용합니다.
- 모의투자 결과가 좋아도 실계좌 전환은 자동 허용되지 않습니다. 실거래 기능을 추가하려면 거래소·증권사별 멱등 주문, 잔고/체결 대사, 포트폴리오 위험한도, emergency kill-switch가 필요합니다.

참고 연구: [Microsoft RD-Agent](https://github.com/microsoft/RD-Agent), [Qlib](https://github.com/microsoft/qlib), [TradingAgents](https://github.com/TauricResearch/TradingAgents).

라이선스: MIT (시장 데이터는 각 공급처 약관에 따름).

## Qlib 실제 학습과 국내주식 장기 검증 (2026-10-20)

현재 `quantlab/qlib_bridge.py`는 실제 Microsoft Qlib의 `qlib.contrib.model.gbdt.LGBModel`을 **학습 및 예측**에 사용합니다. 단일 종목 OHLCV를 원인-시간순 인메모리 데이터셋으로 변환하므로, Qlib의 공식 Alpha158·Alpha360 **다종목 팩터 벤치마크를 재현한 것은 아닙니다**.

```bash
pip install -e '.[dev,ml,qlib]'
python -m quantlab --config examples/market_research_long.json qlib-compare --symbol KRW-BTC
python -m quantlab --config examples/market_research_long.json qlib-compare --symbol KRW-ETH
python -m quantlab --config examples/market_research_long.json qlib-compare --symbol 005930
```

- 무료 한국주식 연구 데이터는 Naver 금융 차트의 **비공식 공개 조회 API**를 사용합니다. 증권사 정식 데이터 계약·기업행동 보정·운영 안정성은 검증되지 않았으며 연구용 전용입니다. 실거래 또는 호가 조회에 사용하지 않습니다.
- 공식 KIS API와 Stooq/Yahoo 수집기는 별도입니다. WSL에서 Stooq와 Yahoo 미국주식 접속은 지속적인 차단/429가 있어 안정적인 데이터원으로 간주하지 않습니다.
- Qlib 결과는 `reports/*__qlib_compare.json`, 재현용 원본 OHLCV는 `reports/snapshots/`에 저장합니다. SHA-256 해시가 보고서에 포함됩니다. **GitHub에는 시세 CSV·실험 DB를 커밋하지 않습니다.**
- 네 개의 비중첩 최종 평가 구간과 2배 수수료 스트레스 테스트를 제공합니다. 모델을 비교해 최종 테스트 성과를 재사용했다면 새로운 미래 데이터에서 다시 평가해야 합니다.
- Qlib 벤치마크는 실거래뿐 아니라 자동 선행 모의거래 **승인 연결도 비활성화**되어 있습니다.

자세한 재현/한계: [Qlib 및 시장자료 검증 안내](docs/QLIB_AND_MARKET_RESEARCH.md).

## Strategy robustness diagnostics (2026-10-20)

`agent-compare` and `qlib-compare` now report a **research-only** risk/robustness appendix:
- Train-only removal of nearly constant or >=98%-matching Agent factors; validation and held-out prices never affect this screening.
- Causally dated bullish/bearish/sideways and high/low-volatility regime attribution using only historical completed closes.
- Fold-stratified 10-bar block bootstrap of daily OOS log-relative returns versus same-capital-exposure buy/hold. **Exploratory confidence ranges are not multiple-testing corrected and are NOT probabilities of future profitability.**
- Additional C-vs-risk-matched buy/hold comparison prevents treating cash exposure reductions as free strategy alpha.
- These results can **never** turn on automatic paper promotion or live broker execution. Strategy changes after inspecting OOS require a completely new future evaluation period.

[Full methodology and limitations](docs/ROBUSTNESS_DIAGNOSTICS.md).


## Automated forward-only Shadow Paper incubation (2026-10-20)

**Research-only tracking, no live broker order APIs.** BTC and ETH can be observed across three immutable virtual strategies each. Every strategy starts from an independent imaginary KRW account: 20% max exposure and a persistent virtual daily-loss stop. Previously completed history is **never backfilled as if traded**; first execution requires a newly closed Upbit UTC session after registration. Enabling the timer does not activate trading with real money.

```bash
python -m quantlab --config examples/shadow_crypto.json --db state/shadow.db shadow-bootstrap
python -m quantlab --config examples/shadow_crypto.json --db state/shadow.db shadow-cycle
python -m quantlab --db state/shadow.db shadow-status
```

The systemd **user** timer in `deploy/systemd/` checks at 09:15/09:25/09:35/10:00 **Asia/Seoul** to allow retries after transient public-data failures. Idempotent SQLite events plus OS file locking prevent duplicate virtual fills. This module is kept separate from the legacy `paper.db` and from Qlib/Agent automatic promotion.

[Setup, monitoring, safety and limitations](docs/FORWARD_SHADOW_PAPER.md).

## Local Read-Only Monitoring Dashboard (2026-10-20)

The `Shadow Paper` service now has an **on-device, localhost-only monitoring dashboard**. It displays BTC/ETH strategy-level virtual equity curves, observed (not backfilled) returns/drawdown, paper fills, last-run health and anomalies such as stale/missing daily observations, failed jobs and virtual drawdown halts.

```bash
python -m quantlab.dashboard --db state/shadow.db --bind 127.0.0.1 --port 8788
```

Open **http://localhost:8788** on the same host (with WSL localhost forwarding where applicable). It never places orders, never changes balances/strategies and cannot authorize live trading. UI/API bind to loopback only; without authentication, never expose to 0.0.0.0 or the public internet.

[Deployment, systemd auto-start and anomaly-check reference](docs/MONITOR_DASHBOARD.md).


## Automated daily reports and anomaly alerts (2026-10-20)

The read-only watchdog now generates daily **forward-only observed** paper performance reports per independent BTC/ETH virtual account, detects missing daily observations, service/read failures and risk halts, and records deduplicated issue/resolution events in a separate SQLite alert inbox. **It never places broker orders or automatically promotes a strategy.**

```bash
python -m quantlab.watchdog --shadow-db state/shadow.db --state-db state/watchdog.db --report-dir reports/forward
```

Reports: `reports/forward/YYYY-MM-DD.json` and `.md`. Dashboard: local-only `http://localhost:8788` with recent daily report and alert history.

A WSL user `freequant-watchdog.timer` template checks at 10:30, 12:30 and 18:30 KST, after the forward-paper runner's 09:15–10:00 checks. On real new alert transitions it may invoke the Windows PowerShell Toast API; **successful API submission does not confirm a visible desktop notification**. Alert events remain available locally even when Windows notification delivery fails.

[Operations runbook and delivery limitations](docs/WATCHDOG_AND_FORWARD_REPORTS.md).


## Strict market data integrity and conservative research review (2026-10-20)

The BTC/ETH forward-only Shadow Paper runner now performs **fail-closed public OHLCV and quote integrity checks before recording each future virtual fill**: exact completed UTC day, 100 consecutive recent daily candles without missing/duplicate sessions, valid finite candle bounds and volume, suspicious daily changes, and public quote sanity relative to the previous close. Raw Upbit duplicate UTC sessions are rejected before canonical normalization.

Every watchdog cycle separately **replays cash/units/fees and account state** from the immutable SQLite paper events, validates frozen-strategy identity and forward event ordering, and blocks new performance reports if the virtual ledger fails reconciliation. No automatic repair is attempted.

Daily reports also show **manual research review readiness**: at least 90 genuine forward observations, no missed observations, at least 8 virtual orders, no virtual risk halt, MDD better than -12%, identical benchmark dates and positive descriptive return advantage. These are *minimum sample checks, not proof of alpha*, and **never authorize strategy auto-promotion or live orders**. Qlib research inputs now have their own date/OHLCV validation (weekend gaps allowed for stocks).

[Detailed validation and safety criteria](docs/DATA_LEDGER_QUALITY.md).


## Historical multiple-testing sensitivity and preregistered future observation (2026-10-20)

`agent-compare` and `qlib-compare` now run **joint moving-block max-T sensitivity** alongside individual bootstrap: same 10-day time blocks are resampled across the complete *reported* A/B/C or Qlib candidate family, so correlated model results cannot be counted as independent wins. Existing past OOS was repeatedly inspected; these are **NOT confirmatory p-values** and do not establish investment alpha.

The current six frozen BTC/ETH paper strategies are now listed in a SHA-256 fingerprinted, Git-versioned **90-day future protocol**:
`protocols/2026-10-20-btc-eth-90d.json`. Predeclared decision days are `2026-10-21..2027-01-18 UTC`; inspect results only after `2027-01-19T00:00Z`.

```bash
.venv/bin/python -m quantlab --db state/shadow.db protocol-inspect \
  --protocol protocols/2026-10-20-btc-eth-90d.json
```

The protocol cannot be overwritten by the CLI and will not reveal window return scores before the deadline. **The normal dashboard still shows interim performance, so it is a preregistration, NOT a genuinely blinded holdout.** Complete records still require manual interpretation and cannot trigger real orders or strategy auto-promotion.

[Full methodology, pitfalls and limitations](docs/PROSPECTIVE_MULTIPLE_TESTING.md).
