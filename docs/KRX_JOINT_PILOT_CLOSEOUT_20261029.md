# 2026-10-29 KRX 연구용 3종 시세 파일럿 자동 마감 감사

전제: PR #111의 2026-10-29 09:43/12:35/14:35 KST 3개 읽기 전용 KIS 분봉·체결·10호가 수집 예약. 운영용 90일 평가와 주문 라우터는 변경하지 않는다.

## 새 최종 보고서
- 2026-10-29 15:45 KST 이후 전용 마감 작업 실행. WSL/cron이 가동 중이어야 실행된다.
- 세 구간의 저장된 `manifest.json/.sha256`, `joint_comparison.json/.sha256`과 **실제 raw KIS 응답/정규화 시장 데이터 전체의 SHA**를 독립적으로 다시 검사.
- 구간별 **4개** (2종목 × 2회) 3종 시장 시세 세트; 하루 최대 **12세트**, 시장 데이터 원격 GET 총 36회(기존 수집기).
- 종목·회차 중복, 출처, 자료 생성 시각이 실제 해당 날짜·구간에 속하는지, 요청 간 시간이 30초 이내인지 검증.
- 동일 HH:MM 체결틱과 분봉 가격 범위 일치 건수, 종목별 표시 호가 스프레드 bp 중앙값·최솟값·최댓값 기록.
- 원본은 있는데 감사 저장만 실패한 경우 **네트워크 재조회 없이 원본으로 감사 복구**. 원본 자체가 누락·훼손된 경우에는 복구한 것처럼 처리하지 않고 `NOT_CAPTURED`, `EVIDENCE_FAILED_VERIFICATION`, `RAW_CAPTURED_AUDIT_MISSING` 등으로 명시.
- `ALL_THREE_WINDOWS_VERIFIED_RESEARCH_ONLY` 상태는 원본과 3종 비교의 정합성이지 **가상주문/실주문 체결률 또는 전략 수익률 검증 완료가 아님**. 항상 `broker_orders=0`, `historical_strategy_returns=null`, `paper_or_live_approval=false`.
- 불변 출력: `/home/hong/apps/FreeQuantLab/state/intraday_research/krx_joint_pilot_20261029_r1/2026-10-29_final_review/final_review.json`과 `.sha256`. 감사 결과가 일부 실패여도 요약 보고서는 실패 상태로 남긴다.

## Cron 등록과 자동 종료
- 기존 3개 일정 유지: 09:43, 12:35, 14:35 KST.
- 추가 1회 마감: 2026-10-29 15:45 KST, `ops/krx_joint_pilot_finalize_20261029.sh` 호출.
- 날짜/시각 조건을 검사하고 2026-10-29 15:45 이후에만 종료 보고서 생성/예약 삭제.
- 전용 `ops/krx_pilot_cron_cleanup_20261029.py`는 **정확히 지정한 4개의 예약 줄과 헤더**가 있는지 확인한 후 해당 줄만 제거. 다른 작업을 수정하지 않음. 내년 같은 날짜에 재실행되는 일을 방지.
- 마감 감사에 실패하더라도 만료 예약은 정리한다. 수집 실패를 덮거나 조용히 재시도하지 않는다.
- **WSL 종료 시 cron은 실행할 수 없음.** 재시동 후 과거의 동일 시각 호가를 만들어 보완하지 않고 해당 구간은 관측 실패로 남긴다. 마감작업 자체가 누락되면 예약 정리는 직접 점검해야 한다.

## 확인 명령
```bash
R=/home/hong/apps/FreeQuantLab/state/intraday_research/krx_joint_pilot_20261029_r1
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
cd /home/hong/apps/FreeQuantLab-krx-closeout-20261028
$PY -m quantlab.intraday_krx_joint_pilot_review --preview
crontab -l

# 수집 및 마감 시간이 지난 뒤:
$PY -m quantlab.intraday_krx_joint_pilot_review --preview
cat "$R/2026-10-29_final_review/final_review.json"
```

예약과 전체 테스트가 성공해도 2026-10-29 장중의 실제 데이터 수집 성공은 **10월 29일 이후**에만 판정할 수 있다. 현재 운영 90일 평가 `d6b531e`는 변경하지 않음.
