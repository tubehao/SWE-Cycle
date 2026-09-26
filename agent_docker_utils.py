"""
Agent Docker 工具函数（新系统保留部分）

从旧版 agent_docker_utils.py 中提取的、仍被新系统
（harbor_agent_builder.py, build_swe_agent_base_image.py, ccb_adapter.py, build_all_docker.py）
使用的函数。
"""
import logging
import docker
import docker.errors
from typing import Optional

import config
from swebench.harness.test_spec.test_spec import TestSpec
from swebench.harness.docker_build import build_image, setup_logger
from swebench.harness.constants import INSTANCE_IMAGE_BUILD_DIR, DOCKER_USER
from swebench.harness.dockerfiles import get_dockerfile_env

logger = logging.getLogger(__name__)

# 支持的语言列表
SUPPORTED_LANGUAGES = ["py", "js", "java", "go", "c", "php", "rb", "rs"]

# 旧版 Agent base image 名称（向后兼容 Python only）
AGENT_BASE_IMAGE_X86 = "swe-agent-base-x86_64:latest"
AGENT_BASE_IMAGE_ARM = "swe-agent-base-arm64:latest"

# ---------- 多语言 repo_script 截断标记 ----------
ENVIRONMENT_TRUNCATION_MARKERS = {
    "py": ["source /opt/miniconda3", "conda activate"],
    "js": ["npm install", "yarn install", "pnpm install", "nvm use", "npm ci"],
    "java": ["mvn ", "gradle ", "ant ", "mvnd "],
    "go": ["go mod ", "go install", "go build", "go get"],
    "c": ["make", "cmake", "configure", "autoreconf"],
    "php": ["composer install", "composer update"],
    "rb": ["bundle install", "gem install", "bundle exec"],
    "rs": ["cargo build", "cargo install", "cargo fetch"],
}


def truncate_repo_script_for_environment(repo_script_list: list, language: str) -> list:
    """
    截断 repo script，只保留 clone + checkout，移除依赖安装步骤。

    用于 Environment/FullPipe 题型，Agent 需要自己搭建环境。

    Args:
        repo_script_list: 原始的 repo script 列表
        language: 语言标识

    Returns:
        截断后的 repo script 列表
    """
    markers = ENVIRONMENT_TRUNCATION_MARKERS.get(language, [])
    repo_list = list(repo_script_list)
    for i, line in enumerate(repo_list):
        if any(marker in line for marker in markers):
            return repo_list[:i]
    return repo_list  # 未匹配到，返回完整列表


def _insert_suffix_before_tag(image: str, suffix: str) -> str:
    """
    Insert suffix before :tag in a docker image reference.
    Examples:
      - "repo:latest" + ".noenvironment" -> "repo.noenvironment:latest"
      - "repo" + ".noenvironment" -> "repo.noenvironment"
      - "registry:5000/repo:latest" handled correctly
    """
    if not suffix:
        # Ensure docker repository part is lowercase
        last_slash = image.rfind("/")
        last_colon = image.rfind(":")
        if last_colon > last_slash:
            name, tag = image.rsplit(":", 1)
            return f"{name.lower()}:{tag}"
        return image.lower()
    # Docker repository names must be lowercase
    suffix = suffix.lower()
    last_slash = image.rfind("/")
    last_colon = image.rfind(":")
    # Treat as having a tag only if ":" appears after last "/"
    if last_colon > last_slash:
        name, tag = image.rsplit(":", 1)
        return f"{name.lower()}{suffix}:{tag}"
    return f"{image.lower()}{suffix}"


def get_agent_base_image(arch: str = "x86_64", language: str = "py") -> Optional[str]:
    """
    获取 agent base image 名称（已包含 MinimalCodeAgent + Claude Code + 预置环境）。

    Args:
        arch: 架构类型（x86_64 / arm64）
        language: 语言标识（py, js, java, go, c, php, rb, rs）

    Returns:
        image_name: agent base image 名称，若不存在则返回 None
    """
    try:
        from build_swe_agent_base_image import get_agent_base_image_name
        image_name = get_agent_base_image_name(arch, language)
    except (ImportError, ValueError):
        # Fallback: 使用旧式命名（仅 Python）
        if language != "py":
            return None
        if arch == "x86_64":
            image_name = AGENT_BASE_IMAGE_X86
        elif arch == "arm64":
            image_name = AGENT_BASE_IMAGE_ARM
        else:
            return None

    try:
        client = docker.from_env()
        client.images.get(image_name)
        return image_name
    except docker.errors.ImageNotFound:
        return None


def build_agent_env_image(
    test_spec: TestSpec,
    client: docker.DockerClient,
    logger: Optional[logging.Logger] = None,
    force_rebuild: bool = False,
) -> str:
    """
    基于 agent base image 构建 env image。
    若 agent base image 不存在则直接报错，不从 swebench base 构建。

    Args:
        test_spec: TestSpec 实例
        client: Docker client
        logger: Logger
        force_rebuild: 是否强制重建

    Returns:
        env_image_key: 环境镜像的 key
    """
    language = getattr(test_spec, 'language', 'py')
    agent_base_image = get_agent_base_image(test_spec.arch, language)

    if agent_base_image is None:
        raise RuntimeError(
            f"Agent base image (Claude Code) not found for language={language}. "
            f"Run: python build_swe_agent_base_image.py --language {language}"
        )

    # 创建基于 agent base image 的 env image key
    agent_env_image_key = f"ccb-agent.{test_spec.env_image_key}"

    # 检查镜像是否已存在
    if not force_rebuild:
        try:
            client.images.get(agent_env_image_key)
            logger.info(f"Agent env image already exists: {agent_env_image_key}")
            return agent_env_image_key
        except docker.errors.ImageNotFound:
            pass

    # 构建基于 agent base image 的 env image
    logger.info(f"Building agent env image: {agent_env_image_key} based on {agent_base_image}")

    env_dockerfile_template = get_dockerfile_env(
        test_spec.platform,
        test_spec.arch,
        test_spec.language,
        agent_base_image,
        **{**test_spec.docker_specs}
    )

    # 构建镜像
    build_dir = INSTANCE_IMAGE_BUILD_DIR / agent_env_image_key.replace(":", "__")
    build_dir.mkdir(parents=True, exist_ok=True)

    if logger is None:
        logger = setup_logger(test_spec.instance_id, build_dir / "build_agent_env.log")

    build_image(
        image_name=agent_env_image_key,
        setup_scripts={"setup_env.sh": test_spec.setup_env_script},
        dockerfile=env_dockerfile_template,
        platform=test_spec.platform,
        client=client,
        build_dir=build_dir,
        nocache=force_rebuild
    )

    logger.info(f"Successfully built agent env image: {agent_env_image_key}")
    return agent_env_image_key


def check_agent_image_complete(
    image_name: Optional[str] = None,
    arch: str = "x86_64",
    language: str = "py",
    client: Optional[docker.DockerClient] = None,
) -> None:
    """
    检查 agent 基础镜像是否「完整可用」。

    检查项（失败任一项将抛出 RuntimeError）：
    - Agent 代码路径是否存在（config.AGENTPATH）
    - conda 环境是否存在（config.ENV_NAME）
    - Python 是否可用
    - Claude Code CLI 是否可用且能返回结果
    """
    if client is None:
        client = docker.from_env()

    if image_name is None:
        image_name = get_agent_base_image(arch, language)
        if image_name is None:
            raise RuntimeError(
                f"No agent base image found for arch={arch}, language={language}. "
                f"Please build it first (e.g. python build_swe_agent_base_image.py --language {language})."
            )

    logger.info(f"Checking agent image completeness: {image_name}")
    container = None
    try:
        container = client.containers.run(
            image=image_name,
            command="tail -f /dev/null",
            user=DOCKER_USER,
            detach=True,
        )

        def _run_check(cmd: str, desc: str) -> None:
            res = container.exec_run(cmd)
            if res.exit_code != 0:
                output = res.output.decode("utf-8", errors="replace") if res.output else ""
                raise RuntimeError(
                    f"Agent image check failed: {desc}\n"
                    f"Command: {cmd}\n"
                    f"Exit code: {res.exit_code}\n"
                    f"Output: {output}"
                )

        _run_check(
            f"bash -lc 'test -d {config.AGENTPATH}'",
            f"Agent path {config.AGENTPATH} missing",
        )
        _run_check(
            f"bash -lc 'source /opt/miniconda3/etc/profile.d/conda.sh && conda env list | grep -q \"^{config.ENV_NAME} \"'",
            f"Conda env {config.ENV_NAME} missing",
        )
        _run_check("bash -lc 'python -c \"print(123)\"'", "Python not available for agent")
        _run_check("bash -lc 'claude -p --output-format json \"ping\"'", "Claude Code CLI not available or failed to respond")

        logger.info(f"Agent image {image_name} passed completeness checks.")

    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                logger.warning("Failed to clean up temporary container used for image check.", exc_info=True)
