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

## 실제 독립 재조회 결과 — 동일 역사 데이터, 달라진 현재 시세 헤더 (2026-10-26)

사용자가 로컬 인증된 WSL에서 동일 조건(`UN`, 005930, 2026-10-23)을 최초와 별도의 디렉터리에 직접 재수집했다. 결과:
- 통합 691개 고유 1분 시간 라벨, 정규화 파일 SHA-256 최초·재수집 **완전 일치**: `daa5c5e2057c3dd2bad44d60b1dd81847c961fcdab8f0eca20f23d8346b1f142`
- 원본 분봉 6페이지의 `output2` 배열을 **JSON 배열 자체로 비교해 모두 동일**. KIS 공식 일봉 `output2`도 완전히 동일.
- API 응답 전체 원본 바이트의 SHA-256은 최초·재수집이 **다름**. 이는 각각의 응답 `output1` 객체에 있는 조회 시점 현재 시세 필드가 변경됐기 때문이다. 분봉 `output1`에서 달라진 항목 이름은 `acml_tr_pbmn`, `acml_vol`, `prdy_ctrt`, `prdy_vrss`, `stck_prpr`이다. **필드 값 자체나 비밀키는 로그·보고서에 기록하지 않는다.**
- 공식 일봉도 `output2`의 OHLCV가 같지만 `output1` 최신시세 필드가 변경됐다. 따라서 `raw_page_hashes_identical=false`를 근거로 **과거 시세가 달라졌다고 해석하면 잘못된 결론**이 된다.
- **09:07** 통합 거래량 143,952주 vs J+NX 144,112주 (−160주), **09:08** 통합 거래량 406주 vs J+NX 384,757주 (−384,351주), 통합 09:08 고가·저가 205,000원 vs 개별 시장 봉 고가·저가 205,500원 / 203,500원은 **모두 동일하게 재현**됐다.
- 해당 09:08 분봉은 첫/마지막 원본 페이지 경계가 아니라 09:45~08:00 범위 페이지 내부에 있었다. 페이지 커서 중복 처리 오류보다 **공급사가 안정적으로 반환하는 역사 분봉 값의 시장별 불일치**라는 설명이 더 적합하다. 다만 이 원본이 올바르다는 뜻이 아니다.

이를 기계적으로 설명하기 위해 `intraday_kis_replay_audit`에 다음 감사를 추가했다.
1. 원본 전체 바이트·해시 검증 후 **분봉 6페이지와 일봉**의 `output2` 데이터를 `output1`과 분리 비교
2. `HISTORICAL_BARS_SAME_VENDOR_OUTPUT1_CHANGED`는 분봉·일봉 본문이 동일하나 시점별 시세 헤더만 다름을 의미
3. 실제 `output2`가 바뀌면 `HISTORICAL_PAGE_OR_DAILY_PAYLOAD_DIFFERENCE_HOLD` / `REPLAY_HISTORICAL_PAYLOAD_CHANGED_HOLD`로 분리해서 신호·매매 승격 차단
4. 변경된 `output1` **필드명만** 기록. 공급사의 현재가·고객값·원문 메시지를 새 감사에 복사하지 않음

실제 증거 파일 (원본 불변, 두 번째 오프라인 감사):
`/home/hong/apps/FreeQuantLab/state/intraday_research/replay_audits_output2_verified_v2/KIS_REPLAY_UN_005930_2026-10-23/audit.json`

### 다음 질문을 해소하려면
- KIS에 **시각 09:08 통합 OHLCV가 양 거래소 자료와 다른 이유**, 수정된 과거 분봉 이력, `stck_cntg_hour` 라벨 정의, `cntg_vol` 시간대 범위를 확인한다.
- 같은 조건을 삼성전자 외 **다른 종목과 여러 거래일**에 소량 반복 조회하여 불일치율을 측정한다. 이 시점까지 자동 보간·봉 수정·연구 성과·실거래 반영은 금지한다.
