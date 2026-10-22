# 가상 위험정지(HALT) 사후 보유 위험 확인 — 기존 90일 연구 무변경

## 검증 결론

현재 `quantlab/shadow.py`는 당일 평가액이 직전 기록 대비 사전 등록된 최대 가상 손실을 넘으면 `HALT` 이벤트를 쓰고 계좌 `halted=1`로 고정한다. **기존에 보유 중인 가상 코인은 매도하지 않고**, 이후 그 계좌는 신규 일별 이벤트 대상에서 제외된다. 따라서 마지막으로 저장된 `last_equity`를 이후 시점의 수익률·실제 청산가능 가치라고 부르는 것은 부정확하다.

본 수정은 과거 손익·거래전략이나 2026-10-22~2027-01-19 UTC의 90일 청정 코호트 거래 규칙을 변경하지 않는다.

## 실제 운용 확인 명령 (읽기 전용)

```bash
cd /home/hong/apps/FreeQuantLab-clean-runtime
/home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.halt_exposure
/home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.halt_exposure \
    --db /home/hong/apps/FreeQuantLab/state/clean_shadow_90d.db
```

반환값은 해당 계좌의 마지막 `HALT` 거래시각·보유 수량·마지막 기록 잔고 및 평가액을 보여주되, **모두 역사적 기록**으로 명시한다. 신규 현재 호가를 조회하지 않으며 매도·가상 주문·세금/슬리피지 추정·중간 평가액 쓰기를 수행하지 않는다. HALT와 대응 이벤트 불일치/비정상 잔고 발견 시 읽기 전용 감사 자체를 거부한다.

## 다음 실험 버전 정책 선등록 항목

- 위험 중단은 `ENTRY_BLOCKED_ONLY`, `EXIT_AT_NEXT_OBSERVABLE_PUBLIC_PROXY`, `FULL_LIQUIDATION_SIMULATED` 등 구체적인 사전 선택 정책으로 구분할 것
- 이미 발생한 HALT 이후 결측된 시장 관측의 손익·수익률을 정상 관측으로 간주하지 않을 것
- 공개 Upbit last-traded ticker는 실행가능 bid/ask 또는 실제 체결 보증이 아니므로 각주와 총 비용 가정을 고정할 것
- 다음 평가 코호트를 별도 SHA/계좌/시작일로 등록하여 기존 90일 비교 계좌는 변경하지 않을 것

테스트는 실제 주문이 아닌 합성 SQLite에서 가격 급락 후 HALT·미청산 보유를 재현하며, 현행 원장에는 어떠한 데이터도 쓰지 않는다.
