#!/usr/bin/env python3
"""
run_gold_validation.py — Pro + Multilingual 数据集 gold patch 两步验证

两步验证逻辑：
  Step 1（无 gold patch）：apply test_patch → 运行测试
      → 要求 FAIL_TO_PASS 全部 fail，PASS_TO_PASS 全部 pass
  Step 2（有 gold patch）：apply gold + test_patch → 运行测试
      → 要求全部 pass

Usage:
  # 全量运行（Pro + Multilingual，两步）
  python filtering/run_gold_validation.py

  # 仅 Pro Step 1
  python filtering/run_gold_validation.py --dataset pro --step step1

  # 仅 Multilingual Step 2
  python filtering/run_gold_validation.py --dataset multilingual --step step2

  # 指定实例
  python filtering/run_gold_validation.py --dataset pro --instance-ids id1 id2

  # 自定义输出和并发
  python filtering/run_gold_validation.py --output-dir /tmp/gold_val --max-workers 8
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from filtering.gold_validators import multilingual as ml_validator
from filtering.gold_validators import pro as pro_validator

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "gold_validation_results"


def run_pipeline(
    datasets: list[str],
    steps: list[str],
    max_workers: int = 4,
    timeout: int = 1800,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    instance_ids: list[str] | None = None,
    dockerhub_username: str = pro_validator.DEFAULT_DOCKERHUB_USERNAME,
    redo: bool = False,
) -> dict:
    """运行验证管道。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {"datasets": {}}

    if "multilingual" in datasets:
        logger.info("=" * 60)
        logger.info("Multilingual gold patch validation")
        logger.info("=" * 60)
        ml_results = ml_validator.validate(
            output_dir=output_dir,
            steps=steps,
            max_workers=max_workers,
            timeout=timeout,
            instance_ids=instance_ids,
        )
        total = len(ml_results)
        passed = sum(1 for v in ml_results.values() if v["validated"])
        summary["datasets"]["multilingual"] = {
            "total": total,
            "validated": passed,
            "filtered": total - passed,
        }

    if "pro" in datasets:
        logger.info("=" * 60)
        logger.info("Pro gold patch validation")
        logger.info("=" * 60)
        pro_results = pro_validator.validate(
            output_dir=output_dir,
            steps=steps,
            max_workers=max_workers,
            dockerhub_username=dockerhub_username,
            instance_ids=instance_ids,
            redo=redo,
        )
        total = len(pro_results)
        passed = sum(1 for v in pro_results.values() if v["validated"])
        summary["datasets"]["pro"] = {
            "total": total,
            "validated": passed,
            "filtered": total - passed,
        }

    # 总计
    total_all = sum(d["total"] for d in summary["datasets"].values())
    validated_all = sum(d["validated"] for d in summary["datasets"].values())
    summary["overall"] = {
        "total": total_all,
        "validated": validated_all,
        "filtered": total_all - validated_all,
    }

    summary_path = output_dir / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    logger.info(f"Summary → {summary_path}")

    _print_summary(summary)
    return summary


def _print_summary(summary: dict) -> None:
    """打印最终汇总。"""
    print("\n" + "=" * 60)
    print("GOLD VALIDATION SUMMARY")
    print("=" * 60)

    for ds_name, info in summary["datasets"].items():
        print(f"\n  {ds_name}:")
        print(f"    Total:     {info['total']}")
        print(f"    Validated: {info['validated']}")
        print(f"    Filtered:  {info['filtered']}")

    overall = summary["overall"]
    print(f"\n  Overall:")
    print(f"    Total:     {overall['total']}")
    print(f"    Validated: {overall['validated']}")
    print(f"    Filtered:  {overall['filtered']}")
    print("=" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pro + Multilingual gold patch 两步验证筛选",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dataset",
        nargs="+",
        default=["pro", "multilingual"],
        choices=["pro", "multilingual"],
        help="要验证的数据集（默认全部）",
    )
    parser.add_argument(
        "--step",
        nargs="+",
        default=["step1", "step2"],
        choices=["step1", "step2"],
        help="要执行的步骤（默认两步都执行）",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=4,
        help="并发 worker 数（默认 4）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Multilingual 单实例超时秒数（默认 1800）",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="输出目录（默认 filtering/gold_validation_results/）",
    )
    parser.add_argument(
        "--instance-ids",
        nargs="+",
        default=None,
        help="指定实例 ID（可选）",
    )
    parser.add_argument(
        "--dockerhub-username",
        default=pro_validator.DEFAULT_DOCKERHUB_USERNAME,
        help=f"DockerHub 用户名（默认 {pro_validator.DEFAULT_DOCKERHUB_USERNAME}）",
    )
    parser.add_argument(
        "--redo",
        action="store_true",
        help="重新运行已有结果的实例",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="启用 debug 日志",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )

    run_pipeline(
        datasets=args.dataset,
        steps=args.step,
        max_workers=args.max_workers,
        timeout=args.timeout,
        output_dir=args.output_dir,
        instance_ids=args.instance_ids,
        dockerhub_username=args.dockerhub_username,
        redo=args.redo,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
