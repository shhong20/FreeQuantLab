# KIS 09:07·09:08 통합 분봉 이상값의 독립 재수집 검증 (2026-10-26)

## 실측에서 확인된 사실

삼성전자 `005930`의 2026-10-23 과거 `FHKST03010230` 원본을 KRX (`J`), 넥스트레이드 (`NX`), 통합 (`UN`) 각각 저장했다.

| 항목 | KRX | NXT | 통합 |
| --- | ---: | ---: | ---: |
| 1분봉 고유 시간 라벨 | 621 | 690 | 691 |
| 1분봉 거래량 합계 | 15,966,889 | 10,629,164 | 26,211,542 |
| 공식 KIS 일봉 거래량 | 16,145,972 | 10,629,482 | 26,775,454 |
| 일봉 − 분봉 수량 | 179,083 | 318 | 563,912 |

공식 일봉 거래량은 `16,145,972 + 10,629,482 = 26,775,454`로 정확히 일치한다. 하지만 통합 분봉은 09:07에 **160주**, 09:08에 **384,351주**가 단순 시장별 합계보다 적다. 09:08 통합 봉의 고가·저가가 **205,000원·205,000원**으로, 같은 라벨의 개별 시장 고가·저가 범위 **205,500원·203,500원**과도 맞지 않는다.

아래 등식도 실제로 성립한다.

```text
통합 일봉 − 통합 분봉 = 563,912주
= (KRX 일봉 − KRX 분봉) 179,083
+ (NXT 일봉 − NXT 분봉)     318
+ (시장별 분봉합 − 통합 분봉) 384,511
```

**해석 주의:** 원본 세 시장 모두 마지막 1분봉 라벨이 `19:59:00`이다. 20:00 거래 종료 경계에서 빠진 체결을 이 등식으로부터 **단정할 수 없다**. 공식 KRX 애프터마켓은 16:00~20:00이고, NXT도 20:00까지 거래하므로 이 가설은 독립 재조회·시간라벨 해석을 통해 추가 입증이 필요하다. (<https://www.yna.co.kr/view/AKR20260910157400008>)

## 재조회는 UN만 먼저, 원본은 불변으로

이 단계에서는 원본 KRX·NXT는 그대로 유지하고, 이상 구간이 발견된 **통합(`UN`) 과거 분봉**만 독립 재요청한다. 같은 날 같은 종목을 대상으로 하되 반드시 다른 `--out-dir`를 사용한다.

KIS 비밀키를 원격 대화에 전달하지 않는다. **사용자 WSL 로컬 셸에서만** 다음 명령 실행:

```bash
cd /home/hong/apps/FreeQuantLab-intraday-20261026
set -a
source ~/.config/freequantlab/kis.env
set +a

/home/hong/apps/FreeQuantLab/.venv/bin/python \
  -m quantlab.intraday_kis_venues \
  --symbol 005930 \
  --session 2026-10-23 \
  --markets UN \
  --out-dir /home/hong/apps/FreeQuantLab/state/intraday_research/venues_replay_20261026_run1
```

기존 스냅샷 폴더 `state/intraday_research/venues/KIS_UN_EXT_005930_2026-10-23`를 수정하지 않는다. 새 원본은 `state/intraday_research/venues_replay_20261026_run1/KIS_UN_EXT_005930_2026-10-23`에 별도 저장된다. 같은 명령 재실행으로 기존 결과를 덮어쓰지 않는다.

## 원본 재요청 완료 후, 두 자료를 **오프라인**으로 비교

```bash
/home/hong/apps/FreeQuantLab/.venv/bin/python \
  -m quantlab.intraday_kis_replay_audit \
  --baseline-root /home/hong/apps/FreeQuantLab/state/intraday_research/venues \
  --replay-root /home/hong/apps/FreeQuantLab/state/intraday_research/venues_replay_20261026_run1 \
  --symbol 005930 --session 2026-10-23 --markets UN \
  --out-dir /home/hong/apps/FreeQuantLab/state/intraday_research/replay_audits
```

이 명령은 **네트워크·계정·주문 API를 전혀 사용하지 않는다**.

검증 항목:

1. 기존·새 스냅샷의 원본 페이지 SHA-256, 실제 저장된 정규화 파일 SHA-256, 날짜·시장·종목, 같은 일봉 원본 무결성 검증
2. UN 시간 라벨 신규·누락, 1분 OHLCV 값이 달라진 시간 라벨과 필드만 별도 기록
3. 09:07·09:08 및 장후 마지막 라벨의 통합 거래량을 **최초 확보된 불변 KRX/NXT 원본**과 비교
4. `OBSERVATIONS_REPRODUCED`면 공급사가 동일하게 반환했다는 뜻일 뿐 **값이 정상이라는 증거가 아님**
5. `VENDOR_OBSERVATIONS_CHANGED_HOLD`면 새로운 원본이 달라졌다는 뜻일 뿐 **어느 쪽이 옳은지 확정하지 않음**
6. 재요청해도 `usable_for_backtesting=false`, `usable_for_paper_or_live=false`. 기존 자료를 자동으로 고치거나 통합 거래량을 인위적으로 합산하지 않는다.

감사 결과는 `state/intraday_research/replay_audits/KIS_REPLAY_UN_005930_2026-10-23/audit.json`과 `audit.sha256`에 불변 기록된다.

## 다른 종목·거래일로 일반화하는 기준

삼성전자 한 종목의 1일 실측으로 전체 시장 데이터가 일관되거나 결함이 있음을 일반화할 수 없다. 다음 수집에서는 KRX·NXT에서 충분히 거래되는 다른 종목과 **거래일 3~5일을 먼저** 대상으로 한다. 거래정지·상장시점·기업행동을 통제하고 분봉-일봉 차이 및 거래소별 합산 오류 빈도를 기록한다. 그 후 충분한 과거 기간(전략 연구 기준 약 60거래일 이상)을 확보한다.

**기본 원칙:** 정합성 검증 전 통합 자료는 전략 성과·모의/실매매 신호에 투입하지 않는다.
