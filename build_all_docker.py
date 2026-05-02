#!/usr/bin/env python3
"""
预构建评测/解题流程所需的全部 Docker 镜像，避免在 eval/solve 过程中因 build 失败而中断。

支持：
- 从本地 jsonl 文件加载实例（test_data-*.jsonl、FullPipeline.jsonl）
- 从 SWE-bench 数据集名加载（需 swebench 可用）
- 构建：SWE base + env 镜像；可选检查/构建 agent base；可选构建 agent instance 镜像（noenv / withenv）

用法示例：
  python build_all_docker.py --dataset-paths dataset/test_data-Development.jsonl dataset/test_data-Environment.jsonl
  python build_all_docker.py --dataset-paths dataset/SWE-bench_Verified_FullPipeline.jsonl --agent-noenv-only
  python build_all_docker.py --dataset-name princeton-nlp/SWE-bench_Verified --split test --max-workers 4
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tqdm.auto import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# 默认 tag，与 eval/solve 一致
LATEST = "latest"
NAMESPACE = None
INSTANCE_IMAGE_TAG = LATEST
ENV_IMAGE_TAG = LATEST


def load_instances_from_jsonl(path: Path) -> List[Dict[str, Any]]:
    """从 jsonl 文件加载实例列表（每行一个 JSON 对象）。"""
    instances = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(f"Skip invalid JSON line in {path}: {e}")
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("instance_id") and obj.get("repo") and obj.get("base_commit") is not None:
                instances.append(obj)
            else:
                logger.warning(f"Skip line missing instance_id/repo/base_commit in {path}")
    return instances


def load_instances_from_dataset_name(dataset_name: str, split: str = "test") -> List[Dict[str, Any]]:
    """从 SWE-bench 数据集名加载实例（需 swebench 可用）。"""
    try:
        from swebench_utils import load_swebench_dataset_wrapper
    except ImportError:
        raise ImportError(
            "swebench_utils not available. Use --dataset-paths with local jsonl instead."
        )
    dataset = load_swebench_dataset_wrapper(dataset_name, split)
    return list(dataset) if dataset else []


def ensure_agent_base_image(arch: str = "x86_64", build_if_missing: bool = False) -> bool:
    """
    检查 agent base 镜像是否存在；可选在缺失时调用 build_agent_claude_code_base_image 构建。
    返回 True 表示存在或构建成功，False 表示不存在且未构建。
    """
    try:
        from agent_docker_utils import get_agent_base_image
    except ImportError:
        logger.error("agent_docker_utils not available")
        return False

    if get_agent_base_image(arch) is not None:
        logger.info(f"Agent base image for {arch} already exists.")
        return True

    if not build_if_missing:
        logger.warning(
            "Agent base image not found. Run: python build_swe_agent_base_image.py [--arch ...]"
        )
        return False

    logger.info("Building agent base image...")
    cmd = [sys.executable, "build_swe_agent_base_image.py", "--arch", arch]
    ret = subprocess.run(cmd, check=False)
    if ret.returncode != 0:
        logger.error("Agent base image build failed.")
        return False
    return get_agent_base_image(arch) is not None


def build_swe_images(
    client: Any,
    instances: List[Dict[str, Any]],
    force_rebuild: bool = False,
    max_workers: int = 4,
) -> None:
    """构建 SWE base 与 env 镜像。"""
    from swebench.harness.docker_build import build_base_images, build_env_images

    if not instances:
        logger.info("No instances for SWE images, skipping.")
        return

    logger.info(f"Building SWE base + env images for {len(instances)} instances...")
    build_base_images(
        client,
        instances,
        force_rebuild=force_rebuild,
        namespace=NAMESPACE,
        instance_image_tag=INSTANCE_IMAGE_TAG,
        env_image_tag=ENV_IMAGE_TAG,
    )
    build_env_images(
        client,
        instances,
        force_rebuild=force_rebuild,
        max_workers=max_workers,
        namespace=NAMESPACE,
        instance_image_tag=INSTANCE_IMAGE_TAG,
        env_image_tag=ENV_IMAGE_TAG,
    )
    logger.info("SWE base + env images done.")


def _build_one_agent_instance(
    task: Tuple[Any, str],
    client: Any,
    force_rebuild: bool,
    log_dir: Path,
    retry: int,
    agent_name: Optional[str] = None,
) -> Tuple[str, bool, Optional[str]]:
    """
    构建单个 agent instance 镜像（带重试）。
    返回 (task_id, success, error_message)。
    """
    from swebench.harness.test_spec.test_spec import TestSpec
    from swebench.harness.docker_build import setup_logger, close_logger
    from harbor_agent_builder import build_agent_image_for_test_spec

    spec: TestSpec = task[0]
    suf: str = task[1]
    task_id = f"{spec.instance_id}{suf}"
    build_log = log_dir / f"{spec.instance_id}{suf.replace('.', '_')}.log"
    last_error: Optional[str] = None

    for attempt in range(1, retry + 2):  # 1 次首次 + retry 次重试
        swebench_logger = setup_logger(spec.instance_id, build_log, mode="a" if attempt > 1 else "w")
        try:
            build_agent_image_for_test_spec(
                test_spec=spec,
                agent_name=agent_name or "minimal-code-agent",
                client=client,
                swebench_logger=swebench_logger,
                force_rebuild=force_rebuild,
                image_suffix=suf,
            )
            try:
                close_logger(swebench_logger)
            except Exception:
                pass
            return (task_id, True, None)
        except Exception as e:
            last_error = str(e)
            try:
                close_logger(swebench_logger)
            except Exception:
                pass
            if attempt <= retry:
                logger.warning(f"Build {task_id} attempt {attempt} failed, retrying ({attempt}/{retry}): {e}")
            else:
                logger.error(f"Build {task_id} failed after {retry + 1} attempts: {e}")

    return (task_id, False, last_error)


def build_agent_instance_images(
    client: Any,
    instances: List[Dict[str, Any]],
    force_rebuild: bool = False,
    noenv_only: bool = True,
    arch: str = "x86_64",
    max_workers: int = 4,
    retry: int = 2,
    agent_name: Optional[str] = None,
) -> None:
    """
    为每个 instance 并行构建 agent instance 镜像（支持失败重试）。
    noenv_only=True 只构建 .noenvironment（Environment/FullPipe）；
    noenv_only=False 再构建 .withenvironment（Development/TestCase solve）。
    """
    from swebench.harness.test_spec.test_spec import make_test_spec
    from harbor_agent_builder import normalize_agent_name, is_harbor_supported

    resolved_agent = normalize_agent_name(agent_name or "minimal-code-agent")

    # 对于自定义 agent（如 MinimalCodeAgent），需要预置 agent base image
    if not is_harbor_supported(resolved_agent):
        from agent_docker_utils import get_agent_base_image
        if get_agent_base_image(arch) is None:
            logger.warning("Agent base image missing; skip building agent instance images.")
            return

    test_specs = []
    for inst in instances:
        try:
            spec = make_test_spec(
                inst,
                namespace=NAMESPACE,
                instance_image_tag=INSTANCE_IMAGE_TAG,
                env_image_tag=ENV_IMAGE_TAG,
                arch=arch,
            )
            test_specs.append(spec)
        except Exception as e:
            logger.warning(f"Skip instance {inst.get('instance_id', '?')}: {e}")

    if not test_specs:
        logger.info("No valid test_specs for agent instance images.")
        return

    suffixes = [".noenvironment"]
    if not noenv_only:
        suffixes.append(".withenvironment")

    tasks: List[Tuple[Any, str]] = []
    for spec in test_specs:
        for suf in suffixes:
            tasks.append((spec, suf))

    log_dir = Path("logs/build_images/agent_instances")
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        f"Building {len(tasks)} agent instance images in parallel (max_workers={max_workers}, retry={retry})..."
    )
    failed = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _build_one_agent_instance,
                task,
                client,
                force_rebuild,
                log_dir,
                retry,
                agent_name=agent_name,
            ): task
            for task in tasks
        }
        # 使用 tqdm 展示 agent instance 构建进度
        with tqdm(total=len(futures), desc="Agent instance images", unit="img") as pbar:
            for future in as_completed(futures):
                task = futures[future]
                try:
                    task_id, success, err = future.result()
                    if not success:
                        failed.append((task_id, err))
                except Exception as e:
                    task_id = f"{task[0].instance_id}{task[1]}"
                    failed.append((task_id, str(e)))
                finally:
                    pbar.update(1)

    if failed:
        logger.warning(f"Agent instance images: {len(failed)} failed after retries: {[f[0] for f in failed]}")
    else:
        logger.info("Agent instance images build pass done.")


def build_all_docker(
    instances: List[Dict[str, Any]],
    *,
    skip_swe: bool = False,
    skip_agent_base: bool = False,
    build_agent_base_if_missing: bool = False,
    skip_agent_instances: bool = False,
    agent_noenv_only: bool = True,
    force_rebuild: bool = False,
    max_workers: int = 4,
    agent_max_workers: int = 4,
    retry: int = 2,
    arch: str = "x86_64",
    agent_name: Optional[str] = None,
) -> None:
    """
    预构建所有镜像：SWE base/env，可选 agent base，可选 agent instance（noenv/withenv）。
    """
    import docker

    import config
    client = docker.from_env(timeout=config.DOCKER_CLIENT_TIMEOUT)

    if not instances:
        logger.warning("No instances loaded; nothing to build.")
        return

    logger.info(f"Pre-building Docker images for {len(instances)} instances.")

    if not skip_swe:
        build_swe_images(client, instances, force_rebuild=force_rebuild, max_workers=max_workers)
    else:
        logger.info("Skipping SWE base + env (--skip-swe).")

    # 对于自定义 agent（非 Harbor），需要确认 agent base image 存在
    from harbor_agent_builder import normalize_agent_name, is_harbor_supported
    resolved_agent = normalize_agent_name(agent_name or "minimal-code-agent")

    if not is_harbor_supported(resolved_agent):
        if not skip_agent_base or not skip_agent_instances:
            ok = ensure_agent_base_image(arch=arch, build_if_missing=build_agent_base_if_missing)
            if not ok and not skip_agent_instances:
                logger.warning("Skipping agent instance builds (agent base missing).")
                skip_agent_instances = True
    else:
        logger.info(f"Using Harbor agent '{resolved_agent}': skipping agent base image check.")

    if not skip_agent_instances:
        build_agent_instance_images(
            client,
            instances,
            force_rebuild=force_rebuild,
            noenv_only=agent_noenv_only,
            arch=arch,
            max_workers=agent_max_workers,
            retry=retry,
            agent_name=agent_name,
        )
    else:
        logger.info("Skipping agent instance images (--skip-agent-instances).")

    logger.info("Pre-build finished.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="预构建 eval/solve 所需的全部 Docker 镜像",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dataset-paths",
        nargs="+",
        type=Path,
        default=None,
        help="本地 jsonl 路径，可多个（如 test_data-Development.jsonl, SWE-bench_Verified_FullPipeline.jsonl）",
    )
    parser.add_argument(
        "--dataset-name",
        type=str,
        default=None,
        help="SWE-bench 数据集名（与 --split 一起使用，与 --dataset-paths 二选一）",
    )
    parser.add_argument(
        "--split",
        type=str,
        default="test",
        help="与 --dataset-name 一起使用",
    )
    parser.add_argument(
        "--skip-swe",
        action="store_true",
        help="跳过 SWE base + env 镜像构建",
    )
    parser.add_argument(
        "--skip-agent-base",
        action="store_true",
        help="不检查 agent base 镜像（若同时构建 agent instances 且 base 缺失会报错）",
    )
    parser.add_argument(
        "--build-agent-base-if-missing",
        action="store_true",
        help="若 agent base 不存在则调用 build_swe_agent_base_image.py 构建",
    )
    parser.add_argument(
        "--skip-agent-instances",
        action="store_true",
        help="不构建 agent instance 镜像",
    )
    parser.add_argument(
        "--agent-noenv-only",
        action="store_true",
        default=True,
        help="仅构建 .noenvironment agent 镜像（Environment/FullPipe 用），默认 True",
    )
    parser.add_argument(
        "--agent-withenv",
        action="store_true",
        help="同时构建 .withenvironment agent 镜像（Development/TestCase solve 用）；与 --agent-noenv-only 互斥，指定后构建两种",
    )
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="强制重建已存在的镜像",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=4,
        help="SWE base/env 镜像并行构建数",
    )
    parser.add_argument(
        "--agent-max-workers",
        type=int,
        default=4,
        help="Agent instance 镜像并行构建数",
    )
    parser.add_argument(
        "--retry",
        type=int,
        default=2,
        metavar="N",
        help="单个 agent instance 构建失败时的重试次数（默认 2，即最多共 3 次尝试）",
    )
    parser.add_argument(
        "--arch",
        type=str,
        default="x86_64",
        choices=("x86_64", "arm64"),
        help="架构",
    )
    parser.add_argument(
        "--agent",
        type=str,
        default=None,
        help="Agent name (e.g. claude-code, aider, minimal-code-agent). Default: minimal-code-agent",
    )

    args = parser.parse_args()

    instances: List[Dict[str, Any]] = []

    if args.dataset_paths:
        for p in args.dataset_paths:
            if not p.exists():
                logger.error(f"File not found: {p}")
                sys.exit(1)
            loaded = load_instances_from_jsonl(p)
            instances.extend(loaded)
            logger.info(f"Loaded {len(loaded)} instances from {p}")
    elif args.dataset_name:
        instances = load_instances_from_dataset_name(args.dataset_name, args.split)
        logger.info(f"Loaded {len(instances)} instances from {args.dataset_name} ({args.split})")
    else:
        logger.error("Specify either --dataset-paths or --dataset-name")
        sys.exit(1)

    # 去重（按 instance_id），保留首次出现
    seen = set()
    unique = []
    for inst in instances:
        iid = inst.get("instance_id")
        if iid and iid not in seen:
            seen.add(iid)
            unique.append(inst)
    instances = unique
    logger.info(f"Total unique instances: {len(instances)}")

    if not instances:
        logger.error("No instances to build for.")
        sys.exit(1)

    build_all_docker(
        instances,
        skip_swe=args.skip_swe,
        skip_agent_base=args.skip_agent_base,
        build_agent_base_if_missing=args.build_agent_base_if_missing,
        skip_agent_instances=args.skip_agent_instances,
        agent_noenv_only=not args.agent_withenv,
        force_rebuild=args.force_rebuild,
        max_workers=args.max_workers,
        agent_max_workers=args.agent_max_workers,
        retry=args.retry,
        arch=args.arch,
        agent_name=args.agent,
    )


if __name__ == "__main__":
    main()
