#!/usr/bin/env python3
"""
run_filtering.py — 数据集筛选管道统一入口

串行执行两个筛选步骤，记录每步筛掉的数量，生成最终报告。

筛选顺序：
  f1. Verified 白名单过滤（仅 SWE-bench_Verified）
  f3. 行为污染检测（blind mode 做题：不给 problem_statement，做对则视为污染）

Usage:
  # 运行全部筛选步骤
  python filtering/run_filtering.py

  # 只运行 f1（白名单）
  python filtering/run_filtering.py --steps f1

  # f3: 跳过做题，直接解析已有 job 结果
  python filtering/run_filtering.py --steps f3 --f3-job-dir <path_to_job_dir>

  # f3: 指定题型
  python filtering/run_filtering.py --steps f3 --problem-type Development
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# 确保 filtering 包可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from filtering.config import DATASET_CONFIGS, RESULTS_DIR
from filtering.filters import (
    FilterResult,
    load_hf_dataset,
    load_jsonl,
    save_results,
    summarize_results,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

def load_all_datasets() -> dict[str, list[dict]]:
    """加载三个数据集，返回 {dataset_name: [instance_dict, ...]}。"""
    all_data = {}
    for ds_name, cfg in DATASET_CONFIGS.items():
        logger.info(f"Loading {ds_name}...")
        if cfg["type"] == "hf":
            instances = load_hf_dataset(cfg["name"], cfg["split"])
        else:
            instances = load_jsonl(cfg["path"])
        logger.info(f"  {ds_name}: {len(instances)} instances")
        all_data[ds_name] = instances
    return all_data


# ---------------------------------------------------------------------------
# 筛选管道
# ---------------------------------------------------------------------------

def _load_presolved_ids(path: Path) -> dict[str, set[str]]:
    """从 presolved JSONL 加载各数据集的已做对 instance_id 集合。

    JSONL 格式：{"instance_id": "...", "solved": true/false}
    仅返回 solved=True 的 ID。按 dataset 分组（目前通过 instance_id 前缀推断）。
    """
    solved_ids: set[str] = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("solved"):
                solved_ids.add(record["instance_id"])
    logger.info(f"Loaded {len(solved_ids)} pre-solved instance_ids from {path}")
    return solved_ids


def run_pipeline(
    steps: list[str],
    problem_type: str = "Development",
    f3_job_dir: Path | None = None,
    f3_skip_solve: bool = False,
    presolved_file: Path | None = None,
    n_concurrent: int = 4,
    shard: int | None = None,
    total_shards: int | None = None,
    output_dir: Path | None = None,
) -> dict:
    """执行筛选管道。

    Args:
        steps: 要运行的步骤列表（如 ["f1", "f3"]）
        problem_type: f3 做题用的题型
        f3_job_dir: f3 直接解析的 job 目录（跳过做题）
        f3_skip_solve: f3 跳过做题
        presolved_file: 预做题结果文件（JSONL），仅 solved=True 的 instance 做 blind test
        n_concurrent: f3 做题并发数
        shard: 分片索引（0-indexed）
        total_shards: 总分片数
        output_dir: f3 输出及报告目录（默认 RESULTS_DIR）

    Returns:
        报告 dict
    """
    results_dir = output_dir or RESULTS_DIR
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    # 加载数据
    all_data = load_all_datasets()

    # 当前通过的实例集合（每步过滤后更新）
    # {dataset_name: [instance_dict, ...]}
    current_data = {ds: list(insts) for ds, insts in all_data.items()}

    report = {
        "steps_executed": [],
        "datasets": {},
        "overall": {},
    }

    # 初始化 report.datasets
    for ds_name, instances in all_data.items():
        report["datasets"][ds_name] = {
            "total": len(instances),
        }

    # ---- f1: Verified 白名单过滤 ----
    if "f1" in steps:
        logger.info("=" * 60)
        logger.info("Step f1: Verified whitelist filtering")
        logger.info("=" * 60)

        from filtering.filters.f1_verified_whitelist import run as f1_run

        all_f1_results: list[FilterResult] = []
        for ds_name, instances in current_data.items():
            results = f1_run(instances, ds_name)
            all_f1_results.extend(results)

            # 更新通过集
            passed_ids = {r.instance_id for r in results if r.passed}
            current_data[ds_name] = [
                inst for inst in instances if inst["instance_id"] in passed_ids
            ]

        # 保存结果
        save_results(all_f1_results, RESULTS_DIR / "f1_whitelist_result.jsonl")
        summary = summarize_results(all_f1_results, "f1_verified_whitelist")
        report["steps_executed"].append(summary)
        _print_step_summary(summary)

        for ds_name in report["datasets"]:
            report["datasets"][ds_name]["f1_filtered"] = summary["by_dataset"].get(
                ds_name, {}
            ).get("filtered", 0)
            report["datasets"][ds_name]["after_f1"] = len(current_data[ds_name])

    # ---- f3: 行为污染检测（blind mode 做题） ----
    if "f3" in steps:
        logger.info("=" * 60)
        logger.info("Step f3: Blind mode contamination detection")
        logger.info("=" * 60)

        from filtering.filters.f3_longcat_solve import run as f3_run

        pre_solved_ids = None
        if presolved_file:
            pre_solved_ids = _load_presolved_ids(presolved_file)

        all_f3_results: list[FilterResult] = []
        for ds_name, instances in current_data.items():
            cfg = DATASET_CONFIGS[ds_name]
            dataset_source = (
                {"type": "hf", "name": cfg["name"], "split": cfg["split"]}
                if cfg["type"] == "hf"
                else {"type": "jsonl", "path": str(cfg["path"])}
            )

            results = f3_run(
                instances=instances,
                dataset_name=ds_name,
                dataset_source=dataset_source,
                problem_type=problem_type,
                skip_solve=f3_skip_solve,
                job_dir=f3_job_dir,
                pre_solved_ids=pre_solved_ids,
                n_concurrent=n_concurrent,
                shard=shard,
                total_shards=total_shards,
                output_dir=results_dir / "longcat_solve" if output_dir else None,
            )
            all_f3_results.extend(results)

            # 更新通过集
            passed_ids = {r.instance_id for r in results if r.passed}
            current_data[ds_name] = [
                inst for inst in instances if inst["instance_id"] in passed_ids
            ]

        save_results(all_f3_results, results_dir / "f3_solve_result.jsonl")
        summary = summarize_results(all_f3_results, "f3_longcat_solve")
        report["steps_executed"].append(summary)
        _print_step_summary(summary)

        for ds_name in report["datasets"]:
            report["datasets"][ds_name]["f3_filtered"] = summary["by_dataset"].get(
                ds_name, {}
            ).get("filtered", 0)
            report["datasets"][ds_name]["after_f3"] = len(current_data[ds_name])

    # ---- 最终统计 ----
    total_input = sum(len(insts) for insts in all_data.values())
    total_remaining = sum(len(insts) for insts in current_data.values())

    report["overall"] = {
        "total_input": total_input,
        "total_remaining": total_remaining,
        "total_filtered": total_input - total_remaining,
    }

    for ds_name in report["datasets"]:
        report["datasets"][ds_name]["final_remaining"] = len(current_data[ds_name])

    # 写报告
    report_path = results_dir / "filtering_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    logger.info(f"Report saved to {report_path}")

    # 写最终保留的 instance_id 列表
    final_path = results_dir / "final_filtered.jsonl"
    with open(final_path, "w", encoding="utf-8") as f:
        for ds_name, instances in current_data.items():
            for inst in instances:
                f.write(json.dumps({
                    "instance_id": inst["instance_id"],
                    "dataset": ds_name,
                    "repo": inst.get("repo", ""),
                }, ensure_ascii=False) + "\n")
    logger.info(f"Final filtered list saved to {final_path} ({total_remaining} instances)")

    # 打印最终报告
    _print_final_report(report)

    return report


# ---------------------------------------------------------------------------
# 打印工具
# ---------------------------------------------------------------------------

def _print_step_summary(summary: dict) -> None:
    """打印单步筛选摘要。"""
    print(f"\n  [{summary['filter']}] "
          f"Total: {summary['total']} → "
          f"Passed: {summary['passed']}, "
          f"Filtered: {summary['filtered']}")

    if summary["reason_counts"]:
        print("  Filtered by reason:")
        for reason, count in sorted(summary["reason_counts"].items()):
            print(f"    {reason}: {count}")

    print("  By dataset:")
    for ds, info in summary["by_dataset"].items():
        print(f"    {ds}: {info['total']} → passed {info['passed']}, filtered {info['filtered']}")


def _print_final_report(report: dict) -> None:
    """打印最终筛选报告。"""
    print("\n" + "=" * 80)
    print("FILTERING REPORT")
    print("=" * 80)

    for ds_name, info in report["datasets"].items():
        print(f"\n  {ds_name}:")
        print(f"    Total input:      {info['total']}")
        for step in report["steps_executed"]:
            step_name = step["filter"]
            key = f"{step_name.split('_')[0]}_filtered"
            if key in info:
                print(f"    {step_name}: -{info[key]}")
        print(f"    Final remaining:  {info.get('final_remaining', '?')}")

    overall = report["overall"]
    print(f"\n  Overall:")
    print(f"    Total input:     {overall['total_input']}")
    print(f"    Total filtered:  {overall['total_filtered']}")
    print(f"    Total remaining: {overall['total_remaining']}")
    print("=" * 80)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="SWE-Cycle 数据集筛选管道",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--steps", nargs="+", default=["f1", "f3"],
        choices=["f1", "f3"],
        help="要执行的筛选步骤（默认全部）",
    )
    parser.add_argument(
        "--problem-type", default="Development",
        choices=["Development", "TestCase", "Environment", "FullPipe"],
        help="f3 做题用的题型（默认 Development）",
    )
    parser.add_argument(
        "--f3-job-dir", type=Path, default=None,
        help="f3: 直接解析已有的 Harbor job 目录（跳过做题）",
    )
    parser.add_argument(
        "--f3-skip-solve", action="store_true",
        help="f3: 跳过做题步骤",
    )
    parser.add_argument(
        "--presolved-file", type=Path, default=None,
        help="预做题结果文件（JSONL），仅对 solved=True 的 instance 执行 blind mode",
    )
    parser.add_argument(
        "--n-concurrent", type=int, default=4,
        help="f3 做题并发数（默认 4）",
    )
    parser.add_argument(
        "--shard", type=int, default=None,
        help="分片索引（0-indexed），配合 --total-shards 多机并行",
    )
    parser.add_argument(
        "--total-shards", type=int, default=None,
        help="总分片数，配合 --shard 多机并行",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=None,
        help="f3 输出及报告目录（默认 filtering/results）",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="启用 debug 日志",
    )

    args = parser.parse_args()

    # shard 参数校验
    if (args.shard is None) != (args.total_shards is None):
        parser.error("--shard 和 --total-shards 必须同时指定")
    if args.shard is not None and (args.shard < 0 or args.shard >= args.total_shards):
        parser.error(f"--shard 必须在 [0, {args.total_shards}) 范围内")

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )

    run_pipeline(
        steps=args.steps,
        problem_type=args.problem_type,
        f3_job_dir=args.f3_job_dir,
        f3_skip_solve=args.f3_skip_solve,
        presolved_file=args.presolved_file,
        n_concurrent=args.n_concurrent,
        shard=args.shard,
        total_shards=args.total_shards,
        output_dir=args.output_dir,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
