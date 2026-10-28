# KIS 접근토큰 재사용 및 중복 발급 방지 — 2026-10-28

## 문제
2026-10-28 15:17 KST에 KIS KRX 현재가 체결/당일분봉 GET은 성공했으나, 이어서 새 Python 프로세스로 3종 L2·시장 틱·1분봉 동시 수집을 시도할 때 `/oauth2/tokenP`가 HTTP 403을 반환했다. 기존 `quantlab.data._kis_token`은 `lru_cache`로 **동일 프로세스에서만** 토큰을 재사용하고, 새 프로세스마다 새 발급 요청을 했다. 긴 프로세스에서는 만료되어도 무기한 오래된 토큰을 반환할 가능성이 있었다.

한국투자증권 KIS 개발자센터는 접근토큰 유효기간 **24시간**, 원칙 **1일 1회 발급**, 갱신발급주기 **6시간** 및 API 응답의 `access_token_token_expired`로 만료 시간을 관리하도록 안내한다.
- 공식 문서: https://apiportal.koreainvestment.com/
- 공식 파이썬 예제: https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/kis_auth.py

**403의 상세 서버 원인은 응답 본문을 로그·보고서로 출력하지 않았으므로 확정하지 않는다.** 새 발급 횟수 문제를 줄이는 개선과 실제 원인 확정은 별개의 사항이다.

## 개발 변경
- `quantlab.kis_token_cache.get_token`: KIS 발급 시 유효기간을 KST로 해석 후 UTC 만료시각 저장. 만료 2분 전에는 재사용하지 않음. 최대 허용 유효기간 25시간 검증.
- 기존 `quantlab.data._kis_token`도 공통 캐시 사용. 기존 `lru_cache` 제거해 프로세스 장기 실행 시 만료 확인 가능.
- 키+시크리트 SHA-256 바인딩: 다른 앱키/시크리트에는 다른 토큰을 잘못 재사용하지 않음. 앱 키·시크리트 원문은 캐시에 저장하지 않음.
- 캐시: `~/.config/freequantlab/auth_cache/kis_access_token.json` (0700 디렉터리, 0600 파일). KIS bearer access token은 **민감한 인증정보이며 이 파일에 저장된다**. 운영체제 사용자의 파일 권한으로 보호하는 방식이지 하드웨어 키 보관소나 암호화된 저장소는 아니다. 저장된 토큰을 백업/공유/버전 관리하거나 원격 업로드 금지.
- POSIX `flock`: 다른 Python 프로세스가 동시에 실행되어도 동일 토큰 캐시를 확인한 뒤 단 하나만 신규 발급.
- 임시파일 fsync·원자적 rename, 소유자·권한·심볼릭 링크/복수 링크 검증. 안전 검증 불가 시 거부.
- OAuth 403 이후 같은 앱 자격증명에 대해 15분 재시도 억제. 발급 오류는 토큰·키·HTTP 본문을 출력하지 않음.
- `python -m quantlab.kis_token_cache --status`: OAuth 호출 없이 `REUSABLE_TOKEN`, `TOKEN_MISSING_OR_EXPIRED`, `REFRESH_BLOCKED_BY_403_COOLDOWN` 상태만 출력. 실제 토큰 문자열 출력 없음.
- `quantlab.intraday_krx_joint_evidence`: `KIS_ACCESS_TOKEN` 환경변수가 제공된 경우 그 유효 토큰을 우선 사용하며, 없으면 공통 안전 캐시에서 가져온다. 환경변수는 현재 프로세스와 자식 프로세스에서 접근 가능한 민감정보이므로 불필요하게 쓰지 않는 것이 바람직하다.
- 주문 API·운영 90일 평가·연구 원본·전략 PnL 모델은 변경하지 않음.

## 2026-10-28 WSL 확인
- 이전 성공한 KIS 분봉·시장 체결 API 호출 이후 새로운 프로세스에서 토큰 발급 403. **이번 코드의 실제 새 발급과 장중 3종 캡처 성공 여부는 아직 미검증**.
- 신규 캐시 오프라인 상태: `TOKEN_MISSING_OR_EXPIRED`. 현재 사용 가능한 이전 토큰은 새 개인 캐시에 없음. 토큰 재발급을 강제/반복하지 않음.
- 합성 테스트에서는 교차 실행 재사용, 키 변경 분리, 만료, 403 단발 요청+재시도 억제, 동시 요청 잠금, 소유자 전용 권한, 심볼릭 링크 거부, 민감정보 비노출 등을 검증.
- 장중 3종 실제 시장 원본이 추가 확보되지 않은 상태이므로 예전 100거래일 전략의 실제 체결가/청산/수익률을 갱신하지 않음.

## 다음 거래일 점검
장중 09:00~15:20 KST에 새로운 연구 출력 폴더로 **읽기 전용** 수집하고, 3종 간 동일 분의 체결/가격 일관성을 검증한다. 단, 새 세션에서 인증 문제가 다시 발생하면 반복 재발급을 피하고 종료한다.

```bash
cd /home/hong/apps/FreeQuantLab
set -a
source ~/.config/freequantlab/kis.env
set +a
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
R=/home/hong/apps/FreeQuantLab/state/intraday_research

$PY -m quantlab.kis_token_cache --status  # 무통신; 토큰 출력하지 않음

# 아직 현재 운영 워크트리에는 미배포이므로,
# 병합된 연구 버전의 실행 경로에서 아래 명령을 수행해야 함.
$PY -m quantlab.intraday_krx_joint_evidence \
  --capture-dir "$R/krx_joint_quote_tick_min_NEXT_SESSION_r1" \
  --count 2 --execute

$PY -m quantlab.intraday_krx_joint_evidence \
  --source-dir "$R/krx_joint_quote_tick_min_NEXT_SESSION_r1" \
  --output-dir "$R/krx_joint_quote_tick_min_NEXT_SESSION_audit_r1"
```

`main` 병합만으로 기존 실행형 운영 워크트리가 자동 업데이트되지는 않는다. 90일 운영 평가를 연구 기능으로 덮어쓰지 않는다. 계정 사용자가 다음 거래일 장중 연구용 worktree에서 실행하거나 별도 승인된 비거래 수집 서비스에만 연결한다.
