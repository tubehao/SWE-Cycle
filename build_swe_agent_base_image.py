#!/usr/bin/env python3
"""
从头构建包含 MinimalCodeAgent + Claude Code CLI + 预置环境的 Agent 基础镜像。

支持多语言：
- Python (py): 使用现有 swebench Python base image
- JavaScript (js): 使用 swebench JS base image + 安装 miniconda
- Java (java): 使用 swebench Java base image + 安装 miniconda
- Go (go): 使用 swebench Go base image + 安装 miniconda
- C (c): 使用 swebench C base image + 安装 miniconda
- PHP (php): 使用 swebench PHP base image + 安装 miniconda
- Ruby (rb): 使用 swebench Ruby base image + 安装 miniconda
- Rust (rs): 使用 swebench Rust base image + 安装 miniconda

输出镜像（按语言）：
- swe-agent-base-py-x86_64:latest   (Python, 兼容旧名 swe-agent-base-x86_64)
- swe-agent-base-js-x86_64:latest   (JavaScript)
- swe-agent-base-java-x86_64:latest (Java)
- ...

构建完成后会自动调用 agent_docker_utils.check_agent_image_complete() 做一次验收检查
（不会在每次 solve 前自动检查，以保证 solve 速度）。
"""

import argparse
import json
import logging
from pathlib import Path
import shutil

import docker
from docker.errors import ImageNotFound

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# 支持的语言列表
SUPPORTED_LANGUAGES = ["py", "js", "java", "go", "c", "php", "rb", "rs"]

# 旧版 Python-only 镜像名（向后兼容）
SWE_AGENT_BASE_X86 = "swe-agent-base-x86_64:latest"
SWE_AGENT_BASE_ARM = "swe-agent-base-arm64:latest"


def _load_dotenv(env_dir: Path) -> dict:
    """从 env_dir 下的 .env 读取 KEY=VALUE，返回 dict。跳过空行和 # 注释。"""
    env_path = env_dir / ".env"
    result = {}
    if not env_path.exists():
        return result
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip().strip("'\"")
    return result


def _make_claude_problem_json(token: str) -> dict:
    return {
        "env": {
            "ANTHROPIC_AUTH_TOKEN": token or "",
            "ANTHROPIC_BASE_URL": "https://api.toiotech.com",
            "ANTHROPIC_MODEL": "claude-sonnet-4-5-20250929",
            "ANTHROPIC_SMALL_FAST_MODEL": "claude-sonnet-4-5-20250929",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-5-20250929",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-sonnet-4-5-20250929",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "8000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": 1,
        },
        "defaultMode": "bypassPermissions",
        "hasCompletedOnboarding": True,
    }


def get_agent_base_image_name(arch: str, language: str = "py") -> str:
    """
    获取 Agent base image 名称（按语言和架构）。

    Args:
        arch: 架构类型（x86_64 / arm64）
        language: 语言标识（py, js, java, go, c, php, rb, rs）

    Returns:
        image_name: e.g., "swe-agent-base-py-x86_64:latest"
    """
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(f"Unsupported language: {language}. Supported: {SUPPORTED_LANGUAGES}")
    if arch not in ("x86_64", "arm64"):
        raise ValueError(f"Unsupported arch: {arch}")

    # 向后兼容：Python 的旧名称保持不变
    if language == "py":
        if arch == "x86_64":
            return SWE_AGENT_BASE_X86
        return SWE_AGENT_BASE_ARM

    return f"swe-agent-base-{language}-{arch}:latest"


def _get_target_image(arch: str, language: str = "py") -> str:
    """Alias for get_agent_base_image_name, for backward compatibility."""
    return get_agent_base_image_name(arch, language)


def _find_swebench_base_image(client: docker.DockerClient, arch: str, language: str = "py") -> str:
    """
    查找 swebench 的 base image（按语言）。

    对于 Python，使用 build_agent_base_image.find_swebench_base_image()。
    对于其他语言，尝试查找 sweb.base.{lang}.{arch}:latest。
    """
    if language == "py":
        try:
            from build_agent_base_image import find_swebench_base_image
        except Exception as e:
            raise RuntimeError(f"Failed to import find_swebench_base_image: {e}") from e
        return find_swebench_base_image(client, arch)

    # 非 Python 语言：查找对应的 swebench base image
    possible_names = [
        f"sweb.base.{language}.{arch}:latest",
    ]
    for image_name in possible_names:
        try:
            client.images.get(image_name)
            logger.info(f"Found swebench base image for {language}: {image_name}")
            return image_name
        except ImageNotFound:
            continue

    # 如果没找到，返回默认名称（用户需要先构建 swebench base image）
    default_name = f"sweb.base.{language}.{arch}:latest"
    logger.warning(f"Could not find swebench base image for {language}. Will use: {default_name}")
    return default_name


def _generate_miniconda_install_block(language: str) -> str:
    """
    为非 Python 语言生成 miniconda 安装的 Dockerfile 片段。
    Python base image 已有 miniconda，无需安装。
    """
    if language == "py":
        return ""

    return """
# ---------- 安装 Miniconda（非 Python base image 需要）----------
RUN apt-get update -qq && apt-get install -y -qq wget ca-certificates >/dev/null \\
    && rm -rf /var/lib/apt/lists/*

RUN wget -q 'https://repo.anaconda.com/miniconda/Miniconda3-py310_24.1.2-0-Linux-x86_64.sh' \\
    -O /tmp/miniconda.sh \\
    && bash /tmp/miniconda.sh -b -p /opt/miniconda3 \\
    && rm /tmp/miniconda.sh

ENV PATH=/opt/miniconda3/bin:$PATH
RUN conda init --all
"""


def build_agent_claude_code_base_image(
    client: docker.DockerClient,
    arch: str = "x86_64",
    language: str = "py",
    force_rebuild: bool = False,
    claude_code_channel_or_version: str = "stable",
    claude_binary_path: str | None = None,
    base_image: str | None = None,
) -> str:
    """
    构建包含 MinimalCodeAgent + Claude Code CLI + 预置环境的 Agent 基础镜像。

    Args:
        client: Docker 客户端
        arch: 架构类型（x86_64 / arm64）
        language: 语言标识（py, js, java, go, c, php, rb, rs）
        force_rebuild: 是否强制重建
        claude_code_channel_or_version: Claude Code 安装版本
        claude_binary_path: 可选的本地 Claude Code 二进制路径
        base_image: 可选的 swebench base image 名称

    Returns:
        target_image: 构建好的镜像名称
    """
    target_image = _get_target_image(arch, language)

    if not force_rebuild:
        try:
            client.images.get(target_image)
            logger.info(f"Agent base image already exists: {target_image}")
            from agent_docker_utils import check_agent_image_complete
            check_agent_image_complete(image_name=target_image, arch=arch, client=client)
            return target_image
        except ImageNotFound:
            pass

    if base_image is None:
        base_image = _find_swebench_base_image(client, arch, language)
    try:
        client.images.get(base_image)
    except ImageNotFound as e:
        raise RuntimeError(
            f"SWE-bench base image not found: {base_image}. "
            f"Please build swebench base image for language={language} first, or pass --base-image."
        ) from e

    script_dir = Path(__file__).parent
    build_dir = script_dir / f".agent_claude_code_base_build_{language}"
    build_dir.mkdir(exist_ok=True)

    # 写入 claude problem.json
    env_vars = _load_dotenv(script_dir) or _load_dotenv(script_dir.parent)
    token = env_vars.get("ANTHROPIC_AUTH_TOKEN", "")
    if not token:
        logger.warning("ANTHROPIC_AUTH_TOKEN not set in .env; Claude Code may not authenticate in container.")
    (build_dir / "claude_problem.json").write_text(
        json.dumps(_make_claude_problem_json(token), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 准备 Minimal-CodeAgent build context
    agent_code_dir = script_dir / "Minimal-CodeAgent"
    req_file = agent_code_dir / "requirements.txt"
    if not req_file.exists():
        raise FileNotFoundError(f"Minimal-CodeAgent requirements.txt not found: {req_file}")
    if not agent_code_dir.exists():
        raise FileNotFoundError(f"Minimal-CodeAgent directory not found: {agent_code_dir}")
    build_agent_dir = build_dir / "Minimal-CodeAgent"
    if build_agent_dir.exists():
        shutil.rmtree(build_agent_dir)
    shutil.copytree(agent_code_dir, build_agent_dir)

    # Claude CLI：可选本地二进制
    claude_binary_src = None
    if claude_binary_path:
        src_path = Path(claude_binary_path).expanduser()
        if not src_path.exists():
            raise FileNotFoundError(f"--claude-binary not found: {src_path}")
        if src_path.is_dir():
            raise IsADirectoryError(f"--claude-binary must be a file, got dir: {src_path}")
        claude_binary_src = build_dir / "claude"
        shutil.copy2(src_path, claude_binary_src)

    # 目标：镜像内必须包含 /MinimalCodeAgent，并且 claude 可调用
    if claude_binary_src:
        install_claude = """
# 安装 Claude Code（二进制方式）
COPY ./claude /usr/local/bin/claude
RUN chmod +x /usr/local/bin/claude
"""
        claude_path_env = ""
        claude_version_check = "RUN claude --version || true"
    else:
        install_claude = f"""
# Claude Code native installer（需要能访问 claude.ai）
RUN apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null \\
    && rm -rf /var/lib/apt/lists/*

RUN http_proxy=http://10.217.146.127:8080 https_proxy=http://10.217.146.127:8080 \\
    curl -fsSL -o /tmp/claude_install.sh https://claude.ai/install.sh \\
    && head -n 1 /tmp/claude_install.sh | grep -qE '^#!' \\
    && bash /tmp/claude_install.sh {claude_code_channel_or_version}
"""
        claude_path_env = 'ENV PATH="/root/.local/bin:$PATH"'
        claude_version_check = "RUN claude --version"

    # 生成 miniconda 安装块（非 Python 需要）
    miniconda_block = _generate_miniconda_install_block(language)

    # conda init/source 取决于 miniconda 是否已预装（Python base 已有）
    conda_source = "/opt/miniconda3/etc/profile.d/conda.sh"

    dockerfile_content = f"""FROM {base_image}

SHELL ["/bin/bash", "-lc"]
{miniconda_block}
# ---------- MinimalCodeAgent（依赖 + 代码）----------
WORKDIR /tmp
COPY Minimal-CodeAgent/requirements.txt /tmp/agent_requirements.txt

RUN /bin/bash -c "source {conda_source} && conda init --all" || true

RUN /bin/bash -c "source {conda_source} && \\
    if ! conda env list | grep -q '^minimalcodeagent '; then \\
        conda create -n minimalcodeagent python=3.10 -y && \\
        conda activate minimalcodeagent && \\
        pip install --no-cache-dir -r /tmp/agent_requirements.txt; \\
    else \\
        conda activate minimalcodeagent && \\
        pip install --no-cache-dir -r /tmp/agent_requirements.txt; \\
    fi"

RUN mkdir -p /opt/agent_deps && cp /tmp/agent_requirements.txt /opt/agent_deps/requirements.txt
COPY Minimal-CodeAgent/ /MinimalCodeAgent/
RUN chmod -R 755 /MinimalCodeAgent

# ---------- Claude Code CLI ----------
{install_claude}
{claude_path_env}

ENV DISABLE_AUTOUPDATER=1

RUN mkdir -p /root/.claude
COPY ./claude_problem.json /root/.claude/settings.json

{claude_version_check}

WORKDIR /testbed
"""

    dockerfile_path = build_dir / "Dockerfile"
    dockerfile_path.write_text(dockerfile_content, encoding="utf-8")

    logger.info(f"Building agent base image: {target_image} (language={language})")
    image, build_logs = client.images.build(
        path=str(build_dir),
        dockerfile=str(dockerfile_path),
        tag=target_image,
        rm=True,
        forcerm=True,
    )
    for log in build_logs:
        if "error" in log:
            logger.error(log["error"])

    logger.info(f"Successfully built: {target_image} (image id: {image.id})")

    from agent_docker_utils import check_agent_image_complete
    check_agent_image_complete(image_name=target_image, arch=arch, client=client)

    # 对于 Python，也打上旧名称的 tag（向后兼容）
    if language == "py":
        pass  # 已经是旧名称格式

    return target_image


def main() -> int:
    parser = argparse.ArgumentParser(description="Build swe-agent-base image (MinimalCodeAgent + Claude Code)")
    parser.add_argument("--arch", type=str, default="x86_64", choices=["x86_64", "arm64"], help="Architecture")
    parser.add_argument(
        "--language",
        type=str,
        default="py",
        choices=SUPPORTED_LANGUAGES,
        help="Language to build agent base image for (default: py)",
    )
    parser.add_argument("--force-rebuild", action="store_true", help="Force rebuild even if image exists")
    parser.add_argument(
        "--base-image",
        type=str,
        default=None,
        help="Specify swebench base image name (default: auto-detect)",
    )
    parser.add_argument(
        "--channel-or-version",
        type=str,
        default="stable",
        help="Claude Code install arg: stable/latest/or specific version (e.g. 2.1.71)",
    )
    parser.add_argument(
        "--claude-binary",
        type=str,
        default=None,
        help="Path to local Claude Code CLI binary. If provided, Docker build will COPY it instead of using https://claude.ai/install.sh",
    )
    parser.add_argument(
        "--all-languages",
        action="store_true",
        help="Build agent base images for all supported languages",
    )
    parser.add_argument(
        "--agent",
        type=str,
        default="minimal-code-agent",
        help="Agent name (e.g. claude-code, aider, minimal-code-agent). "
             "For Harbor agents, uses Harbor install template. Default: minimal-code-agent",
    )
    args = parser.parse_args()

    import config
    client = docker.from_env(timeout=config.DOCKER_CLIENT_TIMEOUT)

    # 检查是否使用 Harbor agent
    from harbor_agent_builder import normalize_agent_name, is_harbor_supported
    resolved_agent = normalize_agent_name(args.agent)

    if is_harbor_supported(resolved_agent):
        # Harbor agent：不需要构建传统的 agent base image
        # 提示用户使用 harbor_agent_builder 或直接在 solve/eval 中使用
        print(f"\nAgent '{resolved_agent}' is a Harbor-supported agent.")
        print("Harbor agents are installed directly during instance image build.")
        print("No separate base image build is needed.")
        print(f"\nTo build instance images with this agent, use:")
        print(f"  python build_all_docker.py --agent {resolved_agent} ...")
        print(f"  python solve.py --agent {resolved_agent} ...")
        return 0

    # MinimalCodeAgent (custom agent): use existing build logic
    languages = SUPPORTED_LANGUAGES if args.all_languages else [args.language]

    success_count = 0
    fail_count = 0
    for lang in languages:
        try:
            image_name = build_agent_claude_code_base_image(
                client=client,
                arch=args.arch,
                language=lang,
                force_rebuild=args.force_rebuild,
                claude_code_channel_or_version=args.channel_or_version,
                claude_binary_path=args.claude_binary,
                base_image=args.base_image if not args.all_languages else None,
            )
            print(f"\n\u2705 Success! Agent base image built for {lang}: {image_name}")
            success_count += 1
        except Exception as e:
            logger.error(f"Build failed for language={lang}: {e}")
            fail_count += 1

    if args.all_languages:
        print(f"\n\u2705 Built {success_count}/{success_count + fail_count} language images")

    return 1 if fail_count > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
