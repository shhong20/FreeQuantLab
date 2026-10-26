# KIS 장 구간별 분봉 커버리지 및 일봉 거래량 차이 검증 (2026-10-26)

## 주요 결론

**18개 실제 KIS 독립 시장 스냅샷**(삼성전자 005930, SK하이닉스 000660 × 2026-10-21/22/23 × KRX J·NXT NX·통합 UN)을 읽기 전용으로 분석했다.

1. **6개 종목일 모두** `J` 역사 분봉: 09:00~15:19 380개, 15:30 단일가 라벨 1개, 16:00~19:59 240개. 하지만 KRX **장전 08:30~08:39 라벨 0개**, **장후 15:40~15:59 라벨 0개**.
2. `NX` 역사 분봉: 08:00~08:49 50개, 09:00~15:19 380개 (NXT 실제 메인은 09:00:30부터 시작), 15:40~19:59 260개.
3. `UN` 분봉은 15:40~15:59 시간 라벨 20개를 제공한다. **이는 통합 분봉에 시간 라벨이 있다는 사실이지 KRX의 같은 구간 체결이 포함됐다는 증거가 아니다.**
4. 시장별 KIS **공식 일봉 거래량은 6/6 일치**: `UN 일봉 = J 일봉 + NX 일봉`. 그러나 KRX의 일봉 거래량은 매 사례 분봉 합보다 많으며, NXT도 약간 부족하다.
5. **UN 통합 분봉의 09:07·09:08 불일치(삼성전자 10월 23일)**와 역사 분봉 소급 변경(KRX J 2개·UN 1개)은 독립적인 데이터 정합성 문제로 유지한다.
6. KRX 일봉−KRX 분봉 차이를 KRX 15:40~16:00 체결이라고 단정하지 않는다. 누락 후보 구간은 **08:30~08:40 장전 시간외종가**, **15:40~16:00 장후 시간외종가**, 데이터 경계/정정 및 제공사 시간 라벨의 의미가 포함된다.

### 수집된 실제 자료별 일봉−분봉 거래량 차이 (주)

| 거래일 | 종목 | KRX J | NXT NX | 통합 UN |
|---|---|---:|---:|---:|
| 2026-10-21 | 삼성전자 | 632,089 | 585 | 632,674 |
| 2026-10-21 | SK하이닉스 | 153,682 | 28 | 153,710 |
| 2026-10-22 | 삼성전자 | 61,890 | 282 | 62,172 |
| 2026-10-22 | SK하이닉스 | 294,375 | 16 | 294,391 |
| 2026-10-23 | 삼성전자 | 179,083 | 318 | 563,911 |
| 2026-10-23 | SK하이닉스 | 231,428 | 105 | 231,533 |

삼성전자 10월 23일은 J+NX 분봉 합계와 UN 분봉 합계가 **384,510주 다르므로**, 단순히 KRX·NXT 각 거래소의 일봉−분봉 부족분을 UN 부족분과 같게 보면 안 된다.

```text
UN 일봉−UN 분봉
= (J 일봉−J 분봉) + (NX 일봉−NX 분봉)
− (UN 분봉 − J 분봉 − NX 분봉)

2026-10-23 삼성전자:
563,911 = 179,083 + 318 − (−384,510)
```

이 식은 **산술 항등식**으로서 거래량 차이를 특정 매매 구간이나 특정 거래소에 귀속시키는 증명이 아니다.

## 관련 공식·시장 운영 안내

- 한국투자증권 공식 `FHKST03010230` 예제에서 `stck_cntg_hour`를 **주식 체결 시간**, `cntg_vol`을 **체결 거래량**이라고 표기한다. 다만 API 사용 예제만으로 분봉 라벨의 시작·끝 기준, 정정 체결 소급 정책, 정규장/시간외 체결을 모두 포함하는지 확정할 수 없다.
  - https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_time_dailychartprice/chk_inquire_time_dailychartprice.py
- KRX의 2026-09-14 애프터마켓 신설(16:00~20:00), 장후 시간외종가 거래(15:40~16:00), 장전 08:30~08:40 등은 서로 다른 거래 단계다.
  - https://www.hygood.co.kr/contents/05/02/050204
  - https://www.kbsec.com/go.able?idt=20260904&linkcd=s060901010000&seq=10010298
- NXT 프리 08:00~08:50, 메인 09:00:30~15:20, 애프터 15:40~20:00. NXT 15:30~15:40 경매와 15:40 이후 실제 체결 구간은 나눠 보아야 한다.
  - https://www.nextrade.co.kr/menu/transactionSys.do
  - https://www.ibks.com/customer/stock/nxtInfo_01.jsp

## 구현한 오프라인 검증

새 모듈: `quantlab.intraday_kis_phase_coverage`

```bash
cd /home/hong/apps/FreeQuantLab-intraday-20261026
/home/hong/apps/FreeQuantLab/.venv/bin/python \
  -m quantlab.intraday_kis_phase_coverage \
  --source-root /home/hong/apps/FreeQuantLab/state/intraday_research/venues_cohort_20261026_run1 \
  --out-dir /home/hong/apps/FreeQuantLab/state/intraday_research/phase_coverage_20261026_r1
```

**WSL 실제 저장 원본으로 실행 완료**. 6개 완전한 종목일에 대해 `KRX_PRE_OPEN_FIXED_PRICE`, `KRX_AFTER_FIXED_PRICE`, `KRX_REGULAR_CONTINUOUS`, `KRX_AFTER_CONTINUOUS`와 NXT 분리 구간의 **존재하는 라벨 수 / 기준 라벨 수 / 빈 라벨 수**를 계산한다. 일봉과 관측 분봉 합계의 차이와 시장별 합산 항등식도 확인한다.

관측과 추정을 구분하며 `no_missing_phase_volume_imputation=true`, `usable_for_backtesting=false`, `usable_for_paper_or_live=false`. API 인증키·원격 인터넷·주문·기존 운영 원장에 접근하지 않는다. 현장 실측 출력:

`/home/hong/apps/FreeQuantLab/state/intraday_research/phase_coverage_20261026_r1/coverage_report.json`

## 검증 후 필요한 외부 데이터와 전략 승격 조건

1. KIS 측에 `FHKST03010230`의 과거 데이터 범위에 **KRX 장전·장후 시간외종가 거래가 포함되는지**, 해당 체결은 다른 TR/API로 확인할 수 있는지 문의.
2. KIS 측에 `stck_cntg_hour` 라벨 기준(체결시각/분 시작/분 종료), `cntg_vol`의 집계 의미와 `output2` 과거 데이터 수정 이력 확인.
3. 09:07·09:08의 UN 분봉은 동일 시각 시장별 합과 거래량/가격 범위가 맞지 않고 재조회 시 일부 값이 바뀜. 공급원 독립 검증 전 매매전략 데이터로 승격 금지.
4. 전략 워크포워드는 **KRX 정규장 전용, 정규장과 실제 거래 가능한 호가/슬리피지를 분리**하는 데이터 계약이 승인된 후에 시작. NXT·UN과 애프터마켓 전략은 별도 거래시간/정정 체결 검증이 필요함.

기존 90일 평가·가상거래·실주문 원장은 변경하지 않는다.
