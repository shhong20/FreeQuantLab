# KRX 2026-10-29 장중 3종 시세 수집 실패 증거·재요청 차단 (2026-10-28)

## 검토 결과
10월 29일 09:43/12:35/14:35 KST 각 2개 종목 × 3개 시장 데이터 × 2회(총 구간당 12 GET)를 수행하는 기존 연구 파일럿은 안전하게 **완성된 원본 세트만 불변 폴더로 승격**하도록 돼 있다. 다만 OAuth 토큰 발급 또는 KIS API 응답이 중간에 실패하면 임시 데이터가 폐기되고, 최종 감사의 누락 상태 `NOT_CAPTURED`만으로는 실행 자체가 없었는지 요청 실패인지 구별하기 어려웠다.

## 수정
- `quantlab.intraday_krx_pilot_failure`에 **실패 영수증** `2026-10-29_{morning|midday|afternoon}_failure.json/.sha256` 추가.
- 사용자 전용 파일(0600)과 SHA-256 체크섬을 기록. 불변 파일이 이미 있으면 다시 덮어쓰지 않음.
- 지정된 한 거래일(2026-10-29 KST)에서만 작성. `failed_phase=AUTH|MARKET_READ|AUDIT` 등 허용값, 직전 검증된 0~12 응답 수, 직전 요청한 종목(005930/000660), 종류(book/ticks/minute), 0/1회차, 시각만 저장.
- **KIS 앱키, 앱시크리트, bearer token, 전체 응답 원문, 서버 오류 본문, traceback, 임의 예외 메시지는 저장하지 않음.** 오류 원인에 대한 KIS 공급사 세부 코드까지 추정하지 않으며 단계별 현상만 기록한다.
- KIS 수집기는 요청 시작 시점 `STARTED`와 원본 검증 성공 후 `VERIFIED`의 안전한 내부 신호만 발행. 요청 중단 시 어느 단계에서 몇 건까지 검증됐는지 조사 가능.
- 이미 실패 증거가 있는데 불변 원본 자체는 없는 경우, 다음 실행은 인증·시세 조회를 **자동 재요청하지 않고 실패**. 완료되지 않은 시장 데이터를 임의 보간하거나 임의 성공으로 표시하지 않음.
- 원본은 이미 저장됐지만 별도 오프라인 감사에 실패한 경우, 기존 원본으로 네트워크 없이 감사 가능. 최초 실패 영수증은 역사적 사고 기록으로 유지.
- 마감 독립 검사는 `MARKET_CAPTURE_FAILED_RECORDED`와 안전한 검증 완료 응답 수·오류 단계도 최종 보고서에 포함. 해시 변조나 허용되지 않은 문서 필드는 `EVIDENCE_FAILED_VERIFICATION`으로 분류.
- 시장 체결틱은 **거래소 시장 데이터**이며 계좌 개인 주문 영수증이 아님. 실주문 0, 확정 포지션/수익률 없음.

## 예약 연결
- 실 WSL cron의 총 5개 작업(오전·정오·오후, 15:45 최종 감사, 정시 사후 감사)은 그대로 유지하되, 새 실패 영수증을 사용하는 작업트리 경로로 정확히 교체.
- 사용자 `hong`의 다른 cron은 변경 금지.
- 15:45 이후에는 3종 시세 원본 SHA 재검증 → 동적 호가 비용 민감도 → 최종 마감 및 **정확히 해당 파일럿 5개 예약 삭제**.
- WSL 중단 중 실제 시장 스냅샷은 복구할 수 없음. 재시동 후에는 2026-10-29 원본만 오프라인 감사하고 누락 상태 및 실패 영수증을 합쳐 기록.
- 기존 운영용 90일 평가 `d6b531e` 및 브로커 주문 경로는 전혀 변경하지 않음.

## 재현 경로
```bash
cd /home/hong/apps/FreeQuantLab-krx-failure-20261028
/home/hong/apps/FreeQuantLab/.venv/bin/python -m pytest -q \
  tests/test_intraday_krx_pilot_failure.py \
  tests/test_intraday_krx_joint_pilot.py \
  tests/test_intraday_krx_joint_pilot_review.py
```

최종 데이터 경로:
`/home/hong/apps/FreeQuantLab/state/intraday_research/krx_joint_pilot_20261029_r1/`

예상된 실제 실패 파일은 해당 거래일의 **실제 예약이 오류를 겪을 경우에만** 생성된다. 현재 테스트에서 만든 합성 실패 영수증은 임시 테스트 폴더만 사용하며 운영 연구 경로에 섞지 않는다.
