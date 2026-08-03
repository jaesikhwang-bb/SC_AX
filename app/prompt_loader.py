"""버전별 프롬프트 파일을 읽고 하나의 시스템 프롬프트로 결합한다."""

from dataclasses import dataclass
from pathlib import Path

import yaml

from app.observability import logger, timed


@dataclass(frozen=True)
class PromptBundle:
    """한 번의 LLM 체인 생성에 필요한 프롬프트 묶음."""

    version: str
    system_prompt: str
    temperature: float
    agent_codes: tuple[str, ...]
    files: tuple[str, ...]


class PromptBundleLoader:
    """활성 manifest가 선언한 프롬프트와 추가 Markdown을 모두 결합한다."""

    def __init__(self, root: Path | None = None) -> None:
        # 테스트에서는 임시 경로를 주입할 수 있고, 운영에서는 프로젝트의
        # prompts/intent-classification 디렉터리를 기본으로 사용한다.
        self._root = (
            root
            or Path(__file__).resolve().parents[1]
            / "prompts"
            / "intent-classification"
        )

    @timed("프롬프트 파일 결합")
    def load(self, requested_version: str | None = None) -> PromptBundle:
        """요청 버전 또는 active.yaml 버전의 전체 프롬프트를 불러온다."""

        logger.info(
            "======== 프롬프트 로딩 시작 | 요청버전=%s",
            requested_version or "active.yaml 사용",
        )
        active = yaml.safe_load(
            (self._root / "active.yaml").read_text(encoding="utf-8")
        )
        version = requested_version or active["active_version"]
        version_dir = self._root / version
        manifest = yaml.safe_load(
            (version_dir / "manifest.yaml").read_text(encoding="utf-8")
        )

        # 먼저 라우터 프롬프트를 배치하고, manifest의 agent_code 순서대로
        # 각 에이전트 프롬프트를 추가한다.
        paths = [
            version_dir / manifest["router"]["system_prompt"],
            version_dir / manifest["router"]["agents_prompt"],
        ]
        paths.extend(
            version_dir / "agents" / f"{code}.md"
            for code in manifest["agent_code"]
        )

        # 향후 manifest에 전용 필드가 생기기 전에 추가된 Markdown도 누락되지
        # 않도록 아직 선언되지 않은 모든 .md 파일을 정렬해 뒤에 붙인다.
        declared = {path.resolve() for path in paths}
        paths.extend(
            path
            for path in sorted(version_dir.rglob("*.md"))
            if path.resolve() not in declared
        )

        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            raise FileNotFoundError(f"프롬프트 파일이 없습니다: {missing}")

        parts: list[str] = []
        relative_files: list[str] = []
        for path in paths:
            relative = path.relative_to(version_dir).as_posix()
            relative_files.append(relative)
            content = path.read_text(encoding="utf-8").strip()

            # 빈 파일은 파일 목록에는 남기되 LLM 전달 문자열에는 추가하지 않는다.
            if content:
                parts.append(f"<!-- prompt: {relative} -->\n{content}")

        if not parts:
            raise ValueError(
                f"{version} 버전에 내용이 있는 프롬프트가 없습니다."
            )

        bundle = PromptBundle(
            version=version,
            system_prompt="\n\n---\n\n".join(parts),
            temperature=float(
                manifest.get("model", {}).get("temperature", 0)
            ),
            agent_codes=tuple(
                code.upper() for code in manifest["agent_code"]
            ),
            files=tuple(relative_files),
        )
        logger.info(
            "======== 프롬프트 결합 완료 | 버전=%s | 파일개수=%d | "
            "에이전트개수=%d | 전체문자수=%d",
            bundle.version,
            len(bundle.files),
            len(bundle.agent_codes),
            len(bundle.system_prompt),
        )
        return bundle
