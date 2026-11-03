# KODEX 200 버전형 가격 후보 데이터셋 V1 — 2026-11-03

## 목적과 범위

PR #123이 실제 삼성자산운용의 2026-10-27 공지(`069500 KODEX 200`, 좌당 현금 분배금 136원)와 10월 28일 최초 관측 종가 95,650원으로 가정한 조정계수 `(95650 - 136)/95650=0.9985781495033978`가 2026-10-22, 26, 27, 28의 서로 다른 공급사 OHLC 16개를 각각 **1원 이내**로 설명하는 것을 확인했다.

이 **정합성이 시장가격 인증이나 공급사 수정주가 산식 확인은 아니다.** 기존 최초 관측 가격, 나중 공급사의 수정주가처럼 보이는 가격, 공시 기반 가설 가격을 서로 바꿔 쓰지 않고, 각각 이름 붙인 원본 추적 가능한 별도 연구 데이터셋으로 저장한다.

공식 1차 출처:
- 분배금 136원 공지 (2026-10-27): https://www.samsungfund.com/etf/lounge/notice-view.do?no=80553
- ETF 지급 기준일 관행: https://www.samsungfund.com/etf/product/view.do?id=2ETF01
- **10월 29일 분배락일은 연구 가정**이며, 별도 거래소 분배락 이벤트 데이터와 수정주가 규약을 아직 재증명하지 못했다.

## 독립 보존되는 세 가지 시나리오

`etf_variants.jsonl` 4개 레코드 × 3가지 OHLC 가격 후보:

1. `original_first_observed`: 최초 관측 DB의 수정 전으로 보이는 OHLC — 당시 실제 체결가격으로 인증되지 않음.
2. `latest_vendor_archived`: 원본 SHA가 확인된 나중 공급사의 수정된 것으로 보이는 OHLC — 거래일 기준 원시 체결가격으로 인증되지 않음.
3. `issuer_cash_distribution_ratio_hypothesis`: 공식 현금 136원과 최초 관측 10월 28일 종가 95,650원으로 산술 계산한 연구 가격 — **가설**, 공급사 공식 산식 아님.

각 레코드는 날짜, 종목코드, 거래소, 공급사 원본 SHA, 종목당 세 후보 OHLC, 가격 잔차, 조정계수, 분배금, 기존 관측 증거 동의 여부, `canonical_trade_price_selected=false` 및 모델/백테스트/실거래 모두 `false` 플래그를 포함한다. 거래량 충돌을 가설로 해결하지 않고, 가격 후보와 거래량은 의도적으로 분리한다.

`unresolved_us.jsonl` 5개는 AAPL·MSFT·SPY의 거래일·출처 SHA·충돌 분류만 담는다. **ETF 136원 조정계수는 미국 주식에 적용하지 않는다.** 임의의 '최종 수정가격'도 생성하지 않는다.

## 산출물 및 계보

실제 WSL에 저장한 불변 V1 연구 데이터:

```text
/home/hong/apps/FreeQuantLab/state/stock_daily/research_variants/
  kodex200_distribution_v1_20261103/
    manifest.json                 # 불변 부모 출처 해시와 상태·권한
    manifest.sha256               # manifest 원본 SHA
    etf_variants.jsonl            # 4 ETF 세션 × 3 별도 가격 후보
    unresolved_us.jsonl           # 미국 5개 미확인 일자
```

부모 출처:
- `quality_reports/gap_provenance_20261103_r2.json(.sha256)` — 주식 관측/장기 이력의 **독립 원본 SHA 검증**
- `quality_reports/etf_distribution_study_20261103_r1/study.json(.sha256)` — 공식 분배금 연구 결과

데이터 생성 또는 독립 재검증 전에 **두 주식 SQLite DB를 `mode=ro`로 열어 전체 결측 목록과 원본 공급사 값을 다시 검사**한 후 부모 보고서 SHA 및 내용까지 일치시킨다. 출처 변경, 출력 데이터 변조, 거짓 모델 승인, 기존 출력 덮어쓰기, 중복 레코드, 근거가 맞지 않는 가설 계수는 실패로 차단한다.

연구 폴더 0700, 네 파일 0600. 저장소의 실제 `long_bars`/ `daily_bars`, 기존 가상매매 장부, Qlib 모델, 신규 주문은 전혀 수정하지 않는다.

## 검증·사용 명령

```bash
cd /home/hong/apps/FreeQuantLab-observer-runtime
P=/home/hong/apps/FreeQuantLab/.venv/bin/python
R=/home/hong/apps/FreeQuantLab/state/stock_daily/quality_reports
D=/home/hong/apps/FreeQuantLab/state/stock_daily/research_variants/kodex200_distribution_v1_20261103

$P -m quantlab.stock_price_variant_dataset \
  --verify-only \
  --study-dir "$R/etf_distribution_study_20261103_r1" \
  --gap-report "$R/gap_provenance_20261103_r2.json" \
  --output-dir "$D"
```

출력 상태 `SOURCE_PINNED_PRICE_VARIANTS_RESEARCH_QUARANTINED_NOT_BACKTEST_READY`는 **출처 계보가 재현되었다**는 의미만 가진다. 수익률·샤프·유효 체결률 또는 매매 가능한 가격 인증을 의미하지 않는다.

## 다음 정규화 단계의 승인 조건
- 실제 거래소/운용사 분배락일·수정주가 적용 범위 공식 증거
- NAV/ETF 분배금 처리와 공급사 일봉 수정주가의 원시 규칙 독립 검증
- 미국 주식 AAPL/MSFT/SPY는 별도 거래소 일별 가격 기준으로 독립 검증
- 서로 다른 가격 기준을 혼용하지 않는 완전히 새로운 버전형 연구 데이터셋, 사전에 정의된 미래 데이터 평가
- 실거래나 모의주문 승인 여부는 별도의 계좌 체결·위험 통제 증거로 판단
