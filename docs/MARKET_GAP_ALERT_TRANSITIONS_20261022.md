# L2·WebSocket 공백의 독립 경고 및 해제 이력 — 2026-10-22

## 범위

이 도구는 WSL 종료 시점 등에서 발생한 실제 공개 시장 관측 공백을 전용 경고 원장에 기록합니다. 과거 주문장/체결 데이터를 복원하거나 생성하지 않으며, 기존 독립 L2·WebSocket 감사의 24시간 품질 기준을 낮추지 않습니다.

새 Python 명령: python -m quantlab.market_gap_alerts

경고 SQLite: /home/hong/apps/FreeQuantLab/state/market-gap-alerts.db

읽기용 이력: /home/hong/apps/FreeQuantLab/reports/market-quality/gap-alert-history.json

L2 observation의 due 슬롯 및 WebSocket BOUNDED_CAPTURE_COMPLETE 세션만 성공 슬롯으로 집계합니다. 양쪽 소스를 모두 읽은 다음 경고 전환을 기록하며, 읽기 실패 시 이전 활성 경고를 해제하지 않습니다. 한 시장당 중복 경고 방지, 심각도 변경, 마지막 24시간 창에서 결측이 사라지면 해제 이벤트를 남깁니다.

90% 미만 슬롯 커버리지 또는 연속 누락 3개 초과는 critical로, 그 외 적은 결측은 warning으로 표시합니다. 144/144 슬롯이 모두 존재해도 원본 SHA, 거래소 timestamp, 거래 인과성, 실제 내 주문 체결 품질은 여전히 별도 감사가 필요합니다. 성공 상태는 CAPTURE_SLOTS_PRESENT_QUALITY_NOT_YET_CERTIFIED라고 명시합니다.

## 독립 자동 일정

기존 품질 감사 L2 :05/:35, WebSocket :07/:37 이후 신규 모니터 :09/:39로 실행하도록 구성했습니다. 새 서비스는 원본 DB만 SQLite mode=ro로 읽고, 전용 이력 DB와 JSON에만 기록합니다. Windows Toast는 기본 비활성이며 실제 화면에 표시됐다고 주장하지 않습니다.

CI 통과 후 실제 WSL이 온라인일 때 수동 진단을 실행해 원장 불변성과 기존 서비스 영향을 확인한 뒤 독립 신규 타이머만 활성화해야 합니다. 현 청정 90일 코호트/전략/수집/감사/백업 타이머와 실주문·자동 모델 승격 설정은 수정하지 않습니다.

## 검증 완료 기준

단순한 슬롯 공백 해제와 24시간 원본·신선도 품질검사 PASS는 구별해야 합니다. Issue #63의 최종 완료를 위해서는 원격 수집 운영의 장시간 중단 원인 예방, 완성된 신규 24시간 샘플과 기존 L2/WS 품질 게이트 통과 확인이 추가로 필요합니다. Windows가 꺼지면 WSL은 시장관측을 수행할 수 없습니다.
