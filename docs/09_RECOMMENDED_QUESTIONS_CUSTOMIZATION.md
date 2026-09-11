# 세부 시나리오별 추천질문 커스터마이징 가이드

추천질문은 활성 서브에이전트 manifest에서 관리한다. 프론트는 클릭한
`question` 문장을 그대로 일반 질문으로 전송한다. 서버는 추천질문 ID나
'네'를 이전 추천질문에 자동 연결하지 않으며 보정·마스터·서브 분류를 모두 거친다.

## 1. 추천질문 설정

해당 agent의 활성 `manifest.yaml`에서 detail의 문자열 배열을 수정한다.
버튼 문구는 그대로 전송되므로 완전한 사용자 요청 형태로 작성한다.

```yaml
recommended_questions:
  - "원천징수 내역을 팩스로 보내줘"
  - "전년도 원천징수 내역도 보여줘"
```

필드 생략, null, 빈 배열은 추천질문 없음이다. 단일 문자열과 `{question: "..."}`
형식도 읽을 수 있다. 빈 문구·잘못된 항목은 무시하고 같은 detail의 중복 문구는
첫 항목만 유지한다. 질문 개수 제한은 없으며 matches 순서와 YAML 순서를 따른다.
설정 변경 후 서버를 재시작한다.

## 2. SSE 출력

정상 PASS 답변에 추천질문이 있으면 `messages → recommendedQuestions → duration → end`
순서로 제공한다. 질문이 없으면 별도 추천질문 이벤트를 생략한다.
assistant 메시지 metadata에도 같은 추천질문 배열을 보관하여 화면 복원에 사용한다.
질문 보정과 RAG 답변용 이력 조회에서는 이 metadata를 읽지 않는다.

```json
{
  "id": "PERFORMANCE_FEE:WITHHOLDING_TAX:1",
  "question": "원천징수 내역을 팩스로 보내줘",
  "interactionType": "prompt",
  "agentCode": "PERFORMANCE_FEE",
  "promptVersion": "v1",
  "scenarioCode": "FEE_DETAILS",
  "detailScenarioCode": "WITHHOLDING_TAX"
}
```

`id`는 화면 렌더링 키다. `detailScenarioCode`는 이 추천질문이 표시된 원래 detail을
뜻하며 클릭 후 실행할 detail을 강제 지정하는 값이 아니다.
`interactionType`은 항상 `prompt`이며 별도 확인 버튼 유형은 없다.

## 3. 프론트 클릭 처리

```javascript
button.textContent = item.question;
button.addEventListener("click", () => {
  sendChat({
    message: item.question,
    session_id: currentSessionId,
    thread_id: null,
    endpoint: currentEndpoint,
    agent_code: currentAgentCode,
    humanInput: [],
    user: currentUser,
  });
});
```

현재 session·선택 agent를 유지하고, 새 일반 질문이므로 thread_id는 null,
humanInput은 빈 배열로 보낸다. recommendation_id는 보내지 않는다.
같은 서브에이전트의 이전 조건이 필요한 경우 일반 질문 보정 단계에서만 보완한다.
응답 중에는 중복 클릭을 막는다.

팩스 요청도 일반 분류를 거쳐 해당 시나리오로 이동한다. 팩스번호가 필요하면
기존 action/humanInput 흐름으로 입력받는다. 이 action 입력 흐름은 유지된다.
단독 '네'는 추천질문의 실행 승인이 아니며 일반 질문처럼 처리한다.

## 4. 확인할 코드와 테스트

- 추천질문 생성: `app/recommended_questions.py`
- 출력: `app/api.py`의 `_recommended_questions_for_result`
- 클릭: `static/chatting.html`, `static/intent_tester.html`
- 문장 기반 분류: `app/graph.py`의 `_classify_intent`, `_run_subagent`
- 회귀 검증: `tests/test_master_context_routing.py`, `tests/test_scenario_actions.py`

기존 추천질문 전용 자동 연결·재선택·세부 시나리오 강제 선택 코드는 제거했다.
기존 클라이언트가 recommendation_id를 보내더라도 요청 모델의 extra=ignore 정책에
따라 무시되며 실제 message 문장만 처리한다.
