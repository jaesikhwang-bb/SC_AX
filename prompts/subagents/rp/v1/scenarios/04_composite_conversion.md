# 4. 복합환산조회

RP 화면에서 로그인한 카드 모집인 본인의 복합판매 구분코드별 환산점수·실적
건수 또는 실적에 반영되지 않은 내역을 조회하려는 질문이다.

## COMPOSITE_CONVERSION_SCORE

- 복합판매 구분코드별 환산점수와 실적 건수 조회
- 파라미터: closing_year_month, reference_date
- reference_date가 있으면 closing_year_month는 null이다.

## COMPOSITE_CONVERSION_EXCLUDED

- 자동이체 미연결, 자동차 연계, 탈회, 삼성전자 매출 등 환산 미반영 내역 조회
- 파라미터: closing_year_month
