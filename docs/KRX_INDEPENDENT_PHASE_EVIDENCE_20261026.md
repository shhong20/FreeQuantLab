# FreeQuantLab — 2026년 KRX 시간외종가·대량매매 원본 확보 및 외부 증거 게이트

## 1. 공식 소스와 접근 가능 범위 (2026-10-26 조사)

| 경로 | 확인한 제공 범위 | 특정 종목·거래일의 **장전/장후 시간외종가 거래량** 조회 가능 여부 | 제약 |
|---|---|---|---|
| KRX 공식 거래제도 | 장전 시간외종가 08:30~08:40, 장후 15:40~16:00, 애프터마켓 16:00~20:00. 별도로 장전 08:00~09:00 및 장후 15:40~20:00 대량·바스켓 매매 존재 | 시간표만 제시 | 체결량 원본 아님 |
| KRX 무료 Open API 서비스 목록 | **유가증권 일별매매정보**와 종목기본정보 등 제공 | **세션별 분리 컬럼 확인되지 않음** | 일봉 합계에 대한 독립 비교 후보일 뿐; API 인증·이용승인 가능성 |
| KRX 시세정보 분배상품 | 별도 정보상품에 시간외거래 데이터 항목 있음 | 원본 계약·상품 세부 스키마 확인 필요 | 배포·가공·축적 라이선스 검토 |
| 코스콤 시세 Open API | 주식 실시간/일중, 종가 히스토리 등 | 과거 특정 날짜의 시간외종가 체결량은 현 공개 문서만으로 확인 안 됨 | 데이터 **가공·축적 시 시세 라이선스 계약 필요**, 샌드박스 수치 일치 보장 안 됨 |
| 현재 KIS `FHKST03010230` | J/NX/UN 과거 1분 OHLCV | 6개 KRX 실제 원본에서 08:30~08:39 및 15:40~15:59 시간 라벨 부재 | 없는 라벨을 0주 체결로 해석 금지 |
| KIS `FHPST02320000` | 과거 30거래일 `ovtm_untp_*` | 2026-09-14 이후 옛 시간외 단일가 가격 필드 0; **현행 시간외종가·애프터마켓의 증거 아님** | 이미 2종목·30일 조회 검증, 재호출 불필요 |

검토 링크:
- KRX 공식 시장운영 규정: https://regulation.krx.co.kr/contents/RGL/03/03020401/RGL03020401.jsp
- KRX 시간외 대량/바스켓 매매 규정: https://regulation.krx.co.kr/contents/RGL/03/03020303/RGL03020303.jsp
- KRX Open API 공식 서비스 목록: https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd
- KRX 유가증권 일별매매정보: https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES002_S2.cmd?BO_ID=JvJFzlAENzZlPBDNGAWC
- KRX 시세정보 분배상품: https://openapi.krx.co.kr/contents/OPP/DATA/OPPDATA002.jsp
- 코스콤 시세 라이선스 안내: https://koscom.gitbook.io/open-api/api/marketv3

**결론:** 과거 `005930`, `000660`의 2026-10-21~23 **시간외종가만 분리된 무료 공개 체결량 API는 검증하지 못했다.** KRX 일별매매 API에 해당 값이 있다고 추정하지 않고, 코스콤의 별도 계약 상품을 무료라고 간주하지 않는다.

## 2. 실측 데이터에서 해결되지 않은 차이

기존 불변 KIS J/NX/UN 18개 소스 중 KRX J 6개 일봉 − 수집된 분봉의 차이는:

| 날짜 | 종목 | KRX 공식 일봉−관측 1분봉 거래량 (주) | 시간외종가 등 원천 증거 |
|---|---|---:|---|
| 2026-10-21 | 삼성전자 `005930` | 632,089 | 아직 없음 |
| 2026-10-21 | SK하이닉스 `000660` | 153,682 | 아직 없음 |
| 2026-10-22 | 삼성전자 | 61,890 | 아직 없음 |
| 2026-10-22 | SK하이닉스 | 294,375 | 아직 없음 |
| 2026-10-23 | 삼성전자 | 179,083 | 아직 없음 |
| 2026-10-23 | SK하이닉스 | 231,428 | 아직 없음 |

주의: 이 값은 **시간외종가 거래량이 아니다**. KRX 대량·바스켓 거래, 경계 체결, 정정 등 다른 이유도 있을 수 있고, 일봉 전체의 정확한 거래집계 범위 자체도 확인해야 한다.

2026-10-23 삼성전자 UN 통합 09:08의 거래량 부족(이전 406→후속 407주 소급변경)도 **독립적인 통합 분봉 이슈**로 별도 보존한다.

## 3. 새 외부 증거 게이트

`quantlab.intraday_krx_phase_evidence`를 추가했다.

- **기본 실행에 API 인증/외부 HTTP가 필요 없음.** 기존 18개 소스의 KRX J 부분을 읽기 전용 검증.
- 추후 공식 소스를 입수한 경우 각 거래일·종목의 `KRX_PREOPEN_FIXED_CLOSE`, `KRX_POSTCLOSE_FIXED_CLOSE`, `KRX_PREOPEN_BLOCK_BASKET`, `KRX_POSTCLOSE_BLOCK_BASKET`를 구분해 비교.
- 원본 파일을 보존하고 파일 SHA-256과 비경로탈출, 음수·중복·다른 날짜·출처 범위 검사.
- **네 구간 중 하나라도 미확보면 전체 잔차를 계산하지 않음**(확인된 합계만 별도 표기).
- 네 구간이 있어 산술 합계가 KRX 부족분과 **우연히 같더라도** 원본 배포자의 진위·거래시간 정의·공식 집계 완전성은 입증되지 않은 것으로 처리. `usable_for_backtesting=false`.
- 거래시간 외 대량매매에 정규장 종목별 체결, 20:00 경계/정정 거래 등 다양한 구간이 있으므로 네 가지는 **후보 구간**이고 전부 아님.
- 기존 원본 파일에 기록을 덧씌우거나 결측 분봉을 생성하지 않음.

실제로 WSL에서 실행:

```bash
cd /home/hong/apps/FreeQuantLab-intraday-20261026
/home/hong/apps/FreeQuantLab/.venv/bin/python \
  -m quantlab.intraday_krx_phase_evidence \
  --cohort-root /home/hong/apps/FreeQuantLab/state/intraday_research/venues_cohort_20261026_run1 \
  --out-dir /home/hong/apps/FreeQuantLab/state/intraday_research/krx_official_phase_evidence_20261026_r1
```

6개 소스 모두 정상 조회되어, 구간별 공식 기록 미확보 6건을 정확하게 표시함. 결과:
`/home/hong/apps/FreeQuantLab/state/intraday_research/krx_official_phase_evidence_20261026_r1/phase_evidence_audit.json`

### 외부 원본을 실제로 입수한 뒤의 연결 형식

별도 `evidence_root` 폴더에 공식 원본 파일을 **수정하지 않고** 넣고, `manifest.json`에서 출처·해시·종목·날짜·구간별 정확한 수량을 명시한다. `source_file`은 동일 루트 아래 파일이어야 한다. 예시(실제 자료가 아니므로 숫자를 임의로 채우지 않음):

```json
{
  "format_version": 1,
  "entries": []
}
```

실제 공식 원본이 준비된 경우 `entries` 각 객체에는 `symbol`, `session`, `phase`, `volume`(0 이상 정수), `provider`(KRX/KIS/KOSCOM), `source_file`, `source_sha256`(64자리 소문자 해시), `source_record_id`(원본 기록의 안전한 식별자)를 입력한다. **0이라고 적을 수 있는 건 공식 기록이 명시적으로 체결량 0을 보고한 경우뿐**이다. 원본의 실제 제도·구간 매핑은 도구가 자동 증명하지 않는다.

```bash
/home/hong/apps/FreeQuantLab/.venv/bin/python \
  -m quantlab.intraday_krx_phase_evidence \
  --cohort-root /home/hong/apps/FreeQuantLab/state/intraday_research/venues_cohort_20261026_run1 \
  --evidence-root /home/hong/apps/FreeQuantLab/state/intraday_research/official_external_evidence \
  --out-dir /home/hong/apps/FreeQuantLab/state/intraday_research/krx_phase_evidence_with_sources_run1
```

외부 원본 자체의 암호학적 해시 일치와 제공기관이 발행한 실제 기록의 진위는 **다른 문제**다. 진위·라이선스·정정 이력 등은 증권사와 거래소로부터 추가 확인한다.

## 4. 다음 외부 검증 질문 — KIS/거래소 문의

1. 2026-10-21~23 `005930`, `000660`의 `J` 과거 분봉 `FHKST03010230`에서 KRX 장전 시간외종가 08:30~08:40 및 장후 시간외종가 15:40~16:00가 왜 분봉 라벨로 표현되지 않는지?
2. KRX 공식 일봉 `acml_vol`은 장전/장후 시간외종가, 애프터마켓 16:00~20:00, 대량·바스켓 매매, 20:00 경계 및 체결취소·정정 거래량을 각각 어떤 방식으로 포함하는지?
3. **특정 과거 날짜·특정 종목·시장단계의 체결량을 각각 조회할 수 있는 공식 TR/API/HTS 다운로드**가 있는지? 해당 데이터의 이용요금 및 별도 시세 라이선스 필요 여부?
4. 삼성전자 2026-10-23 통합 `UN` 09:08 값이 KRX+NXT 분봉과 수십만 주 차이나고 사후 수집에서 406→407주로 바뀐 이유 및 정정 체결 이력 제공 여부?

운영 90일 평가와 주식/코인 자동매매 동작은 이 데이터조사 때문에 변경하지 않는다.
