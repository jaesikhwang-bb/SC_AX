# 4. 복합환산 실제 데이터 조회

복합환산 detail은 로그인한 카드 모집인 본인의 환산점수·실적 건수·미반영
내역을 실제로 조회한다. RP 서비스의 신청 방법, 처리 절차, 변경, 해지, 이용
기준 또는 제한사항을 안내하는 문서 질문에는 선택하지 않는다.

고객 개인의 연결 상태·접수·매출 조회는 RP_FIXED_GUIDANCE이며 복합환산으로
대체하지 않는다. 고객별 환산점수 반영은 PERFORMANCE_FEE 고정 안내 업무다.
이 RP 복합환산 detail로 고객 개인 데이터를 조회하지 않는다.

## COMPOSITE_CONVERSION_SCORE

다음 실제 수치 조회 표현이 질문에 명확히 있을 때만 선택한다.

- 환산점수
- 복합환산 점수
- RP 실적
- 실적 건수
- 복합판매 구분별 실적

예시:

- `이번 달 RP 환산점수를 보여줘`
- `지난달 복합판매 구분별 실적 건수를 조회해줘`
- `내 RP 실적을 알려줘`

파라미터:

- `closing_year_month`
- `reference_date`
- reference_date가 있으면 closing_year_month는 null이다.

## COMPOSITE_CONVERSION_EXCLUDED

다음 실제 제외 내역 조회 표현이 질문에 명확히 있을 때만 선택한다.

- 환산 미반영
- 실적 제외
- 반영되지 않은 이유
- 자동이체 미연결
- 자동차 연계
- 탈회
- 삼성전자 매출

예시:

- `지난달 RP 미반영 내역을 보여줘`
- `자동이체 미연결로 실적에서 제외된 내역을 알려줘`
- `내 복합환산이 왜 반영되지 않았는지 알려줘`

파라미터:

- `closing_year_month`

## 복합환산으로 선택하지 않는 질문

- `도시가스 자동납부 신청절차를 알려줘`
  → `CITY_GAS_AUTOPAY_GUIDE`
- `4대 사회보험 자동납부 기준을 알려줘`
  → `SOCIAL_INSURANCE_AUTOPAY_GUIDE`
- `아파트 관리비 자동납부 해지 방법을 알려줘`
  → `APARTMENT_MANAGEMENT_FEE_AUTOPAY_GUIDE`
- `후불하이패스 카드 신청 방법을 알려줘`
  → `SAMSUNG_POSTPAID_HIPASS_CARD_GUIDE`

`RP`라는 단어 또는 RP 에이전트가 선택된 상태만으로 복합환산 코드를 선택하지
않는다.
