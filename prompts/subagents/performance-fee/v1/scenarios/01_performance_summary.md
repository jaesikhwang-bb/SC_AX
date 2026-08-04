# 1. 실적 종합조회

로그인한 카드 모집인 본인의 전체 환산점수와 실적건수를 종합적으로 조회하려는
질문이다. 다른 모집인의 실적을 조회하지 않는다.

## PERFORMANCE_SUMMARY_TOTAL

- 전체 실적, 종합 실적, 환산점수와 실적건수 조회
- 별도 한정 없이 "내 실적", "실적이 궁금해"라고 묻는 일반 실적 조회
- 같은 질문에 수수료 조회도 있으면 이 세부 시나리오를 유지한 채 수수료
  세부 시나리오를 별도 match로 추가한다.
- 파라미터: closing_year_month, reference_date
- 두 날짜가 모두 없으면 애플리케이션이 closing_year_month를 당월로 설정한다.
- reference_date가 있으면 closing_year_month는 null이다.
