#!/usr/bin/env python3
"""
构建包含 Agent 依赖的基础镜像

这个脚本会基于 swebench 的 Python base image，创建一个新的基础镜像，
其中预先安装好了 Agent 的依赖（从 Minimal-CodeAgent/requirements.txt）。

使用方法:
    python build_agent_base_image.py [--force-rebuild] [--base-image BASE_IMAGE]

参数:
    --force-rebuild: 强制重建镜像，即使已存在
    --base-image: 指定 swebench base image 名称（可选，默认会自动查找）
"""
import argparse
import logging
import subprocess
import sys
from pathlib import Path

import docker
from docker.errors import ImageNotFound

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Agent 基础镜像名称
AGENT_BASE_IMAGE_NAME = "ccb-agent-base:latest"
AGENT_BASE_IMAGE_NAME_X86 = "ccb-agent-base-x86_64:latest"
AGENT_BASE_IMAGE_NAME_ARM = "ccb-agent-base-arm64:latest"

# 默认的 swebench base image（Python）
DEFAULT_SWEBENCH_BASE_IMAGE = "sweb.base.py.x86_64:latest"


def find_swebench_base_image(client: docker.DockerClient, arch: str = "x86_64") -> str:
    """
    查找 swebench 的 Python base image
    
    Args:
        client: Docker client
        arch: 架构类型 (x86_64 或 arm64)
    
    Returns:
        base_image_name: 找到的 base image 名称
    """
    # 尝试查找常见的 swebench base image
    possible_names = [
        f"sweb.base.py.{arch}:latest",
        "sweb.base.py.x86_64:latest",
        "sweb.base.py:latest",
    ]
    
    for image_name in possible_names:
        try:
            client.images.get(image_name)
            logger.info(f"Found swebench base image: {image_name}")
            return image_name
        except ImageNotFound:
            continue
    
    # 如果没找到，返回默认名称（用户需要先构建 swebench base image）
    logger.warning(f"Could not find swebench base image. Will use default: {DEFAULT_SWEBENCH_BASE_IMAGE}")
    return DEFAULT_SWEBENCH_BASE_IMAGE


def build_agent_base_image(
    client: docker.DockerClient,
    base_image: str = None,
    arch: str = "x86_64",
    force_rebuild: bool = False
) -> str:
    """
    构建包含 Agent 依赖的基础镜像
    
    Args:
        client: Docker client
        base_image: swebench base image 名称（如果为 None，会自动查找）
        arch: 架构类型
        force_rebuild: 是否强制重建
    
    Returns:
        image_name: 构建的镜像名称
    """
    # 确定 base image
    if base_image is None:
        base_image = find_swebench_base_image(client, arch)
    
    # 检查 base image 是否存在
    try:
        client.images.get(base_image)
        logger.info(f"Using swebench base image: {base_image}")
    except ImageNotFound:
        logger.error(f"Base image not found: {base_image}")
        logger.error("Please build swebench base image first, or specify a different base image with --base-image")
        sys.exit(1)
    
    # 确定目标镜像名称
    if arch == "x86_64":
        target_image = AGENT_BASE_IMAGE_NAME_X86
    elif arch == "arm64":
        target_image = AGENT_BASE_IMAGE_NAME_ARM
    else:
        target_image = AGENT_BASE_IMAGE_NAME
    
    # 检查目标镜像是否已存在
    if not force_rebuild:
        try:
            client.images.get(target_image)
            logger.info(f"Agent base image already exists: {target_image}")
            logger.info("Use --force-rebuild to rebuild it")
            return target_image
        except ImageNotFound:
            pass
    
    # 获取 requirements.txt 和 agent 代码路径
    script_dir = Path(__file__).parent
    requirements_file = script_dir / "Minimal-CodeAgent" / "requirements.txt"
    agent_code_dir = script_dir / "Minimal-CodeAgent"
    
    if not requirements_file.exists():
        logger.error(f"Requirements file not found: {requirements_file}")
        sys.exit(1)
    
    if not agent_code_dir.exists():
        logger.error(f"Agent code directory not found: {agent_code_dir}")
        sys.exit(1)
    
    # 获取 agent 路径配置（从 config 或使用默认值）
    try:
        from config import AGENTPATH
        agent_path = AGENTPATH
    except ImportError:
        agent_path = "/MinimalCodeAgent"  # 默认路径
    
    logger.info(f"Building agent base image: {target_image}")
    logger.info(f"Base image: {base_image}")
    logger.info(f"Requirements: {requirements_file}")
    logger.info(f"Agent code: {agent_code_dir}")
    logger.info(f"Agent path in container: {agent_path}")
    
    # 创建临时 Dockerfile
    # 在基础镜像中预先创建 conda 环境、安装依赖，并复制 agent 代码
    dockerfile_content = f"""FROM {base_image}

# 设置工作目录
WORKDIR /tmp

# 复制 requirements.txt
COPY Minimal-CodeAgent/requirements.txt /tmp/agent_requirements.txt

# 初始化 conda（如果还没有）
RUN /bin/bash -c "source /opt/miniconda3/etc/profile.d/conda.sh && conda init --all" || true

# 预先创建 conda 环境并安装依赖（如果环境不存在）
# 注意：使用 minimalcodeagent 作为环境名称（与 docker_init.sh 一致）
RUN /bin/bash -c "source /opt/miniconda3/etc/profile.d/conda.sh && \
    if ! conda env list | grep -q '^minimalcodeagent '; then \
        echo 'Creating minimalcodeagent environment...' && \
        conda create -n minimalcodeagent python=3.10 -y && \
        conda activate minimalcodeagent && \
        pip install --no-cache-dir -r /tmp/agent_requirements.txt && \
        echo 'Agent dependencies installed in minimalcodeagent environment'; \
    else \
        echo 'minimalcodeagent environment already exists, installing/updating dependencies...' && \
        conda activate minimalcodeagent && \
        pip install --no-cache-dir -r /tmp/agent_requirements.txt; \
    fi"

# 将 requirements.txt 复制到固定位置，以便 docker_init.sh 可以检查
RUN mkdir -p /opt/agent_deps && cp /tmp/agent_requirements.txt /opt/agent_deps/requirements.txt

# 复制 agent 代码到容器（在 base image 中包含 agent 代码）
COPY Minimal-CodeAgent/ {agent_path}/
RUN chmod -R 755 {agent_path}

# 清理临时文件
RUN rm -f /tmp/agent_requirements.txt

# 设置工作目录
WORKDIR /testbed
"""
    
    # 创建临时构建目录
    build_dir = script_dir / ".agent_base_build"
    build_dir.mkdir(exist_ok=True)
    
    dockerfile_path = build_dir / "Dockerfile"
    dockerfile_path.write_text(dockerfile_content)
    
    # 复制 agent 代码和 requirements.txt 到构建目录
    import shutil
    build_agent_dir = build_dir / "Minimal-CodeAgent"
    if build_agent_dir.exists():
        shutil.rmtree(build_agent_dir)
    shutil.copytree(agent_code_dir, build_agent_dir)
    
    try:
        # 构建镜像
        logger.info("Building Docker image...")
        image, build_logs = client.images.build(
            path=str(build_dir),
            dockerfile=str(dockerfile_path),
            tag=target_image,
            rm=True,
            forcerm=True,
        )
        
        # 打印构建日志
        for log in build_logs:
            if 'stream' in log:
                logger.debug(log['stream'].strip())
            elif 'error' in log:
                logger.error(log['error'])
        
        logger.info(f"Successfully built agent base image: {target_image}")
        logger.info(f"Image ID: {image.id}")
        
        return target_image
        
    except Exception as e:
        logger.error(f"Failed to build agent base image: {e}")
        raise
    finally:
        #TODO: 清理临时文件（可选，保留以便调试）
        # shutil.rmtree(build_dir, ignore_errors=True)
        pass


def main():
    parser = argparse.ArgumentParser(
        description="Build agent base image with pre-installed dependencies"
    )
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Force rebuild the image even if it already exists"
    )
    parser.add_argument(
        "--base-image",
        type=str,
        default=None,
        help="Specify swebench base image name (default: auto-detect)"
    )
    parser.add_argument(
        "--arch",
        type=str,
        default="x86_64",
        choices=["x86_64", "arm64"],
        help="Architecture type (default: x86_64)"
    )
    args = parser.parse_args()
    
    import config
    client = docker.from_env(timeout=config.DOCKER_CLIENT_TIMEOUT)
    
    try:
        image_name = build_agent_base_image(
            client=client,
            base_image=args.base_image,
            arch=args.arch,
            force_rebuild=args.force_rebuild
        )
        print(f"\n✅ Success! Agent base image built: {image_name}")
        print(f"\nYou can now use this image to speed up agent initialization.")
        print(f"The docker_init.sh script will automatically use this image if it exists.")
    except Exception as e:
        logger.error(f"Build failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
