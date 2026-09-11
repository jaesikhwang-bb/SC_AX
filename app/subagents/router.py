"""manifest 기반 Structured Output 시나리오 서브에이전트 실행기."""

from datetime import date
from typing import Annotated, Any, Literal, Protocol, Union

from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from app.config import Settings
from app.query_period import month_parameter
from app.observability import (
    async_timed_block,
    create_llm_usage_callback,
    log_failure_diagnostic,
    logger,
    timed,
)
from app.subagents.models import (
    SubagentClassificationStatus,
    SubagentResult,
    SubagentScenarioMatch,
)
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
            str(scenario["code"]): scenario for scenario in bundle.manifest["scenarios"]
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
                    "반환하세요. 다른 서브에이전트의 시나리오는 선택하지 마세요. "
                    "등록된 모든 세부 시나리오를 검토해도 질문의 실제 기능을 "
                    "처리할 항목이 없으면 가장 가까운 항목을 강제로 고르지 말고 "
                    "status=UNSUPPORTED, matches=[]를 반환하세요. 실제로 처리할 "
                    "세부 시나리오가 있을 때만 status=MATCHED를 반환하세요.",
                ),
            ]
        )
        # 시나리오 구조화 출력이 유효한 결과를 못 만들면 응답이 끝나지 않아
        # 평가 행이 통째로 제한시간에 걸린다. 마스터 분류기와 같은 호출 상한을
        # 공유해 그런 실행을 빠르게 끊는다.
        llm = ChatOpenAI(
            base_url=settings.genos_openai_base_url,
            model=settings.genos_model,
            api_key=settings.genos_bearer_token,
            temperature=bundle.temperature,
            callbacks=[
                create_llm_usage_callback(
                    stage=f"subagent.{bundle.agent_code.lower()}.classification",
                    model=settings.genos_model,
                )
            ],
            **settings.llm_client_options,
        )
        self._chain = prompt | llm.with_structured_output(
            self._output_model,
            method="json_schema",
            strict=True,
        )
        self._model = settings.genos_model
        self._endpoint = settings.genos_openai_base_url
        self._max_retries = settings.llm_max_retries
        logger.info(
            "======== 시나리오 서브에이전트 준비 완료 | 에이전트=%s | "
            "프롬프트버전=%s | 시나리오개수=%d | 세부시나리오개수=%d | "
            "자동재시도=%d회 | 코드위치=app/subagents/router.py:"
            "ScenarioSubagent.classify",
            bundle.agent_code,
            bundle.version,
            len(self._scenario_by_code),
            len(self._detail_by_code),
            settings.llm_max_retries,
        )

    @timed("서브에이전트 시나리오 분류")
    async def classify(
        self,
        query: str,
        *,
        today: date | None = None,
    ) -> SubagentResult:
        """LLM으로 시나리오와 원본 조회 파라미터를 분류한다."""

        reference_date = today or date.today()
        # 단위 테스트가 네트워크 생성자 없이 구조화 체인만 주입하는 경우에도
        # 진단 로그가 테스트 자체를 방해하지 않도록 표시용 기본값을 사용한다.
        endpoint = getattr(self, "_endpoint", "테스트 주입 체인")
        model = getattr(self, "_model", "테스트 주입 모델")
        max_retries = getattr(self, "_max_retries", 0)
        logger.info(
            "======== 서브에이전트 LLM 전달 준비 | 에이전트=%s | "
            "코드위치=app/subagents/router.py:ScenarioSubagent.classify | "
            "엔드포인트=%s | 모델=%s | 기준일=%s | 질문길이=%d | "
            "자동재시도=%d회",
            self._bundle.agent_code,
            endpoint,
            model,
            reference_date.isoformat(),
            len(query),
            max_retries,
        )
        # 마스터와 마찬가지로 전체 프롬프트는 운영 로그에 출력하지 않는다.
        try:
            async with async_timed_block("서브에이전트 LLM 응답 대기"):
                logger.info(
                    "======== 서브에이전트 LLM 요청 직전 | 에이전트=%s | "
                    "보정질문길이=%d | 기준일=%s | StructuredOutput=%s",
                    self._bundle.agent_code,
                    len(query),
                    reference_date.isoformat(),
                    self._output_model.__name__,
                )
                structured = await self._chain.ainvoke(
                    {
                        "today": reference_date.isoformat(),
                        "query": query,
                    }
                )
        except Exception as exc:
            log_failure_diagnostic(
                stage=f"{self._bundle.agent_code} 서브에이전트 LLM 호출",
                code_location="app/subagents/router.py:ScenarioSubagent.classify",
                exc=exc,
                likely_cause=(
                    "GenOS LLM 연결·인증 오류 또는 선택된 서브에이전트의 "
                    "시나리오/파라미터 Structured Output 스키마 불일치"
                ),
                corrective_action=(
                    f"prompts/subagents/{self._bundle.agent_code.casefold()}의 "
                    "active.yaml, manifest.yaml, system prompt와 "
                    "app/subagents/router.py:_create_output_model을 확인하세요."
                ),
                retry_count=max_retries,
                context={
                    "agent_code": self._bundle.agent_code,
                    "query_length": len(query),
                    "today": reference_date.isoformat(),
                    "endpoint": endpoint,
                    "model": model,
                },
            )
            raise

        raw_status = str(
            getattr(structured, "status", SubagentClassificationStatus.MATCHED)
        ).upper()
        if (
            raw_status == SubagentClassificationStatus.UNSUPPORTED.value
            or not structured.matches
        ):
            result = SubagentResult(
                agent_code=self._bundle.agent_code,
                prompt_version=self._bundle.version,
                status=SubagentClassificationStatus.UNSUPPORTED,
                matches=[],
            )
            logger.info(
                "FLOW 세부 시나리오 분류 완료 | 에이전트=%s | "
                "분류상태=UNSUPPORTED | 세부시나리오=없음",
                self._bundle.agent_code,
            )
            return result

        matches: list[SubagentScenarioMatch] = []
        seen_detail_codes: set[str] = set()
        for selected in structured.matches:
            selected_scenario_code = str(selected.scenario_code)
            detail_code = str(selected.detail_scenario_code)
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
            skip_codes = {
                str(code) for code in rule.get("skip_if_selected_detail_codes", [])
            }
            selected_skip_codes = seen_detail_codes & skip_codes
            if selected_skip_codes:
                logger.info(
                    "======== 서브에이전트 필수 다중 매칭 규칙 생략 | "
                    "에이전트=%s | 규칙=%s | 이미선택된제외코드=%s",
                    self._bundle.agent_code,
                    rule.get("name", "이름없음"),
                    sorted(selected_skip_codes),
                )
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
                                for name in _detail_parameter_names(
                                    self._bundle.manifest,
                                    detail,
                                )
                            },
                            manifest=self._bundle.manifest,
                            detail=detail,
                        ),
                    )
                )

        if not matches:
            return SubagentResult(
                agent_code=self._bundle.agent_code,
                prompt_version=self._bundle.version,
                status=SubagentClassificationStatus.UNSUPPORTED,
                matches=[],
            )
        # 단일 명시 월은 LLM의 null/당월 추정보다 우선한다. 일자 조회와
        # 다중 기간 질문은 건드리지 않고 해당 월 파라미터가 있는 detail에만 적용한다.
        explicit_month = month_parameter(query, reference_date)
        if explicit_month:
            for match in matches:
                if (
                    "closing_year_month" in match.parameters
                    and not match.parameters.get("reference_date")
                ):
                    match.parameters["closing_year_month"] = explicit_month
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
            "FLOW 세부 시나리오 분류 완료 | 에이전트=%s | "
            "분류상태=MATCHED | 매칭개수=%d | 세부시나리오=%s",
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
        code: ScenarioSubagent(settings, bundle) for code, bundle in bundles.items()
    }
    return ManifestSubagentRouter(agents)


class _ScenarioOutputBase(BaseModel):
    """엄격한 LLM 스키마와 기존 테스트 응답을 함께 수용하는 기반 모델."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_status(cls, value: Any) -> Any:
        # 실제 GenOS 요청의 JSON Schema에서는 status가 필수다. 다만 기존 mock과
        # 저장 결과가 status 도입 전 형식일 수 있어 Python 검증 경계에서만
        # matches 유무로 호환 값을 보완한다.
        if isinstance(value, dict) and "status" not in value:
            copied = dict(value)
            copied["status"] = (
                "UNSUPPORTED" if copied.get("matches") == [] else "MATCHED"
            )
            return copied
        return value


def _create_output_model(
    bundle: ScenarioPromptBundle,
) -> type[BaseModel]:
    """manifest 값으로 LLM에 전달할 엄격한 Pydantic JSON Schema를 만든다."""

    structured_output_mode = str(
        bundle.manifest.get("structured_output", {}).get(
            "mode",
            "discriminated",
        )
    ).strip()
    if structured_output_mode == "flat":
        return _create_flat_output_model(bundle)
    if structured_output_mode != "discriminated":
        raise ValueError(
            "지원하지 않는 structured_output mode입니다: "
            f"agent={bundle.agent_code}, mode={structured_output_mode}"
        )

    detail_codes = [
        str(detail["code"])
        for scenario in bundle.manifest["scenarios"]
        for detail in scenario["details"]
    ]
    match_models: list[type[BaseModel]] = []
    definitions = bundle.manifest["parameter_definitions"]
    for scenario in bundle.manifest["scenarios"]:
        scenario_code = str(scenario["code"])
        for detail in scenario["details"]:
            detail_code = str(detail["code"])
            parameter_fields: dict[str, tuple[Any, Field]] = {}
            for name in _detail_parameter_names(bundle.manifest, detail):
                definition = definitions[name]
                allowed_values = definition.get("allowed_values")
                value_type = str(definition.get("value_type", "string"))
                parameter_type: Any
                if value_type == "string_list":
                    parameter_type = list[str] | None
                elif value_type == "string":
                    parameter_type = str | None
                else:
                    raise ValueError(
                        "지원하지 않는 parameter value_type입니다: "
                        f"name={name}, value_type={value_type}"
                    )
                if allowed_values and value_type == "string":
                    parameter_type = Literal.__getitem__(
                        tuple(str(value) for value in allowed_values) + (None,)
                    )
                field_options: dict[str, Any] = {
                    "description": str(definition["description"]),
                }
                if value_type == "string":
                    field_options["pattern"] = definition.get("pattern")
                elif value_type == "string_list":
                    field_options["min_length"] = int(
                        definition.get("min_items", 1)
                    )
                    field_options["max_length"] = int(
                        definition.get("max_items", 10)
                    )
                parameter_fields[name] = (
                    parameter_type,
                    Field(**field_options),
                )
            safe_detail_name = "".join(
                part.title() for part in detail_code.casefold().split("_")
            )
            parameters_model = create_model(
                f"{bundle.agent_code}{safe_detail_name}Parameters",
                __config__=ConfigDict(extra="forbid"),
                **parameter_fields,
            )
            match_models.append(
                create_model(
                    f"{bundle.agent_code}{safe_detail_name}Match",
                    __config__=ConfigDict(extra="forbid"),
                    scenario_code=(
                        Literal.__getitem__(scenario_code),
                        Field(description="선택한 최상위 시나리오 코드"),
                    ),
                    detail_scenario_code=(
                        Literal.__getitem__(detail_code),
                        Field(description="선택한 세부 시나리오 코드"),
                    ),
                    parameters=(
                        parameters_model,
                        Field(
                            description=(
                                "선택한 세부 시나리오에서 허용된 파라미터만 추출"
                            )
                        ),
                    ),
                )
            )

    match_union = Union.__getitem__(tuple(match_models))
    discriminated_match = Annotated[
        match_union,
        Field(discriminator="detail_scenario_code"),
    ]
    return create_model(
        f"{bundle.agent_code}ScenarioOutput",
        __base__=_ScenarioOutputBase,
        status=(
            Literal["MATCHED", "UNSUPPORTED"],
            Field(
                description=(
                    "질문의 실제 기능과 일치하는 세부 시나리오가 있으면 MATCHED, "
                    "모든 세부 시나리오를 검토해도 없으면 UNSUPPORTED"
                ),
            ),
        ),
        matches=(
            list[discriminated_match],
            Field(
                min_length=0,
                max_length=len(detail_codes),
                description=(
                    "질문에 포함된 독립 요청과 일치하는 모든 시나리오. "
                    "현재 선택된 서브에이전트의 코드만 사용하고 동일 세부 "
                    "시나리오는 한 번만 반환. status=UNSUPPORTED이면 빈 배열"
                ),
            ),
        ),
    )


def _create_flat_output_model(
    bundle: ScenarioPromptBundle,
) -> type[BaseModel]:
    """많은 detail을 단순 enum 하나로 분류하는 구조화 출력 모델을 만든다.

    RP처럼 detail별 discriminated union이 너무 커지는 에이전트만 manifest에서
    명시적으로 사용한다. LLM은 모든 파라미터를 nullable 필드로 반환하고,
    ``_normalize_parameters``가 선택된 detail에 선언된 키만 최종 결과에 남긴다.
    다른 서브에이전트의 기존 oneOf 스키마에는 영향을 주지 않는다.
    """

    scenario_codes = tuple(
        str(scenario["code"])
        for scenario in bundle.manifest["scenarios"]
    )
    detail_entries = [
        (scenario, detail)
        for scenario in bundle.manifest["scenarios"]
        for detail in scenario["details"]
    ]
    detail_codes = tuple(str(detail["code"]) for _, detail in detail_entries)
    definitions = bundle.manifest["parameter_definitions"]
    used_parameter_names = {
        name
        for _, detail in detail_entries
        for name in _detail_parameter_names(bundle.manifest, detail)
    }

    parameter_fields: dict[str, tuple[Any, Field]] = {}
    for name, definition in definitions.items():
        if str(name) not in used_parameter_names:
            continue
        value_type = str(definition.get("value_type", "string"))
        allowed_values = definition.get("allowed_values")
        parameter_type: Any
        if value_type == "string_list":
            parameter_type = list[str] | None
        elif value_type == "string":
            parameter_type = str | None
        else:
            raise ValueError(
                "지원하지 않는 parameter value_type입니다: "
                f"name={name}, value_type={value_type}"
            )
        if allowed_values and value_type == "string":
            parameter_type = Literal.__getitem__(
                tuple(str(value) for value in allowed_values) + (None,)
            )
        field_options: dict[str, Any] = {
            "description": (
                f"{definition['description']} 선택한 detail에서 사용하지 않는 "
                "파라미터이면 null"
            ),
        }
        if value_type == "string":
            field_options["pattern"] = definition.get("pattern")
        else:
            field_options["min_length"] = int(definition.get("min_items", 1))
            field_options["max_length"] = int(definition.get("max_items", 10))
        parameter_fields[str(name)] = (
            parameter_type,
            Field(**field_options),
        )

    parameters_model = create_model(
        f"{bundle.agent_code}FlatParameters",
        __config__=ConfigDict(extra="forbid"),
        **parameter_fields,
    )
    detail_mapping = "; ".join(
        (
            f"{detail['code']}={detail['name']}: "
            f"{detail.get('description', '')}"
        )
        for _, detail in detail_entries
    )
    match_model = create_model(
        f"{bundle.agent_code}FlatMatch",
        __config__=ConfigDict(extra="forbid"),
        scenario_code=(
            Literal.__getitem__(scenario_codes),
            Field(
                description=(
                    "선택한 detail이 실제로 속한 최상위 시나리오 코드"
                )
            ),
        ),
        detail_scenario_code=(
            Literal.__getitem__(detail_codes),
            Field(description=f"세부 시나리오 선택표: {detail_mapping}"),
        ),
        parameters=(
            parameters_model,
            Field(
                description=(
                    "선택한 detail에서 사용하는 값만 추출하고 나머지 필드는 "
                    "모두 null로 반환"
                )
            ),
        ),
    )
    return create_model(
        f"{bundle.agent_code}FlatScenarioOutput",
        __base__=_ScenarioOutputBase,
        status=(
            Literal["MATCHED", "UNSUPPORTED"],
            Field(
                description=(
                    "일치하는 detail이 있으면 MATCHED, 등록된 detail로 처리할 수 "
                    "없으면 UNSUPPORTED"
                ),
            ),
        ),
        matches=(
            list[match_model],
            Field(
                min_length=0,
                max_length=len(detail_codes),
                description=(
                    "질문에 실제로 포함된 독립 요청과 일치하는 detail만 반환. "
                    "단일 요청은 한 개, 동일 detail은 중복 금지. "
                    "status=UNSUPPORTED이면 빈 배열"
                ),
            ),
        ),
    )


def _normalize_parameters(
    *,
    raw: dict[str, Any],
    manifest: dict[str, Any],
    detail: dict[str, Any],
) -> dict[str, Any]:
    """LLM 누락값을 정규화하고 detail에 선언된 명시적 기본값을 적용한다.

    일반적인 날짜·조회 기본값은 MCP handler에서 처리한다. 다만 분류 결과와
    세부 시나리오가 반드시 일치해야 하는 코드값은 manifest detail의
    ``parameter_defaults``에서 선언해 LLM이 null을 반환해도 안정적으로 보정한다.
    """

    normalized: dict[str, Any] = {}
    parameter_defaults = detail.get("parameter_defaults", {})
    for name in _detail_parameter_names(manifest, detail):
        definition = manifest["parameter_definitions"][name]
        value = raw.get(str(name))
        if str(definition.get("value_type", "string")) == "string_list":
            if not isinstance(value, list) or not value:
                value = parameter_defaults.get(str(name), value)
            items = value if isinstance(value, list) else []
            normalized_items: list[str] = []
            seen: set[str] = set()
            for item in items:
                keyword = str(item).strip()
                if not keyword or keyword in seen:
                    continue
                seen.add(keyword)
                normalized_items.append(keyword)
            normalized[str(name)] = normalized_items
            continue
        normalized_value = "" if value is None else str(value).strip()
        if not normalized_value and str(name) in parameter_defaults:
            normalized_value = str(parameter_defaults[str(name)]).strip()
        normalized[str(name)] = normalized_value
    return normalized


def _detail_parameter_names(
    manifest: dict[str, Any],
    detail: dict[str, Any],
) -> list[str]:
    """한 detail이 LLM에서 추출하도록 허용된 파라미터 이름만 반환한다."""

    configured = detail.get("parameters", [])
    return [
        str(name)
        for name in configured
        if str(name) in manifest["parameter_definitions"]
    ]
