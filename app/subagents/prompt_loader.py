"""버전별 서브에이전트 manifest와 전체 프롬프트를 불러오는 모듈."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.mcp.scenarios.registry import get_scenario_handler_spec
from app.observability import logger, timed
from app.subagents.fixed_responses import get_subagent_fixed_response


@dataclass(frozen=True)
class ScenarioPromptBundle:
    """한 서브에이전트의 동적 스키마와 LLM 체인 생성에 필요한 정보."""

    agent_code: str
    version: str
    system_prompt: str
    temperature: float
    manifest: dict[str, Any]
    files: tuple[str, ...]


class SubagentPromptLoader:
    """registry에 등록된 시나리오 서브에이전트 프롬프트를 로딩한다.

    모든 서브에이전트가 같은 manifest 계약을 사용하므로 새로운 에이전트는
    디렉터리와 manifest/Markdown을 추가하고 registry.yaml에 등록하면 된다.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = (
            root or Path(__file__).resolve().parents[2] / "prompts" / "subagents"
        )

    @timed("서브에이전트 registry 조회")
    def load_all(self) -> dict[str, ScenarioPromptBundle]:
        """활성화된 모든 서브에이전트의 프롬프트 묶음을 반환한다."""

        registry_path = self._root / "registry.yaml"
        registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        configured = registry.get("subagents", {})
        bundles: dict[str, ScenarioPromptBundle] = {}

        for configured_code, config in configured.items():
            if not config.get("enabled", True):
                continue
            bundle = self.load_one(
                directory=str(config["directory"]),
            )
            normalized_code = configured_code.upper()
            if bundle.agent_code != normalized_code:
                raise ValueError(
                    "registry 에이전트 코드와 manifest agent_code가 "
                    f"일치하지 않습니다: {normalized_code}"
                )
            bundles[normalized_code] = bundle

        logger.info(
            "======== 서브에이전트 registry 로딩 완료 | 개수=%d | 코드=%s",
            len(bundles),
            sorted(bundles),
        )
        return bundles

    @timed("서브에이전트 프롬프트 결합")
    def load_one(
        self,
        *,
        directory: str,
        requested_version: str | None = None,
    ) -> ScenarioPromptBundle:
        """한 서브에이전트의 활성 버전 프롬프트를 순서대로 결합한다."""

        agent_root = self._root / directory
        active = yaml.safe_load(
            (agent_root / "active.yaml").read_text(encoding="utf-8")
        )
        version = requested_version or str(active["active_version"])
        version_root = agent_root / version
        manifest = yaml.safe_load(
            (version_root / "manifest.yaml").read_text(encoding="utf-8")
        )

        configured_files = tuple(str(path) for path in manifest["prompt_files"])
        paths = [version_root / path for path in configured_files]
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            raise FileNotFoundError(f"서브에이전트 프롬프트 파일이 없습니다: {missing}")

        # manifest에 적지 않은 Markdown을 실수로 추가해도 누락되지 않도록
        # 선언된 파일 뒤에 정렬하여 결합한다.
        declared = {path.resolve() for path in paths}
        paths.extend(
            path
            for path in sorted(version_root.rglob("*.md"))
            if path.resolve() not in declared
        )

        parts: list[str] = []
        relative_files: list[str] = []
        for path in paths:
            relative = path.relative_to(version_root).as_posix()
            content = path.read_text(encoding="utf-8").strip()
            relative_files.append(relative)
            if content:
                parts.append(f"<!-- prompt: {relative} -->\n{content}")

        self._validate_manifest(manifest)
        bundle = ScenarioPromptBundle(
            agent_code=str(manifest["agent_code"]).upper(),
            version=version,
            system_prompt="\n\n---\n\n".join(parts),
            temperature=float(manifest.get("model", {}).get("temperature", 0)),
            manifest=manifest,
            files=tuple(relative_files),
        )
        logger.info(
            "======== 서브에이전트 프롬프트 결합 완료 | 에이전트=%s | "
            "버전=%s | 파일개수=%d | 시나리오개수=%d",
            bundle.agent_code,
            bundle.version,
            len(bundle.files),
            len(manifest["scenarios"]),
        )
        return bundle

    @staticmethod
    def _validate_manifest(manifest: dict[str, Any]) -> None:
        """시나리오·세부 시나리오 코드와 복합 매칭 참조를 검증한다."""

        scenario_codes: set[str] = set()
        detail_codes: set[str] = set()
        parameter_names = {
            str(name) for name in manifest.get("parameter_definitions", {})
        }
        agent_code = str(manifest["agent_code"]).upper()

        for scenario in manifest["scenarios"]:
            scenario_code = str(scenario["code"])
            if scenario_code in scenario_codes:
                raise ValueError(f"중복 시나리오 코드입니다: {scenario_code}")
            scenario_codes.add(scenario_code)

            for detail in scenario["details"]:
                detail_code = str(detail["code"])
                if detail_code in detail_codes:
                    raise ValueError(f"중복 세부 시나리오 코드입니다: {detail_code}")
                detail_codes.add(detail_code)

                configured_parameters = detail.get("parameters", [])
                if not isinstance(configured_parameters, list):
                    raise ValueError(
                        "detail.parameters는 파라미터 이름 배열이어야 합니다: "
                        f"detail={detail_code}"
                    )
                detail_parameters = {str(name) for name in configured_parameters}
                if len(detail_parameters) != len(configured_parameters):
                    raise ValueError(
                        "detail.parameters에는 중복 값을 사용할 수 없습니다: "
                        f"detail={detail_code}"
                    )
                unknown_detail_parameters = detail_parameters - parameter_names
                if unknown_detail_parameters:
                    raise ValueError(
                        "detail이 선언되지 않은 parameter를 참조합니다: "
                        f"detail={detail_code}, "
                        f"parameters={sorted(unknown_detail_parameters)}"
                    )

                parameter_defaults = detail.get("parameter_defaults", {})
                if not isinstance(parameter_defaults, dict):
                    raise ValueError(
                        "detail.parameter_defaults는 객체여야 합니다: "
                        f"detail={detail_code}"
                    )
                unknown_default_parameters = (
                    {str(name) for name in parameter_defaults} - detail_parameters
                )
                if unknown_default_parameters:
                    raise ValueError(
                        "parameter_defaults가 detail.parameters 밖의 값을 "
                        f"참조합니다: detail={detail_code}, "
                        f"parameters={sorted(unknown_default_parameters)}"
                    )
                for parameter_name, default_value in parameter_defaults.items():
                    definition = manifest["parameter_definitions"][parameter_name]
                    allowed_values = definition.get("allowed_values")
                    if allowed_values and default_value not in allowed_values:
                        raise ValueError(
                            "parameter_defaults 값이 allowed_values에 없습니다: "
                            f"detail={detail_code}, parameter={parameter_name}, "
                            f"value={default_value}"
                        )

                if (
                    get_subagent_fixed_response(agent_code, detail_code) is None
                    and get_scenario_handler_spec(agent_code, detail_code) is None
                ):
                    raise ValueError(
                        "고정답변이 아닌 detail에 Python MCP handler가 없습니다: "
                        f"agent={agent_code}, detail={detail_code}. "
                        "app/mcp/scenarios/registry.py에 등록하세요."
                    )

        # 복합 질문 보완 규칙이 존재하면 현재 서브에이전트에 실제 등록된 세부
        # 시나리오만 참조하는지 시작 시점에 검증한다.
        for rule in manifest.get("required_match_rules", []):
            terms = rule.get("all_terms", [])
            if not isinstance(terms, list) or not terms:
                raise ValueError(
                    "required_match_rules.all_terms는 비어 있지 않은 배열이어야 합니다."
                )
            unknown_details = {
                str(code) for code in rule.get("detail_codes", [])
            } - detail_codes
            if unknown_details:
                raise ValueError(
                    "required_match_rules에 알 수 없는 세부 시나리오가 있습니다: "
                    f"{sorted(unknown_details)}"
                )
