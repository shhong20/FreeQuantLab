# FreeQuantLab — KIS 장전·장후 시간외종가 **당일 순위 스냅샷** (2026-10-26)

## 공식 예제에서 찾은 무료 조회 후보

KIS 공식 Open API의 **국내주식 시간외잔량 순위** (`FHPST01760000`, `GET /uapi/domestic-stock/v1/ranking/after-hour-balance`)는 다음 필드를 제공한다.

| 필드 | 공식 예제 설명 | 의미 제한 |
|---|---|---|
| `mkob_otcp_vol` | 장개시전 시간외종가 거래량 | **현재 조회 순위 응답에 포함된 값**, 역사 분봉 아님 |
| `mkfa_otcp_vol` | 장종료후 시간외종가 거래량 | **현재 조회 순위 응답에 포함된 값**, 확정 일일 총량임을 입증하지 않음 |
| `stck_shrn_iscd` | 주식 단축 종목코드 | 순위 표에 포함된 종목에 한정 |

공식 출처:
- https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/after_hour_balance/after_hour_balance.py
- https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/after_hour_balance/chk_after_hour_balance.py

예제 인수: `fid_cond_mrkt_div_code='J'`, `fid_cond_scr_div_code='20176'`, `fid_input_iscd='0000'`, `fid_rank_sort_cls_code='1'`(장전), `2`(장후), `fid_div_cls_code='0'` 등. **거래일 날짜 파라미터는 없다**. 때문에 2026-10-21/22/23 기존 역사 기록의 분실된 거래량을 곧바로 복구할 수는 없다. 또한 순위에 포함되지 않은 종목의 거래량은 **0이 아니라 관측 불가**다.

## 구현 기능

`quantlab.intraday_kis_fixed_close_rank`

- 사용자 WSL 로컬 환경에서만 `--execute`로 조회, 기본값 DRY RUN.
- `--expected-session`은 **현재 KST 날짜만** 허용하며, 미래·과거·주말은 차단.
- 데이터 수집 시각을 임의로 기록하지 못하도록 지정한 개장/장후 구간에서만 API 호출.
- **장전 PRE: 08:30~09:10 KST**, **장후 POST: 15:40~16:20 KST**. 허용 범위는 시범 관측 창일 뿐 해당 시장의 종가 확정 시각을 증명하지 않는다. 장전 랭킹 1, 장후 랭킹 2를 각각 1 GET.
- 응답의 `output` 목록에서 `005930`·`000660`을 **존재하는 경우에만** 기록하고 전체 응답 바이트, SHA-256, KST 시각, 정보 출처를 별도 불변 경로에 저장.
- 동일 날짜/구간에 이미 원본이 존재하면 해시 확인 후 추가 GET·OAuth 없이 확인. 원본은 절대 덮어쓰지 않는다.
- **랭킹은 전체 시장 시세가 아니고**, 일정 수 종목만 반환할 수 있으며 과거 일봉의 체결원장에 합산하지 않는다.
- `usable_for_backtesting=false`; 주문, 전략 실험, 90일 평가 운영 원장 수정 없음.

## 사용 명령

### 시간 외: 지금 실행해도 파일·인증 요청이 일어나지 않도록 하는 기본 미리보기

*주의*: 정확한 날짜와 관측 창은 미리보기에도 적용된다. 다른 시각이면 `FAIL CLOSED`가 정상이다.

```bash
cd /home/hong/apps/FreeQuantLab-intraday-20261026
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
ROOT=/home/hong/apps/FreeQuantLab/state/intraday_research
TODAY=$(TZ=Asia/Seoul date +%F)

"$PY" -m quantlab.intraday_kis_fixed_close_rank \
  --phase PRE --expected-session "$TODAY" \
  --out-dir "$ROOT/fixed_close_rank_20261026"
```

### 실제 장전 / 장후 관측 창에, 로컬 WSL에서 실행

*비밀키를 ChatGPT/PR/터미널 출력에 붙여 넣지 않는다*. KIS 기존 `kis.env`를 사용한다. 과거 날짜는 절대 받을 수 없으며, 기본 DRY RUN으로 API가 호출되지 않는다.

```bash
cd /home/hong/apps/FreeQuantLab-intraday-20261026
set -a
source ~/.config/freequantlab/kis.env
set +a

PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
ROOT=/home/hong/apps/FreeQuantLab/state/intraday_research
TODAY=$(TZ=Asia/Seoul date +%F)

"$PY" -m quantlab.intraday_kis_fixed_close_rank \
  --phase PRE --expected-session "$TODAY" \
  --out-dir "$ROOT/fixed_close_rank_live" --execute

# 별도: 당일 15:40~16:20 KST에만 수행
"$PY" -m quantlab.intraday_kis_fixed_close_rank \
  --phase POST --expected-session "$TODAY" \
  --out-dir "$ROOT/fixed_close_rank_live" --execute
```

이 API는 요청할 당시 시장 상태를 나타내는 것으로 보이며, 순위가 005930·000660을 누락할 수 있다. 실시간 스냅샷만으로 **확정 장전/장후 체결량**이나 과거 10월 21~23일 부족분을 계산해 보고하지 않는다.

성공하면 `$ROOT/fixed_close_rank_live/KIS_AFTER_HOUR_RANK_J_<session>_PRE` 및 POST 아래에 `raw.json`, `audit.json`, `audit.sha256`가 생성된다. 누락 종목은 null로 기록한다.

## 다음 실제 검증

1. 실제 KST 장전·장후 창 각각 1회 소량 조회하고 `output` 유무, 종목별 `mkob_otcp_vol`·`mkfa_otcp_vol` 반환 여부 확인. 응답이 빈 랭킹이면 **비교 실패**, 거래량 0이 아님.
2. 같은 날짜 KRX 공식 일봉·KIS J 일봉/분봉과 **별도 시간/시장 집계 계약** 검증. KIS 고객센터에서 각 field의 기준 누적/확정 의미 문의.
3. 2026-10-21~23의 과거 부족 거래량은 공개 데이터의 확인 가능한 TR 부재로 **계속 미해결**. 앞으로 수집하는 당일 자료를 과거 날짜로 소급 적용하지 말 것.
4. KIS 공식 문의 양식: 어떤 TR에서 `market=J` 과거 날짜별 장전/장후 시간외종가 체결량과 16~20 애프터마켓을 완전히 분리 가능한지, 집계 기준(시간, 누적, 정정)과 추가 라이선스 필요 여부 확인.

