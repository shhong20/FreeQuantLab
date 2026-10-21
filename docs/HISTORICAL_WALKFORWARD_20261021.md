# BTC/ETH 과거 실거래소 일봉 스트레스 테스트 — 2026-10-21

**결론:** 수익 검증이 완료된 AI 알파 전략은 아직 없다. 과거 데이터를 광범위하게 검증하는 편이 새 90일 평가 코호트를 만드는 것보다 현 개발 단계에서 우선이다. 이 결과를 실제 투자나 자동 승격 근거로 사용하지 않는다.

## 관측 근거

- Upbit API를 사용해 BTC·ETH의 실거래소 UTC 일봉 각 3,000개 다운로드: **2018-08-04~2026-10-20** (미완성 봉 제외).
- 현지 읽기 전용 `state/market_history.db`와 겹치는 최근 **800일 × 2종목 OHLCV 전 항목 일치**.
- CSV와 SHA-256은 서버 내 `/home/hong/apps/FreeQuantLab-historical-20261021/reports/historical-stress-20261021/`에 보관. 기존 거래 원장에 어떠한 가상 체결도 기록하지 않았다.
- BTC 입력 SHA-256: `1b6f4d34ef10f35609dda1d3257de051ffe1545003e00e4d3d1717308d5e2aff`.
- ETH 입력 SHA-256: `495f6afb79222dd7e1b660c2963f6aca96c741a527672a510a0`.
- 해시를 검증한 로컬 CSV로 **네트워크 없이 재실행했을 때 전략별 요약과 다중 비교 진단 완전히 동일함을 확인**했다.

## 검증 설정

- 처음 **600일은 학습용**. 이후 **2020-03-26~2026-10-18 UTC** 20개씩, 종목별 총 **2,398개 비중첩 OOS 의사결정일**.
- 한 구간은 최대 120일. 모델은 매 구간 시작 전에 사용 가능한 정보만으로 학습하며 이후 120일 동안 다시 학습하지 않는다.
- `quantlab.engine.simulate` 및 `quantlab.diagnostics.daily_net_returns` 결과가 일치함을 확인.
- 총 **6전략 × 3거래비용 × 20구간 × 2종목 = 720개 구간별 평가 시나리오**. 720개의 독립 실험이 아님.
- 전략: 동일 최대 20% 노출 매수·보유, MA20/80, MA10/40, 5일 수익률 양수 팩터, logistic p≥0.60, 재학습 LightGBM p≥0.55.
- 후자의 LightGBM은 **기존 운영 Qlib 모델의 재현 실험이 아니고**, 로컬 Exaone LLM의 과거 시점별 의사결정도 이 평가에 포함되지 않는다. 이 사실을 과대포장하지 말 것.
- 비용 15 / 30 / 60bp는 **매수·매도 한쪽 거래대금에 적용되는 가정 수수료+슬리피지**이며, 대량 주문 시장 충격, 체결 호가, 펀딩, 세금, 자금 제약은 평가하지 않았다.
- 각 OOS 구간을 독립적으로 시작·종료해 매 구간 말의 강제청산 비용을 반영했다. 누적 수치는 **가상의 구간 수익률을 이어 곱한 것**이며 실제 한 계좌의 연속 운용 성적이 아니다.
- 기존 연구에서 이미 여러 구간을 열람·선택했으므로 완전히 깨끗한 미관측 홀드아웃이라는 주장은 하지 않는다.

## 가상 구간 누적수익률 (20% 최대 노출, 소수 첫째자리 반올림)

| 전략 | BTC 15bp | BTC 30bp | BTC 60bp | ETH 15bp | ETH 30bp | ETH 60bp |
|---|---:|---:|---:|---:|---:|---:|
| 매수·보유 20% | +88.7% | +86.4% | +82.0% | +128.1% | +125.4% | +120.0% |
| MA20/80 | +71.6% | +68.5% | +62.5% | +116.5% | +113.0% | +106.2% |
| MA10/40 | +85.3% | +81.0% | +72.7% | +147.3% | +141.4% | +130.1% |
| 팩터 r5>0 | +71.6% | +47.5% | +9.0% | +65.8% | +41.9% | +4.0% |
| Logistic p≥0.60 | +9.5% | +6.0% | -0.7% | +14.6% | +6.8% | -7.2% |
| LightGBM p≥0.55 | +37.3% | +8.2% | -32.9% | +36.6% | +5.0% | -38.1% |

## 해석

1. 대부분의 전략은 동일 노출 매수·보유보다 낮은 결과. `LightGBM`의 비용 15bp 기준 20개 OOS 구간 중 비교우위 비율은 BTC 20%, ETH 35%다. 거래량과 비용에 특히 민감하다.
2. ETH MA10/40의 +147.3%는 매수·보유 +128.1%를 넘었으나, 매수·보유보다 나은 폴드는 20개 중 **8개(40%)** 뿐이다. 단일 시나리오의 눈에 띄는 수치를 승리 신호로 선택하면 과최적화된다.
3. 후보군 5개에 대해 동일 인덱스 공동 블록 부트스트랩을 적용한 ETH MA10/40의 familywise maxT 탐색적 꼬리 지표는 **약 0.618**. 이전 시도까지 포괄하지 않아 **확증적 p-value로 해석하면 안 된다**.
4. 고비용 60bp에서 LightGBM은 BTC -32.9%, ETH -38.1%. 현 상태의 ML 신호 품질·회전율·거래 비용에 대한 개선 없이는 자동화 확대가 정당화되지 않는다.
5. 시장 국면 다양성 개선을 위해 2018년 이후 데이터 확보 효과가 컸으나, BTC·ETH 두 자산만으로 시장 일반화 가능성을 말할 수 없다.

## 재현

```bash
cd /home/hong/apps/FreeQuantLab-historical-20261021
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python -m pytest -q

# 최초 실거래소 공개 과거 데이터 조회 (새로운 현 시점 구간으로 바뀜)
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.historical_stress \
  --archive /home/hong/apps/FreeQuantLab/state/market_history.db \
  --report-dir reports/NEW_RUN_UNIQUE_NAME

# 이미 보관한 정확한 2026-10-20 종료 CSV로 네트워크 없이 재평가
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.historical_stress \
  --snapshot-dir reports/historical-stress-20261021 \
  --archive /home/hong/apps/FreeQuantLab/state/market_history.db \
  --report-dir reports/NEW_REPLAY_NAME
```

최종 보고서는 `results.json`, 원본 입력은 `KRW_BTC.csv`·`KRW_ETH.csv`이며, 잘못된 원본 해시 또는 현지 보존 데이터와 불일치한 가격 기록은 fail-closed로 차단한다.

## 운영 변경

새 독립 90일 코호트의 `freequant-clean-shadow.timer`와 `freequant-clean-watchdog.timer`는 사용자 요구에 따라 2026-10-21에 **disabled/inactive** 처리했다. 가상계좌 원장/프로토콜은 보존했다. 기존 원본 기본 전략, Exaone, Qlib 서비스는 이번 연구에서 조작하지 않았다. 실시간 모의투자를 새로운 후보 선정의 대체 수단으로 삼지 않고, 연구가 충분히 정리된 이후 추가 현실성 검증용으로 활용한다.
