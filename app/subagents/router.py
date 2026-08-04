"""manifest 기반 Structured Output 시나리오 서브에이전트 실행기."""

from datetime import date, timedelta
from enum import Enum
from typing import Any, Literal, Protocol

from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, create_model

from app.config import Settings
from app.observability import async_timed_block, logger, timed
from app.subagents.models import SubagentResult, SubagentScenarioMatch
from app.subagents.prompt_loader import (
    ScenarioPromptBundle,
    SubagentPromptLoader,
)


class SubagentRouter(Protocol):
    """마스터 그래프가 구체적인 서브에이전트 구현과 무관하게 쓰는 계약."""

    def supports(self, agent_code: str) -> bool: ...

    def registered_codes(self) -> tuple[str, ...]: ...

    async def classify(
        self,
        *,
        agent_code: str,
        query: str,
    ) -> SubagentResult | None: ...


class EmptySubagentRouter:
    """아직 구현되지 않은 에이전트와 단위 테스트를 위한 빈 라우터."""

    def supports(self, agent_code: str) -> bool:
        return False

    def registered_codes(self) -> tuple[str, ...]:
        return ()

    async def classify(
        self,
        *,
        agent_code: str,
        query: str,
    ) -> SubagentResult | None:
        return None


class ScenarioSubagent:
    """한 manifest의 시나리오·세부 시나리오·파라미터를 분류한다."""

    @timed("시나리오 서브에이전트 초기화")
    def __init__(
        self,
        settings: Settings,
        bundle: ScenarioPromptBundle,
    ) -> None:
        if not settings.genos_bearer_token:
            raise ValueError("GENOS_BEARER_TOKEN 환경변수가 필요합니다.")

        self._bundle = bundle
        self._output_model = _create_output_model(bundle)
        self._scenario_by_code = {
            str(scenario["code"]): scenario
            for scenario in bundle.manifest["scenarios"]
        }
        self._detail_by_code: dict[str, tuple[dict, dict]] = {
            str(detail["code"]): (scenario, detail)
            for scenario in bundle.manifest["scenarios"]
            for detail in scenario["details"]
        }

        prompt = ChatPromptTemplate.from_messages(
            [
                SystemMessage(content=bundle.system_prompt),
                (
                    "human",
                    "오늘 날짜: {today}\n\n"
                    "마스터 에이전트가 보정한 사용자 질문:\n{query}\n\n"
                    "질문에 서로 독립적인 요청이 여러 개 있으면 이 서브에이전트 "
                    "안에서 해당하는 모든 세부 시나리오를 matches 배열에 "
                    "한 번씩 선택하세요. 단일 요청이면 matches에는 한 개만 "
                    "반환하세요. 다른 서브에이전트의 시나리오는 선택하지 마세요.",
                ),
            ]
        )
        llm = ChatOpenAI(
            base_url=settings.genos_openai_base_url,
            model=settings.genos_model,
            api_key=settings.genos_bearer_token,
            temperature=bundle.temperature,
        )
        self._chain = prompt | llm.with_structured_output(
            self._output_model,
            method="json_schema",
            strict=True,
        )
        logger.info(
            "======== 시나리오 서브에이전트 준비 완료 | 에이전트=%s | "
            "프롬프트버전=%s | 시나리오개수=%d | 세부시나리오개수=%d",
            bundle.agent_code,
            bundle.version,
            len(self._scenario_by_code),
            len(self._detail_by_code),
        )

    @timed("서브에이전트 시나리오 분류")
    async def classify(
        self,
        query: str,
        *,
        today: date | None = None,
    ) -> SubagentResult:
        """LLM 분류 후 manifest 규칙으로 누락 파라미터 기본값을 확정한다."""

        reference_date = today or date.today()
        logger.info(
            "======== 서브에이전트 LLM 전달 | 에이전트=%s | "
            "기준일=%s | 질문길이=%d",
            self._bundle.agent_code,
            reference_date.isoformat(),
            len(query),
        )
        # 마스터와 마찬가지로 전체 프롬프트는 운영 로그에 출력하지 않는다.
        async with async_timed_block("서브에이전트 LLM 응답 대기"):
            structured = await self._chain.ainvoke(
                {
                    "today": reference_date.isoformat(),
                    "query": query,
                }
            )

        matches: list[SubagentScenarioMatch] = []
        seen_detail_codes: set[str] = set()
        for selected in structured.matches:
            selected_scenario_code = selected.scenario_code.value
            detail_code = selected.detail_scenario_code.value
            if detail_code in seen_detail_codes:
                logger.info(
                    "======== 서브에이전트 중복 세부시나리오 제거 | 코드=%s",
                    detail_code,
                )
                continue
            seen_detail_codes.add(detail_code)
            scenario, detail = self._detail_by_code[detail_code]
            # LLM의 최상위 코드가 어긋나면 더 구체적인 세부 시나리오의 실제
            # 부모로 자동 보정한다.
            scenario_code = str(scenario["code"])
            if selected_scenario_code != scenario_code:
                logger.info(
                    "======== 서브에이전트 시나리오 조합 자동 보정 | "
                    "LLM최상위=%s | 세부시나리오=%s | 적용최상위=%s",
                    selected_scenario_code,
                    detail_code,
                    scenario_code,
                )
            matches.append(
                SubagentScenarioMatch(
                    scenario_code=scenario_code,
                    scenario_name=str(scenario["name"]),
                    detail_scenario_code=detail_code,
                    detail_scenario_name=str(detail["name"]),
                    parameters=_normalize_parameters(
                        raw=selected.parameters.model_dump(),
                        manifest=self._bundle.manifest,
                        detail=detail,
                        today=reference_date,
                    ),
                )
            )

        # manifest가 특정 복합 표현에 필수 세부 시나리오 조합을 선언하면
        # LLM이 일부를 누락했더라도 공통 로직으로 보완한다. 업무 용어나 코드는
        # Python에 하드코딩하지 않아 다른 서브에이전트도 같은 기능을 쓸 수 있다.
        normalized_query = query.casefold()
        for rule in self._bundle.manifest.get("required_match_rules", []):
            terms = [str(term).casefold() for term in rule.get("all_terms", [])]
            if not terms or not all(term in normalized_query for term in terms):
                continue
            logger.info(
                "======== 서브에이전트 필수 다중 매칭 규칙 적용 | "
                "에이전트=%s | 규칙=%s",
                self._bundle.agent_code,
                rule.get("name", "이름없음"),
            )
            for required_detail_code in rule.get("detail_codes", []):
                detail_code = str(required_detail_code)
                if detail_code in seen_detail_codes:
                    continue
                scenario, detail = self._detail_by_code[detail_code]
                seen_detail_codes.add(detail_code)
                matches.append(
                    SubagentScenarioMatch(
                        scenario_code=str(scenario["code"]),
                        scenario_name=str(scenario["name"]),
                        detail_scenario_code=detail_code,
                        detail_scenario_name=str(detail["name"]),
                        parameters=_normalize_parameters(
                            raw={
                                str(name): None
                                for name in self._bundle.manifest[
                                    "parameter_definitions"
                                ]
                            },
                            manifest=self._bundle.manifest,
                            detail=detail,
                            today=reference_date,
                        ),
                    )
                )

        if not matches:
            raise ValueError("서브에이전트는 하나 이상의 시나리오를 선택해야 합니다.")
        primary = matches[0]
        result = SubagentResult(
            agent_code=self._bundle.agent_code,
            prompt_version=self._bundle.version,
            scenario_code=primary.scenario_code,
            scenario_name=primary.scenario_name,
            detail_scenario_code=primary.detail_scenario_code,
            detail_scenario_name=primary.detail_scenario_name,
            parameters=primary.parameters,
            matches=matches,
        )
        logger.info(
            "======== 서브에이전트 분류 완료 | 에이전트=%s | "
            "매칭개수=%d | 세부시나리오=%s",
            result.agent_code,
            len(result.matches),
            [match.detail_scenario_code for match in result.matches],
        )
        return result


class ManifestSubagentRouter:
    """registry에 등록된 여러 시나리오 서브에이전트를 공통 방식으로 실행한다."""

    def __init__(self, agents: dict[str, ScenarioSubagent]) -> None:
        self._agents = agents

    def supports(self, agent_code: str) -> bool:
        return agent_code.upper() in self._agents

    def registered_codes(self) -> tuple[str, ...]:
        return tuple(sorted(self._agents))

    async def classify(
        self,
        *,
        agent_code: str,
        query: str,
    ) -> SubagentResult | None:
        agent = self._agents.get(agent_code.upper())
        if agent is None:
            logger.info(
                "======== 서브에이전트 미등록 | 에이전트=%s | 실행생략",
                agent_code.upper(),
            )
            return None
        return await agent.classify(query)


@timed("서브에이전트 라우터 생성")
def create_subagent_router(settings: Settings) -> SubagentRouter:
    """registry의 활성 서브에이전트를 모두 생성한다."""

    bundles = SubagentPromptLoader().load_all()
    agents = {
        code: ScenarioSubagent(settings, bundle)
        for code, bundle in bundles.items()
    }
    return ManifestSubagentRouter(agents)


def _create_output_model(
    bundle: ScenarioPromptBundle,
) -> type[BaseModel]:
    """manifest 값으로 LLM에 전달할 엄격한 Pydantic JSON Schema를 만든다."""

    scenario_codes = [
        str(scenario["code"])
        for scenario in bundle.manifest["scenarios"]
    ]
    detail_codes = [
        str(detail["code"])
        for scenario in bundle.manifest["scenarios"]
        for detail in scenario["details"]
    ]
    scenario_enum = Enum(
        f"{bundle.agent_code}ScenarioCode",
        {code: code for code in scenario_codes},
        type=str,
    )
    detail_enum = Enum(
        f"{bundle.agent_code}DetailScenarioCode",
        {code: code for code in detail_codes},
        type=str,
    )

    parameter_fields: dict[str, tuple[Any, Field]] = {}
    for name, definition in bundle.manifest[
        "parameter_definitions"
    ].items():
        allowed_values = definition.get("allowed_values")
        parameter_type: Any = str | None
        if allowed_values:
            # manifest의 허용 코드가 JSON Schema enum으로 전달되므로 LLM이
            # 임의의 파라미터 문자열을 생성하지 못하게 한다.
            parameter_type = Literal.__getitem__(
                tuple(str(value) for value in allowed_values) + (None,)
            )
        parameter_fields[str(name)] = (
            parameter_type,
            Field(
                description=str(definition["description"]),
                pattern=definition.get("pattern"),
            ),
        )

    parameters_model = create_model(
        f"{bundle.agent_code}Parameters",
        __config__=ConfigDict(extra="forbid"),
        **parameter_fields,
    )
    match_model = create_model(
        f"{bundle.agent_code}ScenarioMatchOutput",
        __config__=ConfigDict(extra="forbid"),
        scenario_code=(
            scenario_enum,
            Field(description="선택한 최상위 시나리오 코드"),
        ),
        detail_scenario_code=(
            detail_enum,
            Field(description="선택한 세부 시나리오 코드"),
        ),
        parameters=(
            parameters_model,
            Field(description="질문에서 명시적으로 추출한 조회 파라미터"),
        ),
    )
    return create_model(
        f"{bundle.agent_code}ScenarioOutput",
        __config__=ConfigDict(extra="forbid"),
        matches=(
            list[match_model],
            Field(
                min_length=1,
                max_length=len(detail_codes),
                description=(
                    "질문에 포함된 독립 요청과 일치하는 모든 시나리오. "
                    "현재 선택된 서브에이전트의 코드만 사용하고 동일 세부 "
                    "시나리오는 한 번만 반환"
                ),
            ),
        ),
    )


def _normalize_parameters(
    *,
    raw: dict[str, str | None],
    manifest: dict[str, Any],
    detail: dict[str, Any],
    today: date,
) -> dict[str, str | None]:
    """세부 시나리오의 manifest 규칙으로 최종 조회 파라미터를 만든다."""

    # 모든 정의 필드를 null로 시작해 다른 시나리오용 값이 섞이지 않게 한다.
    normalized = {
        str(name): None
        for name in manifest["parameter_definitions"]
    }
    rules = detail.get("parameter_rules", {})
    for name, rule in rules.items():
        extracted = raw.get(name)
        normalized[name] = (
            extracted
            if extracted is not None
            else _resolve_default(str(rule.get("default", "NONE")), today)
        )

    # 기준일자가 있으면 마감작업년월을 null로 만드는 규칙처럼 파라미터 간
    # 우선순위도 manifest가 선언한다.
    for target, trigger in detail.get("clear_when_present", {}).items():
        if normalized.get(trigger) is not None:
            normalized[target] = None
    return normalized


def _resolve_default(policy: str, today: date) -> str | None:
    """manifest의 날짜 기본값 정책을 실제 YYYYMM/YYYY 값으로 변환한다."""

    if policy == "NONE":
        return None
    if policy == "CURRENT_MONTH":
        return today.strftime("%Y%m")
    if policy == "PREVIOUS_MONTH":
        previous_month = today.replace(day=1) - timedelta(days=1)
        return previous_month.strftime("%Y%m")
    if policy == "PREVIOUS_YEAR":
        return str(today.year - 1)
    if policy.startswith("VALUE:"):
        # 날짜가 아닌 분류 코드도 manifest에서 고정 기본값으로 선언할 수 있다.
        # 세부 시나리오와 추출 코드가 서로 어긋나는 것을 방지한다.
        value = policy.removeprefix("VALUE:").strip()
        if not value:
            raise ValueError("VALUE 기본값에는 값이 필요합니다.")
        return value
    raise ValueError(f"지원하지 않는 파라미터 기본값 정책입니다: {policy}")
