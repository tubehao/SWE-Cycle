#!/usr/bin/env python3
"""
Harbor Agent Builder: 统一的 Agent Docker Image 构建模块

支持两种 agent 安装方式：
1. Harbor 内置 agent：使用 Harbor 的 install.sh.j2 Jinja2 模板
2. 自定义 agent（如 MinimalCodeAgent）：使用手动 Dockerfile 指令

核心入口：build_image_with_agent(dockerfile_content, agent_name, ...) -> str
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import docker
from docker.errors import ImageNotFound

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Harbor 支持的 agent 列表及其模板文件名映射
# ---------------------------------------------------------------------------
HARBOR_SUPPORTED_AGENTS = [
    "claude-code",
    "aider",
    "codex",
    "cursor-cli",
    "gemini-cli",
    "goose",
    "kimi-cli",
    "mini-swe-agent",
    "swe-agent",
    "opencode",
    "openhands",
    "openhands-sdk",
    "qwen-code",
    "cline-cli",
]

# agent 名称 -> 模板文件相对路径（相对于 Harbor installed 目录）
_AGENT_TEMPLATE_MAP = {
    "claude-code": "install-claude-code.sh.j2",
    "aider": "install-aider.sh.j2",
    "codex": "install-codex.sh.j2",
    "cursor-cli": "install-cursor-cli.sh.j2",
    "gemini-cli": "install-gemini-cli.sh.j2",
    "goose": "install-goose.sh.j2",
    "kimi-cli": "install-kimi-cli.sh.j2",
    "mini-swe-agent": "install-mini-swe-agent.sh.j2",
    "swe-agent": "install-swe-agent.sh.j2",
    "opencode": "install-opencode.sh.j2",
    "openhands": "install-openhands.sh.j2",
    "openhands-sdk": "install-openhands-sdk.sh.j2",
    "qwen-code": "install-qwen-code.sh.j2",
    "cline-cli": "cline/install-cline.sh.j2",
}

# 自定义 agent 的别名映射（允许用户使用不同命名方式）
_AGENT_ALIASES = {
    "minimal-code-agent": "minimal-code-agent",
    "minimalcodeagent": "minimal-code-agent",
    "MinimalCodeAgent": "minimal-code-agent",
}


def _get_harbor_installed_dir() -> Path:
    """获取 Harbor agents/installed 目录路径。"""
    import config
    harbor_path = getattr(config, "HARBOR_PATH", None)
    if harbor_path:
        candidate = Path(harbor_path) / "src" / "harbor" / "agents" / "installed"
        if candidate.exists():
            return candidate
    # 尝试项目内 submodule 路径
    local = Path(__file__).parent / "harbor" / "src" / "harbor" / "agents" / "installed"
    if local.exists():
        return local
    # Fallback：返回 config 中配置的路径（即使不存在，后续会在模板加载时报错）
    if harbor_path:
        return Path(harbor_path) / "src" / "harbor" / "agents" / "installed"
    return local


def _get_script_dir() -> Path:
    """获取当前脚本所在目录。"""
    return Path(__file__).parent


def normalize_agent_name(agent_name: str) -> str:
    """标准化 agent 名称。"""
    return _AGENT_ALIASES.get(agent_name, agent_name)


def is_harbor_supported(agent_name: str) -> bool:
    """判断 agent 是否为 Harbor 内置支持。"""
    name = normalize_agent_name(agent_name)
    return name in HARBOR_SUPPORTED_AGENTS


def get_harbor_install_script(agent_name: str, version: str | None = None) -> str:
    """
    从 Harbor 的 Jinja2 模板渲染出 agent 安装脚本。

    Args:
        agent_name: Harbor agent 名称（如 claude-code, aider 等）
        version: 可选的版本号

    Returns:
        渲染后的 shell 脚本内容

    Raises:
        FileNotFoundError: 模板文件不存在
        ValueError: agent 不被 Harbor 支持
    """
    name = normalize_agent_name(agent_name)
    if name not in _AGENT_TEMPLATE_MAP:
        raise ValueError(
            f"Agent '{agent_name}' is not a Harbor supported agent. "
            f"Supported: {HARBOR_SUPPORTED_AGENTS}"
        )

    template_rel = _AGENT_TEMPLATE_MAP[name]
    template_path = _get_harbor_installed_dir() / template_rel
    if not template_path.exists():
        raise FileNotFoundError(
            f"Harbor install template not found: {template_path}. "
            f"Please ensure Harbor is available at the expected path."
        )

    template_content = template_path.read_text(encoding="utf-8")

    # 使用 Jinja2 渲染（如果可用），否则用简单替换
    try:
        from jinja2 import Template
        tmpl = Template(template_content)
        return tmpl.render(version=version or "")
    except ImportError:
        # Fallback: 简单的字符串替换
        logger.warning("jinja2 not installed; using simple string replacement for template rendering")
        if version:
            result = template_content.replace("{{ version }}", version)
            result = result.replace("{{version}}", version)
        else:
            # 移除 version 条件块（简单处理）
            import re
            # 移除 {% if version %} ... {% else %} 之间的内容，保留 {% else %} ... {% endif %} 的内容
            # 或者如果没有 else，移除整个 if 块
            result = template_content
            # 简单策略：移除所有 Jinja2 标签
            result = re.sub(r'\{%.*?%\}\s*\n?', '', result)
            result = re.sub(r'\{\{.*?\}\}', '', result)
        return result


def generate_agent_dockerfile_block(
    agent_name: str,
    version: str | None = None,
    language: str = "py",
) -> str:
    """
    生成添加到 Dockerfile 末尾的 agent 安装指令块。

    对于 Harbor agent：
      COPY install_agent.sh /tmp/install_agent.sh
      RUN bash /tmp/install_agent.sh

    对于 MinimalCodeAgent（自定义）：
      复用现有 build_swe_agent_base_image 中的逻辑

    Args:
        agent_name: agent 名称
        version: 可选版本号
        language: 语言标识（对 MinimalCodeAgent 需要）

    Returns:
        Dockerfile 指令块字符串
    """
    name = normalize_agent_name(agent_name)

    if is_harbor_supported(name):
        return _generate_harbor_agent_block(name, version)
    elif name == "minimal-code-agent":
        return _generate_minimal_code_agent_block(language)
    else:
        raise ValueError(
            f"Unknown agent: '{agent_name}'. "
            f"Supported Harbor agents: {HARBOR_SUPPORTED_AGENTS}; "
            f"Custom agents: minimal-code-agent"
        )


def _generate_harbor_agent_block(agent_name: str, version: str | None = None) -> str:
    """生成 Harbor agent 的 Dockerfile 指令块。"""
    return f"""
# ========== Harbor Agent: {agent_name} ==========
COPY install_agent.sh /tmp/install_agent.sh
RUN chmod +x /tmp/install_agent.sh && bash /tmp/install_agent.sh
"""


def _generate_minimal_code_agent_block(language: str = "py") -> str:
    """
    生成 MinimalCodeAgent 的 Dockerfile 指令块。
    复用 build_swe_agent_base_image 中的逻辑。
    """
    from build_swe_agent_base_image import _generate_miniconda_install_block

    miniconda_block = _generate_miniconda_install_block(language)
    conda_source = "/opt/miniconda3/etc/profile.d/conda.sh"

    return f"""
# ========== MinimalCodeAgent ==========
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
RUN apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null \\
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/root/.local/bin:$PATH"

ENV DISABLE_AUTOUPDATER=1

RUN mkdir -p /root/.claude
COPY claude_problem.json /root/.claude/settings.json

RUN claude --version

WORKDIR /testbed
"""


def _prepare_build_context(
    build_context_dir: Path,
    agent_name: str,
    version: str | None = None,
    language: str = "py",
) -> None:
    """
    准备构建上下文：将 agent 相关文件复制到 build_context_dir。

    Args:
        build_context_dir: Docker build context 目录
        agent_name: agent 名称
        version: 可选版本号
        language: 语言标识
    """
    name = normalize_agent_name(agent_name)

    if is_harbor_supported(name):
        # Harbor agent：渲染 install 脚本并写入 build context
        install_script = get_harbor_install_script(name, version)
        install_script_path = build_context_dir / "install_agent.sh"
        install_script_path.write_text(install_script, encoding="utf-8")
        logger.info(f"Wrote Harbor install script for {name} to {install_script_path}")

    elif name == "minimal-code-agent":
        # MinimalCodeAgent：复制必要文件
        script_dir = _get_script_dir()

        # 复制 Minimal-CodeAgent 代码
        agent_code_dir = script_dir / "Minimal-CodeAgent"
        if not agent_code_dir.exists():
            raise FileNotFoundError(f"Minimal-CodeAgent directory not found: {agent_code_dir}")

        build_agent_dir = build_context_dir / "Minimal-CodeAgent"
        if build_agent_dir.exists():
            shutil.rmtree(build_agent_dir)
        shutil.copytree(agent_code_dir, build_agent_dir)

        # 写入 claude settings
        from build_swe_agent_base_image import _load_dotenv, _make_claude_problem_json
        env_vars = _load_dotenv(script_dir) or _load_dotenv(script_dir.parent)
        token = env_vars.get("ANTHROPIC_AUTH_TOKEN", "")
        if not token:
            logger.warning(
                "ANTHROPIC_AUTH_TOKEN not set in .env; "
                "Claude Code may not authenticate in container."
            )
        claude_settings = _make_claude_problem_json(token)
        (build_context_dir / "claude_problem.json").write_text(
            json.dumps(claude_settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"Prepared MinimalCodeAgent build context in {build_context_dir}")
    else:
        raise ValueError(f"Unknown agent: '{agent_name}'")


def build_image_with_agent(
    dockerfile_content: str,
    agent_name: str,
    image_tag: str,
    build_context_dir: str | Path,
    client: docker.DockerClient,
    force_rebuild: bool = False,
    agent_version: str | None = None,
    language: str = "py",
    logger: logging.Logger | None = None,
) -> str:
    """
    统一入口：基于给定 Dockerfile 构建包含指定 agent 的 image。

    流程：
    1. 检查 image 是否已存在（非 force_rebuild 时跳过构建）
    2. 将 Dockerfile 内容 + agent 安装块合并为最终 Dockerfile
    3. 准备 build context（复制必要文件到 build_context_dir）
    4. 调用 client.images.build() 构建镜像
    5. 返回镜像 tag

    Args:
        dockerfile_content: Dockerfile 内容（字符串）
        agent_name: agent 名称（如 claude-code, aider, minimal-code-agent）
        image_tag: 目标镜像 tag
        build_context_dir: build context 目录
        client: Docker 客户端
        force_rebuild: 是否强制重建
        agent_version: 可选的 agent 版本号
        language: 语言标识（py, js, java 等）
        logger: Logger 实例

    Returns:
        image_tag: 构建好的镜像 tag
    """
    log = logger or logging.getLogger(__name__)
    build_context_dir = Path(build_context_dir)
    build_context_dir.mkdir(parents=True, exist_ok=True)

    # 1. 检查 image 是否已存在
    if not force_rebuild:
        try:
            client.images.get(image_tag)
            log.info(f"Image already exists: {image_tag}, skipping build")
            return image_tag
        except ImageNotFound:
            pass

    # 2. 生成 agent 安装块并追加到 Dockerfile
    name = normalize_agent_name(agent_name)
    agent_block = generate_agent_dockerfile_block(name, agent_version, language)
    final_dockerfile = dockerfile_content.rstrip() + "\n" + agent_block

    # 3. 准备 build context
    _prepare_build_context(build_context_dir, name, agent_version, language)

    # 写入最终 Dockerfile
    dockerfile_path = build_context_dir / "Dockerfile"
    dockerfile_path.write_text(final_dockerfile, encoding="utf-8")
    log.info(f"Generated final Dockerfile at {dockerfile_path}")

    # 4. 调用 docker build
    log.info(f"Building image: {image_tag} with agent: {name}")
    try:
        image, build_logs = client.images.build(
            path=str(build_context_dir),
            dockerfile=str(dockerfile_path),
            tag=image_tag,
            rm=True,
            forcerm=True,
        )
        for entry in build_logs:
            if "stream" in entry:
                line = entry["stream"].strip()
                if line:
                    log.debug(f"[docker build] {line}")
            if "error" in entry:
                log.error(f"[docker build error] {entry['error']}")
    except docker.errors.BuildError as e:
        log.error(f"Docker build failed for {image_tag}: {e}")
        # 打印构建日志以便调试
        for entry in e.build_log:
            if "stream" in entry:
                log.error(f"  {entry['stream'].strip()}")
            if "error" in entry:
                log.error(f"  ERROR: {entry['error']}")
        raise

    log.info(f"Successfully built image: {image_tag} (id: {image.id})")
    return image_tag


def build_image_from_dockerfile_path(
    dockerfile_path: str | Path,
    agent_name: str,
    image_tag: str,
    client: docker.DockerClient,
    **kwargs,
) -> str:
    """
    便捷函数：从文件路径读取 Dockerfile 并构建。
    用于 SWE-bench_Pro-os 预写 Dockerfile 场景。

    Args:
        dockerfile_path: Dockerfile 文件路径
        agent_name: agent 名称
        image_tag: 目标镜像 tag
        client: Docker 客户端
        **kwargs: 传递给 build_image_with_agent 的其他参数

    Returns:
        image_tag: 构建好的镜像 tag
    """
    dockerfile_path = Path(dockerfile_path)
    if not dockerfile_path.exists():
        raise FileNotFoundError(f"Dockerfile not found: {dockerfile_path}")

    dockerfile_content = dockerfile_path.read_text(encoding="utf-8")

    # 使用 Dockerfile 所在目录的父目录作为 build context（如未指定）
    if "build_context_dir" not in kwargs:
        kwargs["build_context_dir"] = dockerfile_path.parent

    return build_image_with_agent(
        dockerfile_content=dockerfile_content,
        agent_name=agent_name,
        image_tag=image_tag,
        client=client,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Harbor 对齐：Docker image 工具函数
# ---------------------------------------------------------------------------

def get_swebench_image_names(instance: dict) -> tuple[str, str]:
    """
    获取 SWE-bench 实例的 (base_image, env_image) 名称。

    Args:
        instance: SWE-bench 实例 dict（需要包含 instance_id, repo, base_commit 等）

    Returns:
        (base_image_key, env_image_key) 元组
    """
    from swebench.harness.test_spec.test_spec import make_test_spec
    test_spec = make_test_spec(
        instance,
        instance_image_tag="latest",
        env_image_tag="latest",
    )
    return test_spec.base_image_key, test_spec.env_image_key


# ---------------------------------------------------------------------------
# 与现有系统集成的辅助函数（旧流程使用，新流程通过 Harbor 管理）
# ---------------------------------------------------------------------------

def build_agent_image_for_test_spec(
    test_spec,
    agent_name: str,
    client: docker.DockerClient,
    swebench_logger: logging.Logger,
    force_rebuild: bool = False,
    image_suffix: str = "",
    agent_version: str | None = None,
) -> str:
    """
    为给定的 TestSpec 构建包含 agent 的 instance image。

    兼容现有系统：根据 test_spec 生成 Dockerfile 内容，然后调用 build_image_with_agent。
    同时保留现有的 repo_script 截断逻辑（对 Environment/FullPipe 类型）。

    Args:
        test_spec: swebench TestSpec 实例
        agent_name: agent 名称
        client: Docker 客户端
        swebench_logger: Logger
        force_rebuild: 是否强制重建
        image_suffix: 镜像后缀（如 ".noenvironment", ".withenvironment"）
        agent_version: 可选的 agent 版本号

    Returns:
        agent_instance_image_key: 构建好的镜像 tag
    """
    from agent_docker_utils import _insert_suffix_before_tag, truncate_repo_script_for_environment
    from swebench.harness.constants import INSTANCE_IMAGE_BUILD_DIR
    from swebench.harness.dockerfiles import get_dockerfile_env, get_dockerfile_instance

    language = getattr(test_spec, 'language', 'py')
    name = normalize_agent_name(agent_name)

    # 确定 env image
    if len(test_spec.env_script_list) == 0:
        # Environment/FullPipe: 需要一个基础镜像
        # 对于 Harbor agent，使用 swebench 的 base image
        # 对于 MinimalCodeAgent，需要 agent base image
        if name == "minimal-code-agent":
            from agent_docker_utils import get_agent_base_image
            agent_base_image = get_agent_base_image(test_spec.arch, language)
            if agent_base_image is None:
                raise RuntimeError(
                    f"Agent base image not found for language={language}. "
                    f"Run: python build_swe_agent_base_image.py --language {language}"
                )
            env_image_key = agent_base_image
        else:
            # Harbor agent: 使用 swebench base image key
            env_image_key = test_spec.base_image_key
    else:
        if name == "minimal-code-agent":
            # 需要 agent env image（包含 MinimalCodeAgent）
            from agent_docker_utils import build_agent_env_image
            env_image_key = build_agent_env_image(test_spec, client, swebench_logger, force_rebuild)
        else:
            # Harbor agent: 使用 swebench env image
            env_image_key = test_spec.env_image_key

    # 生成 instance image key
    if name == "minimal-code-agent":
        prefix = "ccb-agent"
    else:
        prefix = f"ccb-{name}"
    agent_instance_image_key = _insert_suffix_before_tag(
        f"{prefix}.{test_spec.instance_image_key}",
        image_suffix,
    )

    # 检查镜像是否已存在
    if not force_rebuild:
        try:
            client.images.get(agent_instance_image_key)
            swebench_logger.info(f"Agent instance image already exists: {agent_instance_image_key}")
            return agent_instance_image_key
        except ImageNotFound:
            pass

    swebench_logger.info(f"Building agent instance image: {agent_instance_image_key} (agent={name})")

    # 生成 instance Dockerfile（基于 env_image_key）
    instance_dockerfile = get_dockerfile_instance(
        test_spec.platform,
        test_spec.language,
        env_image_key,
    )

    # 准备 build context
    build_dir = INSTANCE_IMAGE_BUILD_DIR / agent_instance_image_key.replace(":", "__")
    build_dir.mkdir(parents=True, exist_ok=True)

    # 写入 setup_repo.sh
    if len(test_spec.env_script_list) == 0:
        # NoEnvironment：只保留 clone + checkout
        repo_list = truncate_repo_script_for_environment(test_spec.repo_script_list, language)
        if len(repo_list) == len(test_spec.repo_script_list):
            swebench_logger.warning(
                f"NoEnvironment: repo_script not truncated (language={language})"
            )
        setup_repo_script = "\n".join(["#!/bin/bash", "set -euxo pipefail"] + repo_list) + "\n"
    else:
        setup_repo_script = test_spec.install_repo_script

    (build_dir / "setup_repo.sh").write_text(setup_repo_script)

    # 对于 MinimalCodeAgent，不需要追加 agent 块（已在 base image 中）
    if name == "minimal-code-agent":
        # 直接用现有逻辑（agent 代码已在 base image 中）
        from swebench.harness.docker_build import build_image
        build_image(
            image_name=agent_instance_image_key,
            setup_scripts={"setup_repo.sh": setup_repo_script},
            dockerfile=instance_dockerfile,
            platform=test_spec.platform,
            client=client,
            build_dir=build_dir,
            nocache=force_rebuild,
        )
        swebench_logger.info(f"Successfully built: {agent_instance_image_key}")
        return agent_instance_image_key

    # Harbor agent: 在 instance Dockerfile 后追加 agent 安装块
    return build_image_with_agent(
        dockerfile_content=instance_dockerfile,
        agent_name=name,
        image_tag=agent_instance_image_key,
        build_context_dir=build_dir,
        client=client,
        force_rebuild=True,  # 已经检查过 exists，这里强制构建
        agent_version=agent_version,
        language=language,
        logger=swebench_logger,
    )


# ---------------------------------------------------------------------------
# CLI 入口（dry-run 验证）
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Harbor Agent Builder: 统一的 Agent Docker Image 构建工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # Dry-run: 查看生成的 Dockerfile
  python harbor_agent_builder.py --agent claude-code --dry-run

  # Dry-run with specific Dockerfile
  python harbor_agent_builder.py --agent claude-code --dockerfile-path /path/to/Dockerfile --dry-run

  # 列出支持的 agents
  python harbor_agent_builder.py --list-agents

  # 构建镜像
  python harbor_agent_builder.py --agent claude-code --dockerfile-path /path/to/Dockerfile --image-tag my-image:latest
        """,
    )
    parser.add_argument(
        "--agent",
        type=str,
        default="minimal-code-agent",
        help="Agent name (e.g. claude-code, aider, minimal-code-agent)",
    )
    parser.add_argument(
        "--agent-version",
        type=str,
        default=None,
        help="Agent version (optional)",
    )
    parser.add_argument(
        "--dockerfile-path",
        type=str,
        default=None,
        help="Path to base Dockerfile (content will be used as base)",
    )
    parser.add_argument(
        "--image-tag",
        type=str,
        default=None,
        help="Target image tag (required for actual build)",
    )
    parser.add_argument(
        "--language",
        type=str,
        default="py",
        help="Language (py, js, java, etc.)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print the generated Dockerfile, don't build",
    )
    parser.add_argument(
        "--list-agents",
        action="store_true",
        help="List all supported agents",
    )
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Force rebuild even if image exists",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    if args.list_agents:
        print("Harbor supported agents:")
        for a in HARBOR_SUPPORTED_AGENTS:
            print(f"  - {a}")
        print("\nCustom agents:")
        print("  - minimal-code-agent")
        return 0

    if args.dry_run:
        # 读取基础 Dockerfile
        if args.dockerfile_path:
            base_content = Path(args.dockerfile_path).read_text(encoding="utf-8")
        else:
            base_content = "FROM ubuntu:22.04\n# (base Dockerfile placeholder)"

        agent_block = generate_agent_dockerfile_block(
            args.agent, args.agent_version, args.language,
        )
        final_dockerfile = base_content.rstrip() + "\n" + agent_block

        print("=" * 80)
        print(f"Generated Dockerfile (agent={args.agent}):")
        print("=" * 80)
        print(final_dockerfile)

        # 如果是 Harbor agent，也显示 install 脚本
        name = normalize_agent_name(args.agent)
        if is_harbor_supported(name):
            print("=" * 80)
            print(f"Harbor install script for {name}:")
            print("=" * 80)
            try:
                script = get_harbor_install_script(name, args.agent_version)
                print(script)
            except (FileNotFoundError, ValueError) as e:
                print(f"Error: {e}")

        return 0

    # 实际构建
    if not args.image_tag:
        parser.error("--image-tag is required for actual build (use --dry-run for preview)")

    if not args.dockerfile_path:
        parser.error("--dockerfile-path is required for actual build")

    import config
    client = docker.from_env(timeout=config.DOCKER_CLIENT_TIMEOUT)

    image_tag = build_image_from_dockerfile_path(
        dockerfile_path=args.dockerfile_path,
        agent_name=args.agent,
        image_tag=args.image_tag,
        client=client,
        force_rebuild=args.force_rebuild,
        agent_version=args.agent_version,
        language=args.language,
    )
    print(f"\n\u2705 Successfully built: {image_tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
