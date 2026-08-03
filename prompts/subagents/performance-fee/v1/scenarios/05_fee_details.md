# 5. 수수료 내역 조회

로그인한 카드 모집인 본인이 실제로 지급받은 수수료·수당, 부과된 세금과 공제,
최종 실지급액, 장기 지급 추이 또는 원천징수 내역을 조회하려는 질문이다.
지점장이 다른 모집인의 지급 내역을 조회하는 업무가 아니다.

## FEE_ITEM_DETAILS

- 본인에게 지급된 항목별 수수료 또는 수당 내역
- 파라미터: closing_year_month

## FEE_TAX_NET_PAYMENT

- 본인 수수료에 부과된 항목별 세금·공제와 실제 지급액·입금액
- 파라미터: closing_year_month

## FEE_12_MONTH_TREND

- 본인이 실제 지급받은 최근 12개월 또는 1년 수수료 변화와 추이
- 파라미터: closing_year_month

## WITHHOLDING_TAX

- 본인에게 지급된 수수료의 연도별 원천징수 내역
- 파라미터: reference_year
