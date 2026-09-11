# 고객 상세 문의 고정답변 관리

## 원칙과 실행 경로

고객별 실제 상세정보를 조회하거나 추측하여 제공하지 않는다. 고객이라는 단어만으로
PERFORMANCE_FEE에 몰아넣지 않고 마스터가 업무 영역을 선택한 뒤 서브에이전트 LLM이
문의 목적에 맞는 detail을 선택한다. 질문 문자열을 Python 조건문으로 비교하지 않는다.

`마스터 분류 → 서브에이전트 detail 분류 → 고정답변 등록 확인 → MCP 생략 → 기존 답변 출력`

고정 안내도 기존 출력 가드레일과 SSE 경로를 사용한다. 일반 문서 질문과 모집인 본인의
집계 조회는 유지한다. 에이전트 전환 확인이 필요한 경우 기존 SWITCH_AGENT가 먼저 동작한다.

## 문구 수정 위치

`app/subagents/fixed_responses.py`의 `SUBAGENT_FIXED_RESPONSES`에서
`(agent_code, detail_scenario_code)`에 해당하는 `default_message`를 수정한다.
모집인 구분별 문구는 기존 `messages_by_recruitment_org_type`으로 설정할 수 있다.

| 에이전트 | 문의 | detail |
|---|---|---|
| PERFORMANCE_FEE | 폐기·회입 사유·대상·시기 | CUSTOMER_DISPOSAL_REASON_REQUEST |
| PERFORMANCE_FEE | 고객 자동차·삼성전자 매출 영향 | CUSTOMER_SALES_PERFORMANCE_REQUEST |
| PERFORMANCE_FEE | 고객 카드 탈회 | CUSTOMER_WITHDRAWAL_REQUEST |
| PERFORMANCE_FEE | 고객별 점수·환산 반영 | CUSTOMER_CONVERSION_REQUEST |
| PERFORMANCE_FEE | 미등록·미수령 고객 식별 | CUSTOMER_UNREGISTERED_IDENTITY_REQUEST |
| PERFORMANCE_FEE | 수수료 항목 설명·이상 문의 | FEE_DETAIL_GUIDANCE |
| PERFORMANCE_FEE | 장기·작년 수수료 내역 | FEE_LONG_TERM_HISTORY_GUIDANCE |
| PERFORMANCE_FEE | 지급일 | FEE_PAYMENT_DATE_GUIDANCE |
| PERFORMANCE_FEE | 일반 수수료 팩스 | FEE_FAX_UNAVAILABLE_GUIDANCE |
| RP | 고객 접수 취소 | RP_APPLICATION_CANCEL_GUIDANCE |
| RP | 고객 연결·매출·접수 상태 | RP_CUSTOMER_PAYMENT_GUIDANCE |
| RP | 사후 신청·고객 대신 접수 | RP_POST_ISSUANCE_APPLICATION_GUIDANCE |
| QUALIFICATION | 고객 소득증빙 대상·가처분 | PROVISIONAL_DISPOSITION_GUIDANCE |

## 분류 수정 위치

- 업무 간 경계: `prompts/intent-classification/v1/router/system.md`, `agents/*.md`
- 코드 정의: 각 `prompts/subagents/<업무>/v1/manifest.yaml`
- 최종 판정: 각 업무의 `system.md`
- PERFORMANCE_FEE 상세: `scenarios/06_information_inquiry.md`, `07_access_restriction.md`
- RP 상세: `scenarios/04_fixed_guidance.md`

고정답변 detail은 manifest의 parameters를 빈 목록으로 선언한다. RP flat 구조화 출력은
사용하지 않는 필드를 null로 생성하고, 선택 이후에는 빈 parameters로 정리한다.

## 반드시 구분할 테스트

- 기타수수료가 뭐예요 → 고정 안내 / 이번 달 기타수수료 얼마야 → 실제 조회
- 고객 관리비 환산 반영됐어 → 실적 고정 안내 / 고객 관리비 어느 카드로 연결됐어 → RP 고정 안내
- 도시가스 신청절차 알려줘 → RAG / 카드 발급 후 도시가스 신청 가능해 → 사후 신청 고정 안내
- 신규입회 자격기준 → RAG / 이 고객 소득증빙 대상이야 → 자격기준 고정 안내
- 수수료 팩스 보내줘 → 불가 안내 / 원천징수 팩스 보내줘 → 기존 실제 전송
- 폐기 사유가 뭐야 → 고정 안내 / 이번 달 폐기 건수와 회입수수료 → 실제 조회

테스트는 코드 등록, 파라미터 계약, MCP 생략을 검증한다. 실제 LLM의 표현별 분류 정확도는
배포 모델로 별도 확인해야 한다. 프롬프트 변경 후 서버를 재시작하여 새 대화로 확인한다.
