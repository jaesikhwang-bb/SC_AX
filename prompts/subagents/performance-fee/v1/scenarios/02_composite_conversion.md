# 2. 복합환산조회

로그인한 카드 모집인 본인의 복합판매 구분코드별 환산점수·실적건수 또는 본인
실적에 반영되지 않은 발급 내역을 조회하려는 질문이다.

## COMPOSITE_CONVERSION_SCORE

- 복합판매 구분코드별 환산점수와 실적 건수 조회
- `RP 실적`, `RP 환산점수`, `RP 복합환산 점수`, `RP 내역`처럼 RP 실적
  데이터를 묻는 표현도 이 세부 시나리오로 분류한다.
- `아파트 점수`, `전기요금 환산`, `통신 몇 점`, `도시가스 몇 점`,
  `4대보험 환산`처럼 로그인한 모집인 본인의 특정 RP 항목별 실제 점수·건수를
  묻는 질문도 이 세부 시나리오로 분류한다.
- 파라미터: closing_year_month, reference_date
- reference_date가 있으면 LLM은 closing_year_month를 null로 반환하고,
  애플리케이션의 최종 조회 파라미터는 빈 문자열 `""`로 변환한다.

## COMPOSITE_CONVERSION_EXCLUDED

- 자동이체 미연결, 자동차 연계, 탈회, 삼성전자 매출 등 환산 미반영 내역
- `RP 미반영 내역`, `RP 실적 제외 내역`, `RP가 왜 반영되지 않았는지`도 이
  세부 시나리오로 분류한다.
- `점수가 다 안 뜬 것 같아`, `실적 빠진 것 있어`, `계좌 미연결`, `무통장`,
  `자동이체 안 된 건 있어`처럼 본인의 환산 누락·미반영 건수를 묻는 구어체도
  이 세부 시나리오다.
- 파라미터: closing_year_month

특정 고객을 식별하여 자동차·삼성전자 매출의 실적 반영 여부를 묻는 질문은
`CUSTOMER_SALES_PERFORMANCE_REQUEST`, 고객의 폐기·회입 사유는
`CUSTOMER_DISPOSAL_REASON_REQUEST`, 카드 탈회는 `CUSTOMER_WITHDRAWAL_REQUEST`,
고객별 RP 환산 반영은 `CUSTOMER_CONVERSION_REQUEST`를 선택한다.
고객 이름이 없어도 발급 후 자동차·삼성전자 구매의 실적 영향·구매 시기 문의는
`CUSTOMER_SALES_PERFORMANCE_REQUEST` 고정 안내다.
