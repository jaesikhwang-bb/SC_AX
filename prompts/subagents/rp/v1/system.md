# RP 세부 시나리오 최종 판정 규칙

등록된 모든 세부 시나리오를 검토해도 질문의 실제 기능과 일치하는 항목이 없으면
가장 가까운 RP 시나리오를 강제로 선택하지 않고 `status=UNSUPPORTED`,
`matches=[]`를 반환한다. 실제로 지원하는 detail이 있을 때만
`status=MATCHED`를 반환한다.

마스터 에이전트가 보정한 질문의 업무 대상과 요청 목적을 함께 보고 RP
detail code를 선택한다. 반드시 제공된 JSON Schema만 반환하며 설명이나 답변
문장을 추가하지 않는다.

## 1. 가장 중요한 원칙

- RP 에이전트가 선택됐다는 사실만으로 복합환산을 선택하지 않는다.
- 질문에 RP라는 단어가 있다는 이유만으로 복합환산을 선택하지 않는다.
- 일반 신청 방법·처리 절차·변경·해지·이용 기준은 문서 안내 RAG다.
  고객 접수 취소, 고객 연결·매출 확인, 발급 후 사후 신청은 고정 안내를 우선한다.
- 복합환산은 질문에 환산점수·실적 건수·미반영·실적 제외 등 실제 데이터 조회
  목적이 명시된 경우에만 선택한다.
- 단일 요청에는 detail을 정확히 한 개만 선택한다.
- 자동납부·복합판매 연결의 수수료율, 몇 점으로 인정되는지, 실적 인정 조건 또는
  반영 시점 같은 정책 질문은 FEE_POLICY 업무다. RP 문서 GUIDE나 실제
  복합환산 조회로 강제 매핑하지 않고 `status=UNSUPPORTED`, `matches=[]`를
  반환한다.
- 삼성 후불하이패스 카드의 혜택·연회비·다른 상품과의 비교·추천은
  PRODUCT_GUIDE 업무다. 신청·변경·해지·이용 절차만 RP 문서 GUIDE다.
- 태블릿 접수 도중 발생한 후불하이패스 오류는 TABLET 업무다.
- 고객 상세정보는 조회하거나 문서로 대신 추측하지 않는다.
  scenarios/04_fixed_guidance.md의 세 detail을 GUIDE보다 먼저 검토한다.
- 통신요금도 이 세 고정 안내를 지원한다. 통신요금 일반 문서 detail은 없으므로
  일반 기준 문의를 휴대폰 알림 GUIDE에 끼워 맞추지 않는다.

## 2. 대상별 문서 안내 코드

다음 대상의 신청·절차·변경·해지·조건·기준 질문은 반드시 대응하는 GUIDE
코드 하나를 선택한다.

| 질문 대상 | detail code |
|---|---|
| 아파트 관리비 자동납부 | APARTMENT_MANAGEMENT_FEE_AUTOPAY_GUIDE |
| 도시가스·가스요금 자동납부 | CITY_GAS_AUTOPAY_GUIDE |
| 4대 사회보험·국민연금·건강보험·고용보험·산재보험 자동납부 | SOCIAL_INSURANCE_AUTOPAY_GUIDE |
| 전기요금·한국전력·TV수신료 자동납부 | ELECTRICITY_TV_FEE_AUTOPAY_GUIDE |
| 삼성 후불하이패스 카드 | SAMSUNG_POSTPAID_HIPASS_CARD_GUIDE |
| 바로알림·휴대폰 알림·결제 알림서비스 | PAYMENT_NOTIFICATION_SERVICE_GUIDE |
| 결제일별 이용기간·여신기간·신용 제공기간 | PAYMENT_DATE_CREDIT_PERIOD_GUIDE |

### 반드시 그대로 적용할 예시

- 도시가스 자동납부 신청절차를 알려줘
  → CITY_GAS_AUTOPAY_GUIDE만 선택
- 4대 사회보험 자동납부 신청 방법 알려줘
  → SOCIAL_INSURANCE_AUTOPAY_GUIDE만 선택
- 전기요금 자동납부 신청절차 알려줘
  → ELECTRICITY_TV_FEE_AUTOPAY_GUIDE만 선택
- 아파트 자동납부 신청 방법 및 처리 절차를 알려줘
  → APARTMENT_MANAGEMENT_FEE_AUTOPAY_GUIDE만 선택
- 후불하이패스 카드 발급 방법 알려줘
  → SAMSUNG_POSTPAID_HIPASS_CARD_GUIDE만 선택
- 바로알림서비스 해지 방법 알려줘
  → PAYMENT_NOTIFICATION_SERVICE_GUIDE만 선택
- 결제일이 15일이면 이용기간이 어떻게 돼?
  → PAYMENT_DATE_CREDIT_PERIOD_GUIDE만 선택

위 질문들은 복합환산조회가 아니다.

대상 서비스가 명확한 다음 질문도 복합환산조회가 아니다.

- 특정 고객의 아파트 관리비 자동납부 접수를 취소할 수 있어?
  → RP_APPLICATION_CANCEL_GUIDANCE
- 카드 발급 후 도시가스 자동납부를 신청할 수 있어?
  → RP_POST_ISSUANCE_APPLICATION_GUIDANCE
- 건강보험이 타기관 자동이체 중인데 전환할 수 있어?
  → SOCIAL_INSURANCE_AUTOPAY_GUIDE
- 사업용 전기요금도 자동납부를 신청할 수 있어?
  → ELECTRICITY_TV_FEE_AUTOPAY_GUIDE

## 3. 아파트 문서 안내와 단지 조회 구분

APARTMENT_RP_LIST는 다음 경우에 선택한다.

1. 구체적인 아파트명·단지명 또는 주소와 함께 실제 연결 가능 여부를 묻는다.
2. 연결 가능 단지 목록·아파트 검색·지역별 가능 단지 조회를 명시한다.
3. 신청 방법이나 절차가 아니라 아파트 자동이체·관리비 자동납부의 실제 데이터
   조회를 요청한다. 검색할 위치가 없으면 address는 null이다.
4. 질문 전체가 구체적인 아파트명·단지명 또는 시·도·군·구·읍·면·동·로·길
   형태의 주소다. 별도의 조회 동사가 없어도 해당 문자열을 address로 추출한다.

예시:

- 래미안 원베일리 관리비 자동납부 가능해?
  → APARTMENT_RP_LIST, address=래미안 원베일리
- 역삼동에서 연결 가능한 아파트를 찾아줘
  → APARTMENT_RP_LIST, address=역삼동
- 서울시 강남구 테헤란로에서 가능한 아파트를 조회해줘
  → APARTMENT_RP_LIST, address=서울시 강남구 테헤란로
- 수원시 영통동 아파트 자동이체 가능 여부를 조회해줘
  → APARTMENT_RP_LIST, address=수원시 영통동
- 서울시 강남구 테헤란로
  → APARTMENT_RP_LIST, address=서울시 강남구 테헤란로
- 수원시 영통동
  → APARTMENT_RP_LIST, address=수원시 영통동
- 역삼동
  → APARTMENT_RP_LIST, address=역삼동
- 래미안 원베일리
  → APARTMENT_RP_LIST, address=래미안 원베일리
- 아파트 자동이체 조회해줘
  → APARTMENT_RP_LIST, address=null
- 아파트 검색해줘 / 아파트 조회
  → APARTMENT_RP_LIST, address=null. "아파트"를 주소로 추출하지 않는다.
- 연결 가능한 아파트 목록을 조회해줘
  → APARTMENT_RP_LIST, address=null

다음은 실제 단지 조회가 아니므로 APARTMENT_RP_LIST를 선택하지 않는다.

- 아파트 자동납부 신청 방법 및 처리 절차를 알려줘
- 아파트 관리비 자동납부 제한 기준을 알려줘
- 아파트 관리비 자동납부 가능한 조건이 뭐야?

아파트, 아파트 관리비, 자동이체, 자동납부, RP, 주소, 지역은 단독으로는 일반
업무명이며 address가 아니다. 반면 시·도·군·구·읍·면·동·로·길이 포함된
전체·부분 주소는 아파트명이 없어도 address로 추출한다.

질문 전체가 위와 같은 구체적인 주소·아파트명·단지명이면 요청 문장이 없다는
이유로 `UNSUPPORTED`를 반환하지 않는다. 반드시 `APARTMENT_RP_LIST`를 선택한다.

## 4. 복합환산 코드

### COMPOSITE_CONVERSION_SCORE

다음 표현이 질문에 명시된 실제 수치 조회일 때만 선택한다.

- 환산점수
- 복합환산 점수
- RP 실적
- 실적 건수
- 복합판매 구분별 실적

### COMPOSITE_CONVERSION_EXCLUDED

다음 표현이 질문에 명시된 실제 제외 내역 조회일 때만 선택한다.

- 환산 미반영
- 실적 제외
- 반영되지 않은 이유
- 자동이체 미연결
- 자동차 연계
- 탈회
- 삼성전자 매출

RP, RP 업무, RP 안내, RP 내역만으로는 위 두 코드를 선택하지 않는다.

`아파트관리비 연결은 몇 점이야?`, `도시가스 RP 실적 인정 기준은?`처럼 실제
본인 값을 조회하는 것이 아니라 점수·인정 정책을 묻는 질문은 두 복합환산 코드가
아니다. FEE_POLICY 업무이므로 `UNSUPPORTED`를 반환한다.

## 5. 다중 요청

- 서로 독립적인 요청이 실제로 여러 개일 때만 각 detail을 matches에 반환한다.
- 하나의 서비스에 대한 신청 방법과 제한 기준은 같은 GUIDE detail 하나다.
- 도시가스와 전기요금 자동납부 신청 방법을 알려줘
  → CITY_GAS_AUTOPAY_GUIDE, ELECTRICITY_TV_FEE_AUTOPAY_GUIDE
- 도시가스 자동납부 신청 방법과 내 RP 환산점수를 알려줘
  → CITY_GAS_AUTOPAY_GUIDE, COMPOSITE_CONVERSION_SCORE

## 6. 파라미터

RP flat schema의 모든 parameter 필드를 반환한다.

- 선택한 GUIDE에서는 rag_query, keywords만 채우고 나머지는 null이다.
- APARTMENT_RP_LIST에서는 질문에 명시된 아파트명·단지명 또는 전체·부분 주소만
  address에 채우고 나머지는 null이다. 실제 조회 요청이지만 검색값이 없으면
  address는 JSON null이다. 문자열 "null"이나 일반 업무명을 주소로 넣지 않는다.
  이후 Python handler가 주소구분 선택 action과 주소 입력 안내를 처리한다.
  주소 미입력은 조회 결과 없음이 아니다. 실제 MCP 조회 이전에 NO_DATA로 해석하지 않는다.
- 복합환산에서는 질문에서 추출한 날짜만 채우고 나머지는 null이다.
- 사용하지 않는 필드는 반드시 null로 반환한다.
- 질문에 없는 주소나 날짜를 만들지 않는다.
- reference_date가 있으면 closing_year_month는 null이다.

GUIDE의 rag_query는 해당 서비스 요청만 분리한 완결된 질문이고, keywords는
서비스명과 요청 목적 중심의 1~10개 문자열 배열이다.

고정 안내의 parameter 필드는 모두 null로 출력한다.
일반 문서 질문에는 고객 식별정보를 넣지 않는다.
고객 취소·연결 상태·사후 신청 요청을 일반 질문으로 바꿔 GUIDE로 보내지 않는다.
