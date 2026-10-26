# KIS 시간외 단일가 필드의 제도 개편 전후 검증 (2026-10-26)

## 실제 데이터로 확인된 사실

한국투자증권 `FHPST02320000` (시간외 일자별 시세) 응답을 **실제 로컬 KIS 인증**으로 삼성전자 `005930`, SK하이닉스 `000660` 각각 1회 조회하고, 원본과 SHA-256을 불변 보존했다. 최근 **30거래일(2026-09-09~2026-10-26)**, 총 60개 종목·거래일 행이 정상 반환됐다. KIS 공급사 결과 코드 `rt_cd=0`.

| 기간 | 두 종목 각 데이터 개수 | `ovtm_untp_prpr` (시간외 단일가 가격) | `ovtm_untp_vol` | `ovtm_untp_tr_pbmn` |
|---|---:|---|---|---|
| 9/9~9/11 (제도 변경 전) | 각각 3개 | 모두 0 아님 | 모두 0 | 모두 0 |
| 9/14~10/26 (제도 변경 후) | 각각 27개 | **모두 0** | 모두 0 | 모두 0 |

이 날짜 경계는 **KRX가 2026-09-14 기존 16:00~18:00 시간외 단일가를 폐지하고 16:00~20:00 애프터마켓 접속매매로 대체**한 날짜와 정확히 일치한다.

- KB증권 공식 변경 공지 (2026-09-04): https://www.kbsec.com/go.able?idt=20260904&linkcd=s060901010000&seq=10010298
- 카카오페이증권 공식 거래시간 변경 공지 (2026-09-03): https://www.kakaopaysec.com/customer/notice/dynamicBoardPageDetail.do?id=7452
- KIS 공식 API: https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_overtimeprice/inquire_daily_overtimeprice.py

**해석:** 가격 필드가 제도 개편 경계에서 0으로 바뀐 현상은 폐지된 *옛 시간외 단일가 세션*의 필드라는 설명과 일치한다. 그렇다고 **0주 실제 시간외 거래** 또는 **KRX 애프터마켓 0주**라고 해석하면 안 된다. 이전 3거래일에도 이 API의 `ovtm_untp_vol`과 거래대금이 0이어서, **필드 거래량의 실제 공급 범위/정확성도 미검증**이다.

### KIS 일봉 교차검증

같은 API의 `acml_vol`(누적 거래량), `stck_clpr`(종가)은 독립 수집한 KIS KRX 시장 `J` 공식 일봉 **2종목×3일 총 6/6건에서 정확히 일치**했다.

| 날짜 | 종목 | `FHPST02320000 acml_vol` = 공식 KRX 일봉 | `stck_clpr` |
|---|---|---:|---:|
| 10/21 | 삼성전자 | 17,268,762 | 223,500 |
| 10/22 | 삼성전자 | 11,469,322 | 214,500 |
| 10/23 | 삼성전자 | 16,145,972 | 215,500 |
| 10/21 | SK하이닉스 | 4,228,927 | 1,294,000 |
| 10/22 | SK하이닉스 | 2,841,149 | 1,223,000 |
| 10/23 | SK하이닉스 | 3,941,446 | 1,277,000 |

이 일봉의 **동일성은 시간외 단일가 거래량 0과 별개**다. 기존 KRX 일봉-분봉 차이(최대 수십만 주)는 이 API `ovtm_untp_vol` 필드로는 해소되지 않는다. 수정·누락 분봉을 임의 생성하지 않는다.

## 구현 및 재현 경로

오프라인 `quantlab.intraday_kis_overtime_regime`를 추가했다. 처음 수집한 SHA-256 원본을 검증한 후, 2026-09-14 전후 각 `ovtm_untp_*` 필드의 0/비0/결측 건수를 분리 기록하며 공식 KRX 일봉의 거래량·종가 값이 같은지 비교한다.

```bash
cd /home/hong/apps/FreeQuantLab-intraday-20261026
/home/hong/apps/FreeQuantLab/.venv/bin/python \
  -m quantlab.intraday_kis_overtime_regime \
  --overtime-root /home/hong/apps/FreeQuantLab/state/intraday_research/overtime_daily_20261026_run1 \
  --cohort-root /home/hong/apps/FreeQuantLab/state/intraday_research/venues_cohort_20261026_run1 \
  --out-dir /home/hong/apps/FreeQuantLab/state/intraday_research/overtime_legacy_scope_20261026_run1
```

WSL 실측으로 완료. 원본 불변 출력:
`/home/hong/apps/FreeQuantLab/state/intraday_research/overtime_legacy_scope_20261026_run1/legacy_regime_scope.json`

**독립 판단**: 시간외 종가(08:30~08:40/15:40~16:00)와 2026 KRX 애프터마켓(16:00~20:00) 실제 분봉·거래량을 각각 확인하려면 다른 공식 체결원장, 세션 분리 KRX 데이터 또는 KIS 공급사 계약이 필요하다. `FHPST02320000`의 단일가 필드 0은 어느 것도 설명하지 못한다.

**품질 게이트 유지**: `usable_for_backtesting=false`, `usable_for_paper_or_live=false`; 운영 90일 평가와 시세 원본·매매 API에는 어떤 변경도 없다.
