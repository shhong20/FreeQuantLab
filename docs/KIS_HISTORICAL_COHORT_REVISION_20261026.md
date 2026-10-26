# KIS 과거 분봉의 소급 변경 감시: 삼성전자 2026-10-23 실측 (2026-10-26)

## 문제

FreeQuantLab은 삼성전자 `005930` 2026-10-23 KIS 역사 분봉을 한 차례 수집하여 `J`(KRX), `NX`(NXT), `UN`(통합) **각각의 불변 원본 스냅샷**으로 보존했다. 그 뒤 `UN`을 독립 재조회했을 때 `output2`는 동일했고, 원본 바이트 해시만 시점별 현재가 등을 포함한 `output1` 차이로 변경됐다.

그러나 그 후 2종목 × 3거래일 × 3시장 **18개 독립 시장 소스**를 재수집하자, 같은 삼성전자·같은 과거 날짜의 역사 분봉 `output2` 값도 일부 달라졌다. 이는 시장 간 합계 이상과 구분해야 할 **과거 값 소급 변경(이전·신규 관측값의 차이)**이다.

## 실제 WSL 저장본 간 독립 비교

원본: `/home/hong/apps/FreeQuantLab/state/intraday_research/venues/`

새로 수집한 코호트: `/home/hong/apps/FreeQuantLab/state/intraday_research/venues_cohort_20261026_run1/`

| 시장 | 시각 | 기존 `cntg_vol` | 신규 | 변화 |
|---|---|---:|---:|---:|
| KRX (`J`) | 09:07 | 102,331 | 102,262 | −69 |
| KRX (`J`) | 09:08 | 157,838 | 157,907 | +69 |
| 통합 (`UN`) | 09:08 | 406 | 407 | +1 |
| NXT (`NX`) | 전체 | 동일 | 동일 | 0 |

- KRX 해당 두 분봉 수량 합은 변화 없음. KRX·NXT·통합 **공식 일봉 `output2`도 동일**하므로 일봉 수량은 그대로다.
- 기존과 신규의 다른 시각 분봉은 모두 일치함. 바뀐 **정규화 분봉 라벨은 총 3개**.
- 이전 `UN`의 09:08 이상 거래량 406주는 신규 407주로 1주만 바뀌었고, 여전히 신규 KRX 157,907 + NXT 226,919 = 384,826주와는 **384,419주 불일치**. 09:07은 신규 KRX 102,262 + NXT 41,781 = 144,043주 vs UN 143,952주 (−91).
- 두 불일치의 합은 **−384,510주**. 이전 **−384,511주**와 달라져도 09:08 집중 현상이 지속된다.
- `output1`의 조회 시점 시세값 차이만 있었던 최초 재조회와 달리 이번에는 **`output2` 실제 과거 분봉 내용의 차이**가 있으므로 `REPLAY_OBSERVATIONS_CHANGED_HOLD`가 정당하다.

이 결과를 제공사 오류라고 단정하지 않는다. 시간 경계·정정 체결·서로 다른 시각 정의·소급 시세 정리 등 원인은 KIS 공식 설명 및 독립 체결 자료로 검증해야 한다. 임의로 분봉을 예전/새 값으로 덮어쓰거나 통합 분봉을 J+NX 합으로 고치지 않는다.

## 자동 소급변경 코호트 검사기

`quantlab.intraday_kis_cohort_revisions`는 비밀키·인터넷·주문 기능을 전혀 사용하지 않는다. 독립된 두 수집 루트의 J/NX/UN **원본 해시와 저장된 1분 OHLCV, 공식 일봉 output2**를 검사하고, 완전하게 확보된 **이전/신규 시장 3개 자료가 모두 있는 조합만** 변화율 분모로 계산한다.

```bash
cd /home/hong/apps/FreeQuantLab-intraday-20261026
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
ROOT=/home/hong/apps/FreeQuantLab/state/intraday_research

"$PY" -m quantlab.intraday_kis_cohort_revisions \
  --baseline-root "$ROOT/venues" \
  --newer-root "$ROOT/venues_cohort_20261026_run1" \
  --symbols 005930,000660 \
  --sessions 2026-10-21,2026-10-22,2026-10-23 \
  --out-dir "$ROOT/cohort_revision_crossrun_20261026_r1"
```

이미 **실제 WSL 자료에 실행했고 결과를 별도 불변 저장**했다.

```text
status = HISTORICAL_SOURCE_REVISION_HOLD
complete_pairs = 1
missing_capture_pairs = 5
revision_affected_pairs = 1
changed_minute_labels_across_venues = 3
J=2, NX=0, UN=1
usable_for_backtesting = false
```

결과: `/home/hong/apps/FreeQuantLab/state/intraday_research/cohort_revision_crossrun_20261026_r1/cohort_revision_summary.json` 및 `cohort_revision_summary.sha256`.

**5개 미비교 종목일 쌍은 안정적이라고 간주하지 않는다.** 기존에 최초 10/23 삼성전자 자료만 있었기 때문에 전체 6건 중 1건만 소급 비교할 수 있었다.

## 다음 단계

1. **시장별 분봉 시간라벨 `stck_cntg_hour`의 정확한 창 정의**와 체결 취소·정정 시점 반영 여부를 KIS에 확인해야 한다.
2. 이후 다른 종목일의 독립 수집본을 별도 경로에 추가 확보하면 같은 코호트 명령으로 **수정 분봉 빈도와 거래시간별 분포**를 측정할 수 있다. 기존 결과를 재사용하거나 덮어쓰지 않는다.
3. 앞선 코호트의 일봉 대비 분봉 거래량 차이(모든 6개 종목일)에 대해 KRX/NXT 종료시점 집계, 장후 거래, 분봉 시간 의미를 공식 자료로 비교해야 한다.
4. **공식 데이터 의미 확인과 시간순 데이터 정확도 검증 전까지** 모든 출력은 `usable_for_backtesting=false`이며, 기존 가상계좌·90일 평가·실주문 환경으로 자동 승격하지 않는다.
