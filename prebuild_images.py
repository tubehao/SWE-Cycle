#!/usr/bin/env python3
"""
预构建 CCB 评测任务的 Docker image，避免每次 trial 重复 build。

支持两种使用方式：
1. 独立脚本：python prebuild_images.py --tasks-dir harbor_output/Development/tasks
2. 被 run_harbor.py 调用：prebuild_task_images(tasks_dir, ...)

核心逻辑：
- 根据 build context（Dockerfile + 所有文件）计算内容 hash
- image tag 包含 hash，模板/文件变化自动触发重建
- 已存在且 hash 匹配的 image 直接跳过
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Tuple

from tqdm.auto import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 核心函数（供 run_harbor.py 和独立脚本共享）
# ---------------------------------------------------------------------------

IMAGE_PREFIX = "ccb__"


_HASH_EXCLUDE_FILES = frozenset({
    "instruction.md",
    "eval_prompt.md",
    "docker-compose.yaml",
})


def context_hash(environment_dir: Path) -> str:
    """计算 Docker build context 的内容 hash（sha256 前 12 位）。

    遍历 environment_dir 下所有文件，按 *相对路径排序* 后逐个
    写入文件名 + 文件内容。这样只要文件内容不变，hash 就不变。

    排除 instruction.md / eval_prompt.md / docker-compose.yaml：
    它们是纯运行时文件（提示词 + compose 配置），不影响 Docker image
    构建产物。通过 volume mount 在运行时注入最新版。
    """
    h = hashlib.sha256()
    for f in sorted(environment_dir.rglob("*")):
        if f.is_file() and f.name not in _HASH_EXCLUDE_FILES:
            rel = f.relative_to(environment_dir)
            h.update(str(rel).encode("utf-8"))
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


def make_image_tag(task_dir: Path) -> str:
    """根据 task 目录名 + build context hash 生成确定性 image tag。

    格式: ccb__{instance_id}:{context_hash}
    例如: ccb__astropy__astropy-12907:a1b2c3d4e5f6
    """
    instance_id = task_dir.name.lower()
    env_dir = task_dir / "environment"
    if not env_dir.exists():
        raise FileNotFoundError(f"environment dir not found: {env_dir}")
    h = context_hash(env_dir)
    return f"{IMAGE_PREFIX}{instance_id}:{h}"


def image_exists(image_tag: str) -> bool:
    """检查本地是否已存在指定 tag 的 Docker image。"""
    result = subprocess.run(
        ["docker", "image", "inspect", image_tag],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def build_one_image(
    task_dir: Path,
    force: bool = False,
) -> Tuple[str, bool, Optional[str]]:
    """构建单个 task 的 Docker image。

    Returns:
        (image_tag, success, error_message)
    """
    env_dir = task_dir / "environment"
    if not (env_dir / "Dockerfile").exists():
        return ("", False, f"No Dockerfile in {env_dir}")

    try:
        tag = make_image_tag(task_dir)
    except Exception as e:
        return ("", False, str(e))

    if not force and image_exists(tag):
        logger.debug(f"Image exists, skipping: {tag}")
        return (tag, True, None)

    logger.info(f"Building: {tag}")
    result = subprocess.run(
        ["docker", "build", "-t", tag, str(env_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=3600,
    )
    if result.returncode != 0:
        output = result.stdout.decode(errors="replace")[-2000:]
        return (tag, False, f"docker build failed (rc={result.returncode}):\n{output}")

    return (tag, True, None)


def write_image_tag_to_task_toml(task_dir: Path, image_tag: str) -> None:
    """将 prebuilt image tag 写入 task.toml 的 [environment] 节。

    如果已有 docker_image 行则替换，否则在 [environment] 节末尾追加。
    """
    toml_path = task_dir / "task.toml"
    if not toml_path.exists():
        return

    lines = toml_path.read_text(encoding="utf-8").splitlines()
    new_line = f'docker_image = "{image_tag}"'

    # 查找并替换已有的 docker_image 行
    for i, line in enumerate(lines):
        if line.strip().startswith("docker_image"):
            lines[i] = new_line
            toml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return

    # 没找到，在 [environment] 节最后一个属性后追加
    in_env = False
    insert_idx = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[environment]":
            in_env = True
            continue
        if in_env:
            if stripped.startswith("[") and stripped != "[environment]":
                insert_idx = i
                break
            if stripped and "=" in stripped:
                insert_idx = i + 1

    lines.insert(insert_idx, new_line)
    toml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def remove_image_tag_from_task_toml(task_dir: Path) -> None:
    """从 task.toml 中移除 docker_image 行（--no-prebuild 时还原）。"""
    toml_path = task_dir / "task.toml"
    if not toml_path.exists():
        return

    lines = toml_path.read_text(encoding="utf-8").splitlines()
    new_lines = [l for l in lines if not l.strip().startswith("docker_image")]
    if len(new_lines) != len(lines):
        toml_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def prebuild_task_images(
    tasks_dir: Path,
    force: bool = False,
    max_workers: int = 4,
) -> Tuple[List[str], List[Tuple[str, str]]]:
    """预构建 tasks_dir 下所有 task 的 Docker image。

    Args:
        tasks_dir: 包含多个 task 子目录的父目录
        force: 强制重建所有 image（忽略已存在的）
        max_workers: 并行构建数

    Returns:
        (成功的 image tag 列表, 失败的 (task_name, error) 列表)
    """
    task_dirs = sorted([
        d for d in tasks_dir.iterdir()
        if d.is_dir() and (d / "environment" / "Dockerfile").exists()
    ])
    if not task_dirs:
        logger.warning(f"No tasks with Dockerfile found in {tasks_dir}")
        return [], []

    # 清理 stale tag：task.toml 中记录的 docker_image 如果本地已不存在，移除该字段
    stale_count = 0
    for d in task_dirs:
        toml_path = d / "task.toml"
        if not toml_path.exists():
            continue
        for line in toml_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("docker_image"):
                old_tag = line.split("=", 1)[1].strip().strip('"').strip("'")
                if old_tag and not image_exists(old_tag):
                    remove_image_tag_from_task_toml(d)
                    stale_count += 1
                break
    if stale_count:
        logger.info(f"Cleared {stale_count} stale docker_image tags from task.toml")

    logger.info(
        f"Pre-building {len(task_dirs)} Docker images "
        f"(workers={max_workers}, force={force})"
    )

    successes: List[str] = []
    failures: List[Tuple[str, str]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_dir = {
            executor.submit(build_one_image, d, force): d
            for d in task_dirs
        }
        with tqdm(total=len(task_dirs), desc="Pre-building images") as pbar:
            for future in as_completed(future_to_dir):
                task_dir = future_to_dir[future]
                try:
                    tag, ok, err = future.result()
                except Exception as e:
                    failures.append((task_dir.name, str(e)))
                    pbar.update(1)
                    continue

                if ok:
                    successes.append(tag)
                    write_image_tag_to_task_toml(task_dir, tag)
                else:
                    failures.append((task_dir.name, err or "unknown error"))
                pbar.update(1)

    logger.info(
        f"Pre-build complete: {len(successes)} succeeded, {len(failures)} failed"
    )
    for name, err in failures:
        logger.warning(f"  Failed: {name}: {err}")

    return successes, failures


def clean_prebuild_images(
    tasks_dir: Optional[Path] = None,
    all_ccb: bool = False,
    dry_run: bool = False,
) -> List[str]:
    """清理预构建的 Docker image。

    Args:
        tasks_dir: 只清理指定 tasks_dir 对应的 image
        all_ccb: 清理所有 ccb__ 前缀的 image
        dry_run: 只打印不删除

    Returns:
        被删除（或将被删除）的 image tag 列表
    """
    if all_ccb:
        result = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True, text=True,
        )
        targets = [
            line.strip() for line in result.stdout.splitlines()
            if line.strip().startswith(IMAGE_PREFIX)
        ]
    elif tasks_dir:
        targets = []
        for d in tasks_dir.iterdir():
            if not d.is_dir():
                continue
            env_dir = d / "environment"
            if not (env_dir / "Dockerfile").exists():
                continue
            try:
                tag = make_image_tag(d)
                if image_exists(tag):
                    targets.append(tag)
            except Exception:
                continue
    else:
        return []

    if not targets:
        logger.info("No prebuild images to clean.")
        return []

    for tag in targets:
        if dry_run:
            logger.info(f"  [dry-run] would remove: {tag}")
        else:
            subprocess.run(
                ["docker", "rmi", tag],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(f"  Removed: {tag}")

    return targets


# ---------------------------------------------------------------------------
# 独立脚本入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="预构建 CCB 评测任务的 Docker image",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 预构建已生成的 task 目录
  python prebuild_images.py --tasks-dir harbor_output/Development/tasks --max-workers 8

  # 强制重建
  python prebuild_images.py --tasks-dir harbor_output/Development/tasks --force

  # 预构建多个题型
  python prebuild_images.py \\
      --tasks-dirs harbor_output/Development/tasks \\
                   harbor_output/TestCase/tasks \\
                   harbor_output/Environment/tasks \\
                   harbor_output/FullPipe/tasks

  # 清理所有预构建 image
  python prebuild_images.py --clean --all

  # 只看会删哪些（不实际删除）
  python prebuild_images.py --clean --all --dry-run
""",
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # ---- build 子命令（默认）----
    build_parser = subparsers.add_parser("build", help="预构建 Docker image")
    build_parser.add_argument(
        "--tasks-dirs", nargs="+", type=Path, required=True,
        help="task 目录列表（每个目录下包含多个 instance 子目录）",
    )
    build_parser.add_argument(
        "--force", action="store_true",
        help="强制重建所有 image（忽略已存在的）",
    )
    build_parser.add_argument(
        "--max-workers", type=int, default=4,
        help="并行构建数（默认 4）",
    )

    # ---- clean 子命令 ----
    clean_parser = subparsers.add_parser("clean", help="清理预构建的 Docker image")
    clean_group = clean_parser.add_mutually_exclusive_group(required=True)
    clean_group.add_argument(
        "--tasks-dirs", nargs="+", type=Path,
        help="只清理指定 tasks_dir 对应的 image",
    )
    clean_group.add_argument(
        "--all", action="store_true",
        help="清理所有 ccb__ 前缀的 image",
    )
    clean_parser.add_argument(
        "--dry-run", action="store_true",
        help="只打印不删除",
    )

    args = parser.parse_args()

    # 默认子命令为 build（兼容不输入子命令的情况）
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "build":
        total_successes = 0
        total_failures = 0
        for td in args.tasks_dirs:
            if not td.exists():
                logger.warning(f"Tasks dir not found, skipping: {td}")
                continue
            logger.info(f"Processing: {td}")
            successes, failures = prebuild_task_images(
                td, force=args.force, max_workers=args.max_workers,
            )
            total_successes += len(successes)
            total_failures += len(failures)
        logger.info(
            f"All done: {total_successes} succeeded, {total_failures} failed"
        )
        return 1 if total_failures > 0 else 0

    elif args.command == "clean":
        if getattr(args, "all", False):
            removed = clean_prebuild_images(
                all_ccb=True, dry_run=args.dry_run,
            )
        else:
            removed = []
            for td in args.tasks_dirs:
                removed.extend(clean_prebuild_images(
                    tasks_dir=td, dry_run=args.dry_run,
                ))
        action = "Would remove" if args.dry_run else "Removed"
        logger.info(f"{action} {len(removed)} images")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
