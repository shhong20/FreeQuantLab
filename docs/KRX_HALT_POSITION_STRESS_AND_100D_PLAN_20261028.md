# KRX 100일 확장 및 서킷브레이커 보유 포지션 위험 점검 — 2026-10-28

## 1. 과거 데이터 확보 범위 확대: 무료 KIS J 100거래일

기존 `quantlab.intraday_krx_backfill`을 변경하지 않고, 실제 WSL에서 아래 **인증 없는 계획**을 새로 생성하고 이전 60일 원본 SHA를 재검증했다.

```bash
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
R=/home/hong/apps/FreeQuantLab/state/intraday_research
"$PY" -m quantlab.intraday_krx_backfill --init \
  --source-root "$R/venues_cohort_20261026_run1" \
  --plan-dir "$R/krx100_backfill_plan_20261028_r1" \
  --end-session 2026-10-23 --session-count 100
"$PY" -m quantlab.intraday_krx_backfill --all-batches \
  --plan-dir "$R/krx100_backfill_plan_20261028_r1"
```

| 항목 | 실제 계획 검사 결과 |
|---|---:|
| XKRX 거래일 후보 범위 | 2026-06-01~2026-10-23 |
| 종목 | 005930, 000660 |
| 목표 | 100일 × 2종목 = 200 KRX J 스냅샷 |
| 기존 해시 검증된 원본 | 120개 |
| 추가 수집 대상 | **80개** |
| 배치 | 최대 5일씩 **20개** |
| 실제 신규 API/주문 | **0회 (DRY RUN)** |

현재 **추가 80개를 실제로 확보했다고 주장할 수 없다**. 로컬 자격증명으로만 실행할 수 있도록 사용자 WSL에서 아래 명령을 사용해야 한다. 새 계획은 원본 폴더·90일 평가를 변경하지 않는다.

```bash
(
  set -e
  cd /home/hong/apps/FreeQuantLab-intraday-20261026
  set -a
  source ~/.config/freequantlab/kis.env
  set +a
  /home/hong/apps/FreeQuantLab/.venv/bin/python \
    -m quantlab.intraday_krx_backfill \
    --all-batches \
    --plan-dir /home/hong/apps/FreeQuantLab/state/intraday_research/krx100_backfill_plan_20261028_r1 \
    --execute
)
```

실행 중 KIS 호출이 실제 과거 6월 데이터까지 제공하는지, 응답 원본과 거래일·380분봉 정합성이 검증되는지는 별개다. 오류는 숨기거나 누락일을 0주로 채우지 않는다. 실행 후에는 새로운 `quantlab.intraday_krx_halt_5m` 오프라인 연구 출력으로 100일 각 거래중단 구간을 재검증할 수 있다.

## 2. 실제 중단 중 보유 주식 위험: 오프라인 정책 리플레이

`quantlab.intraday_krx_halt_position_stress`는 **기존 120개 원본으로 구성한 SHA 검증 완료 `krx60_halt_5m_20261026_r2`**를 읽는다. 가상 보유량 기본값은 10주이지만 **사용자 계좌/실제 주문과 관계없는 스트레스 예제**이다.

- 2026-07-28 10:13:43, 07-29 12:32:32, 10-15 12:38:12 등 보도 기반 CB 시작, 각 20분 중단+10분 단일가매매 *잠정* 시간 창.
- 중단 발생 시 `EXIT_LONG` 의도를 가정하더라도 실제 매도·체결 없음: `filled_shares=0`, `held_shares` 유지.
- 중단 후 자동 재주문·재진입·잔고 처분 **없음**. 시장 개시·체결이 공식 확인되기 전 재평가 필요.
- 가격 비교는 `중단 시작 이전의 마지막 완전 5분봉 종가`와 `잠정 중단 종료 이후의 첫 완전 5분봉 시가`만 사용. **실행 가능한 체결가·실현수익률·샤프·MDD·전략 선택을 계산하지 않음**.
- 기존 원본, 5분봉 출력, DB/90일 평가, 실주문 함수 변경 없음. 모듈이 읽는 원본 요약과 종목별 5분봉 자료의 SHA 검증, 누락 시간 슬롯/코드·사건 날짜 정합성, 주문 금지 게이트를 다시 확인.

### 실제 6개 사례 (2026-10-28 WSL 검증)

| 세션 | 종목 | 마지막 완전 관측 5분봉 종가 | 중단 이후 첫 완전 5분봉 시가 | 단순 가격 비율 변화 |
|---|---|---:|---:|---:|
| 07-28 | 삼성전자 | 230,000 | 231,500 | +0.6522% |
| 07-28 | SK하이닉스 | 1,617,500 | 1,622,000 | +0.2782% |
| 07-29 | 삼성전자 | 202,500 | 201,000 | -0.7407% |
| 07-29 | SK하이닉스 | 1,356,000 | 1,336,000 | -1.4749% |
| 10-15 | 삼성전자 | 209,500 | 209,500 | 0.0000% |
| 10-15 | SK하이닉스 | 1,260,000 | 1,261,000 | +0.0794% |

**위 숫자는 가상 주문 체결 이력이나 실제 계좌 수익률이 아니다.** KIS `stck_cntg_hour`가 분 시작·종료 라벨인지 공식적으로 확인되지 않았고, CB 구간은 언론 기사와 자료 공백의 시간대 일치만 확인했다.

실제 WSL에서 다음 명령을 실행했고 결과 `stress_report.json` 및 `stress_report.sha256`을 저장했다.

```bash
cd /home/hong/apps/FreeQuantLab-intraday-20261026
/home/hong/apps/FreeQuantLab/.venv/bin/python \
  -m quantlab.intraday_krx_halt_position_stress \
  --input-root /home/hong/apps/FreeQuantLab/state/intraday_research/krx60_halt_5m_20261026_r2 \
  --out-dir /home/hong/apps/FreeQuantLab/state/intraday_research/krx60_halt_position_stress_20261028_r1 \
  --assumed-held-shares 10
```

## 3. 전략 백테스터에 연결하기 전에

현재 신규 기능은 **거래중단 대응 정책의 안전성 테스트**이지 완전한 트레이드 체결 시뮬레이터가 아니다.

1. KRX 공식 기계 판독 거래정지·재개·단일가매매 시각 기록 확보.
2. KIS 분봉 시작/끝 라벨 계약 및 15:30/15:32 경계 의미 검증.
3. 보유 포지션이 있는 가운데 CB 이전/이후의 잔고, 미체결 주문, 거래 비용, 갭 위험을 반영하는 실제 point-in-time 상태 모델 설계. 현재 테스트는 모든 출구가 **무체결**로 남는다는 보수적인 정책을 명시할 뿐 실현손익을 산출하지 않는다.
4. 추가 80개 정상 원본 확보와 데이터 품질 감사. **서킷브레이커일을 없애서 수익률·MDD가 좋아 보이게 하는 편향 금지**.
5. 이 전제들이 충족되기 전 `intraday_research.evaluate`의 자동 승격 및 가상/실거래 연결 불허.
