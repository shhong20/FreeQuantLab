# 2026-10-21 | 업비트 실시간 호가·실제 시장체결 공개데이터 검증

## 검증 성격: 새로운 *현시점 견적* 측정 / 과거 체결 시뮬레이터의 정밀 보정은 아직 불가능

본 연구는 **Read-only public Quotation GET API**만 사용했다. 주문, 주문 테스트, API 키, 인증된 Exchange API, 거래소 계좌는 전혀 사용하지 않았다. 원래 기본/Exaone/Qlib 자동 관측 및 가상 원장은 변경하지 않았다.

정식 출처:
- 업비트 공개 [호가 조회](https://docs.upbit.com/kr/reference/list-orderbooks): `GET https://api.upbit.com/v1/orderbook?markets=KRW-BTC,KRW-ETH,KRW-XRP,KRW-ADA&count=30` (최대 30호가, 10req/sec 그룹).
- 업비트 공개 [최근 시장 체결 조회](https://docs.upbit.com/kr/kr/reference/list-pair-trades): `GET https://api.upbit.com/v1/trades/ticks?market=...&count=200` (각 페어 최근 체결, UTC 날짜별 조회는 보통 최근 7일 범위).
- 웹소켓 호가 구독도 사용 가능하지만 이번에는 사용하지 않았다. 과거 2018~2026년 오더북이 자동 소급 제공되는 것이 아니다.

### 실제 확보·해시 봉인

| 공개 데이터 | 수집 시각 UTC | 원본 데이터 건수 | 저장 |
|---|---|---|---|
| 실시간 30호가 | 2026-10-21 05:02:30.903 ~ 05:02:45.269 | 8개 batched HTTP × 4자산 = **32 책** | `reports/upbit-live-orderbook-20261021/` |
| 시장 체결 프린트 | 2026-10-21 05:06:34.934 ~ 05:06:35.033 | 200건×4자산 = **800건** | `reports/public-trade-prints-20261021/` |

각 HTTP 응답의 원본 JSON SHA-256, 수신 시각, 해당 페어의 exchange timestamp, 스냅샷 지연시간을 증거로 남기고, 조작·중복 호가 타임스탬프·크로스 호가·음수 잔량·가격 역순·페어 불일치·12초 이상 낡은 시세는 분석에서 거부한다. 시장체결은 거래의 식별번호 중복, 비정상 가격/수량, 타임스탬프 및 원본 해시 무결성을 독립 감사한다.

수집된 시장 체결 200건의 시간 폭은 BTC 약 133초, ETH 약 210초, XRP 약 273초, **ADA 약 2,931초(48.9분)**이다. 각 200건은 같은 기간의 유동성 지표가 **아니며**, 공개 거래 프린트는 우리 주문의 실제 체결·대기열 위치를 나타내지 않는다. 호가와 체결 프린트도 수 분 간격으로 수집됐으므로 **동일 호가에 귀속된 체결이라고 추정하지 않는다**.

### 1,000만원 즉시 공격형 매수 가정: 화면 30호가의 깊이 기반 가격 불이익

`중간 호가(mid) = (best bid + best ask)/2`. 매수하려는 코인 기본 수량을 `1,000만원 / 최우선 매도 호가`로 계산하고, 현 시점의 최우선 매도 호가부터 표시된 수량을 순차 소비한 **가상 VWAP**을 계산했다. 매도는 반대 방향. 공개된 깊이에 전량 수량이 없으면 완전체결 VWAP을 계산하지 않는다.

| 페어 | 화면 매수 VWAP vs 호가 중간값 중앙치 | 기존 일봉 `5bp+40bp×sqrt(거래금액/20일 거래대금 중앙값)` | 첫 스냅샷 호가 스프레드 |
|---|---:|---:|---:|
| KRW-BTC | **2.02bp** | 5.43bp | 1.27bp |
| KRW-ETH | **2.91bp** | 5.57bp | 3.07bp |
| KRW-XRP | **2.82bp** | 5.39bp | 5.64bp |
| KRW-ADA | **16.86bp** | 6.05bp | **33.73bp** |

수수료는 표에서 제외한다. 기존 모형의 불리한 가격은 일봉 **시가 대비**로 적용되지만, 새 값은 실시간 **호가 중간값 대비**이므로 정확히 같은 기준이 아니다. 현재 단편적 관측상 ADA 최우선 스프레드만으로 상대적으로 비용 부담이 큰 것은 확실하지만, **이 표만으로 옛 모형의 편향을 몇 bp로 확정하거나 매매 정책을 재보정하면 안 된다.**

추가 발견: **1억원 가상 매도 KRW-BTC**는 8개 스냅샷 중 **2개에서 공개된 30호가의 총 잔량만으로 전량 소화 불가**. 공개되지 않은 하위 잔량까지 없다고 주장하는 것은 아니다. 공개된 깊이가 소진되는 주문은 `complete_visible_depth=false`와 `full_fill_vwap=null`로 기록한다.

### 전향적 데이터 필요성 및 게이트

- 오늘 확보한 **8회 × 4페어**, 총 약 **14.37초**의 연속 호가 관측은 가격대별 깊이를 직접 확인하는 데는 의미가 있지만, 시장 국면·시간대·급등락·발표 시점에 따른 가격 영향과 부분 체결 빈도를 검증하기에는 너무 짧다.
- 분석기가 **페어별 최소 100개의 고유 exchange timestamp 샘플 + 24시간 이상 관측 범위** 전에는 `EXPLORATORY_ONLY_NOT_CALIBRATION`으로 처리하도록 했다. 기준을 충족해도 `SNAPSHOT_DEPTH_COVERAGE_GATE_ONLY`일 뿐, 실제 주문 대기열·체결률·시장충격 파라미터 보정 통과가 아니다.
- 최근 체결 프린트는 다른 참가자의 완료된 거래이며, 우리 주문이 있었다면 어느 가격에 얼마나 체결됐을지를 알 수 없다. **실거래를 전혀 하지 않은 상태에서 실행 비용의 실제 예측 오차를 직접 계산할 수 없다**.
- 표의 실시간 결과를 기존 2018~2026년 재학습 OOS 계좌에 주입해 새로운 가상 성적을 생성하지 않았다. 과거 전기간 L2 호가장 데이터가 검증되기 전에는 이식하면 시계열 왜곡이다.
- 충분한 전향적 호가·거래 매칭과 실제 실행 지연·부분체결 증거를 확보한 이후에만 별도 검증된 체결모형 버전을 개발한다. 현재 어떤 모델도 자동 승격·실거래 적격이 아니다.

### 코드 및 재현

- `quantlab/live_orderbook_evidence.py`: read-only 수집, 응답 검증, 원본 해시 봉인, 30레벨 양방향 sweep, 20일 완료봉 거래대금 기반 옛 비용 가정 병렬 *참고 출력*, 미충족 캘리브레이션 판정.
- `quantlab/live_trade_evidence.py`: read-only 시장 거래 프린트 보존·원본 해시·고유 ID 검증. 거래량을 시가 실행 가능량으로 사용하는 코드 없음.
- `tests/test_live_orderbook_evidence.py`, `tests/test_live_trade_evidence.py`: 변조/누락/시간/거래량/불완전 30호가/중복 견적/인증 없는 GET/실제 주문 금지 안전 계약.

이미 수집된 스냅샷을 *네트워크 없이* 분석 재현 (보고서 대상 디렉터리는 새로 작성):

```bash
cd /home/hong/apps/FreeQuantLab-orderbook-20261021
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.live_orderbook_evidence \
  --analyze-only --capture-dir reports/upbit-live-orderbook-20261021 \
  --report-dir reports/UNIQUE_OFFLINE_ANALYSIS
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.live_trade_evidence \
  --audit-only --output-dir reports/public-trade-prints-20261021
```

새 시점 수집은 `--capture-dir`와 `--report-dir`에 반드시 빈 새 경로를 지정. 2026-10-20 종료 스냅샷으로 계산한 비용 참고선은 **그 다음 UTC 날짜 2026-10-21까지만** 유효하도록 차단돼 있다. 미래 날짜 수집의 거래대금 비교를 원하면 최신 완성 일봉을 새 SHA 고정 스냅샷으로 갱신해 입력해야 한다.

연구 코드와 실서버 원장, systemd 기존 3개 자동 타이머, 실거래 권한은 분리 유지.
