"""
f3_longcat_solve.py — 行为污染检测（blind mode 做题筛选）

使用 longcat 模型通过 opencode agent 在 blind mode 下做题（不给 problem_statement），
基于脚本评测（script_reward）判断题目是否被模型训练数据污染：
  - script_reward > 0 → 模型在不知道问题描述的情况下做对了，标记为 contaminated，筛掉
  - Docker build 失败 / opencode 运行出错 → 标记为 opencode_error，筛掉
  - 其他（做题失败）→ 保留

流程：
  1. 对每个数据集调用 run_harbor.py（opencode agent + longcat 模型 + --blind-mode）
  2. 解析 trial_analysis.jsonl，提取 script_reward 和 status
  3. 汇总结果
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from filtering.config import LONGCAT_OPENCODE_MODEL, PROJECT_ROOT, RESULTS_DIR
from filtering.filters import FilterResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 做题输出目录
# ---------------------------------------------------------------------------

SOLVE_OUTPUT_DIR = RESULTS_DIR / "longcat_solve"


# ---------------------------------------------------------------------------
# 调用 run_harbor.py
# ---------------------------------------------------------------------------

def _run_harbor_solve(
    dataset_source: dict,
    problem_type: str,
    instance_ids: list[str] | None = None,
    output_dir: Path | None = None,
    n_concurrent: int = 4,
) -> Path:
    """调用 run_harbor.py 用 opencode + longcat 做题。

    Args:
        dataset_source: {"type": "hf", "name": ..., "split": ...}
                     或 {"type": "jsonl", "path": ...}
        problem_type: "Development" / "TestCase" / "Environment" / "FullPipe"
        instance_ids: 要跑的 instance_id 列表（None 表示全跑）
        output_dir: 输出目录
        n_concurrent: 并发 trial 数

    Returns:
        Harbor job 目录路径
    """
    if not LONGCAT_OPENCODE_MODEL:
        raise NotImplementedError("LONGCAT_OPENCODE_MODEL not configured — set it in filtering/config.py")

    if output_dir is None:
        output_dir = SOLVE_OUTPUT_DIR

    cmd = [
        sys.executable, str(PROJECT_ROOT / "run_harbor.py"),
        "--problem-type", problem_type,
        "--agent", "opencode",
        "--model-name", "deepseek-chat",
        "--n-attempts", "1",
        "--n-concurrent", str(n_concurrent),
        "--output-dir", str(output_dir),
        "--eval-model", "none",
        "--blind-mode",
        "--resume",
    ]

    if dataset_source["type"] == "hf":
        cmd += ["--dataset", dataset_source["name"]]
        if "split" in dataset_source:
            cmd += ["--split", dataset_source["split"]]
    else:
        cmd += ["--dataset-path", str(dataset_source["path"])]

    if instance_ids:
        cmd += ["--instance-ids"] + instance_ids

    logger.info(f"Running Harbor solve: {' '.join(cmd)}")

    result = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        capture_output=False,  # 让输出直接打印到终端
    )

    if result.returncode != 0:
        logger.warning(f"run_harbor.py exited with code {result.returncode}")

    # 找到最新的 job 目录
    jobs_dir = output_dir / problem_type / "jobs"
    if not jobs_dir.exists():
        raise FileNotFoundError(f"Jobs directory not found: {jobs_dir}")

    job_dirs = sorted(jobs_dir.iterdir(), key=lambda d: d.name, reverse=True)
    if not job_dirs:
        raise FileNotFoundError(f"No job directories found in {jobs_dir}")

    return job_dirs[0]


# ---------------------------------------------------------------------------
# 解析做题结果
# ---------------------------------------------------------------------------

def _parse_trial_analysis(job_dir: Path) -> dict[str, dict[str, Any]]:
    """从 trial_analysis.jsonl 解析做题结果。

    Returns:
        {instance_id: {"status": ..., "script_reward": ..., "reward": ..., ...}}
    """
    analysis_path = job_dir / "trial_analysis.jsonl"
    if not analysis_path.exists():
        logger.warning(f"trial_analysis.jsonl not found in {job_dir}")
        return {}

    results: dict[str, dict[str, Any]] = {}
    with open(analysis_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                iid = record.get("instance_id", "")
                if iid:
                    # 如果同一个 instance 有多个 trial，取第一个
                    if iid not in results:
                        results[iid] = record
            except json.JSONDecodeError:
                continue

    return results


def _classify_trial(trial: dict[str, Any]) -> tuple[bool, str]:
    """根据 blind mode 做题结果判断是否应被筛掉。

    Returns:
        (passed, reason):
          - (False, "contaminated") — 模型在无 problem_statement 情况下做对了，疑似训练数据污染
          - (False, "opencode_error") — 基础设施错误（Docker build 失败、opencode 崩溃等）
          - (True, "ok") — 模型没做对，保留
    """
    status = trial.get("status", "")
    reward = trial.get("reward", 0.0)
    script_reward = trial.get("script_reward")

    if status == "error":
        return False, "opencode_error"

    effective_reward = script_reward if script_reward is not None else reward
    try:
        if float(effective_reward) > 0:
            return False, "contaminated"
    except (TypeError, ValueError):
        pass

    return True, "ok"


# ---------------------------------------------------------------------------
# 主逻辑
# ---------------------------------------------------------------------------

def run(
    instances: list[dict],
    dataset_name: str,
    dataset_source: dict,
    problem_type: str = "Development",
    skip_solve: bool = False,
    job_dir: Path | None = None,
    pre_solved_ids: set[str] | None = None,
    n_concurrent: int = 4,
    shard: int | None = None,
    total_shards: int | None = None,
    output_dir: Path | None = None,
) -> list[FilterResult]:
    """执行行为污染检测（blind mode 做题筛选）。

    Args:
        instances: 通过上一步筛选的实例列表
        dataset_name: 数据集名称
        dataset_source: 数据集来源（传给 _run_harbor_solve）
        problem_type: 题型（默认 Development）
        skip_solve: 跳过做题，直接解析已有结果
        job_dir: 直接指定 job 目录（跳过做题）
        pre_solved_ids: 已被模型正常做对的 instance_id 集合。
            仅对这些 instance 执行 blind mode 做题；
            不在此集合中的 instance 直接标记为 passed（保留）。
            若为 None，则对所有 instance 执行 blind mode。
        n_concurrent: 并发 trial 数
        shard: 当前分片索引（0-indexed），配合 total_shards 实现多机切分
        total_shards: 总分片数

    Returns:
        每个实例的 FilterResult
    """
    results = []

    if pre_solved_ids is not None:
        need_blind = [inst for inst in instances if inst["instance_id"] in pre_solved_ids]
        skip_instances = [inst for inst in instances if inst["instance_id"] not in pre_solved_ids]

        for inst in skip_instances:
            results.append(FilterResult(
                instance_id=inst["instance_id"],
                dataset=dataset_name,
                passed=True,
                reason="not_solved",
                details={"note": "not solved in normal mode, skip blind test"},
            ))

        logger.info(
            f"[{dataset_name}] pre_solved_ids: {len(pre_solved_ids)} solved, "
            f"{len(need_blind)} need blind test, {len(skip_instances)} auto-pass"
        )
    else:
        need_blind = list(instances)

    if not need_blind:
        return results

    # Step 1: 分片（在做题之前，确保每个 shard 有独立的 instance 集合）
    if total_shards is not None and total_shards > 1 and shard is not None:
        need_blind.sort(key=lambda inst: inst["instance_id"])
        total_before = len(need_blind)
        this_shard = need_blind[shard::total_shards]
        other_shard = [inst for inst in need_blind if inst not in this_shard]
        for inst in other_shard:
            results.append(FilterResult(
                instance_id=inst["instance_id"],
                dataset=dataset_name,
                passed=True,
                reason="other_shard",
                details={"note": f"belongs to another shard (this={shard}/{total_shards})"},
            ))
        need_blind = this_shard
        logger.info(
            f"[{dataset_name}] Shard {shard}/{total_shards}: "
            f"{len(need_blind)}/{total_before} instances for this shard"
        )

    if not need_blind:
        logger.info(f"[{dataset_name}] No instances for this shard")
        return results

    # Step 2: 计算 shard 专属 output_dir
    solve_dir = output_dir if output_dir is not None else SOLVE_OUTPUT_DIR
    if total_shards is not None and total_shards > 1 and shard is not None:
        solve_dir = solve_dir / f"shard_{shard}"
    logger.info(f"[{dataset_name}] Output dir: {solve_dir}")

    instance_ids = [inst["instance_id"] for inst in need_blind]

    # Step 3: 做题（Harbor --resume 在 shard 专属目录内自行恢复）
    if job_dir is None and not skip_solve:
        try:
            job_dir = _run_harbor_solve(
                dataset_source=dataset_source,
                problem_type=problem_type,
                instance_ids=instance_ids,
                output_dir=solve_dir,
                n_concurrent=n_concurrent,
            )
        except NotImplementedError as e:
            logger.warning(f"Skipping solve: {e}")
            results.extend(
                FilterResult(
                    instance_id=iid,
                    dataset=dataset_name,
                    passed=True,
                    reason="pending",
                    details={"note": str(e)},
                )
                for iid in instance_ids
            )
            return results

    # Step 4: 解析结果
    trial_results = _parse_trial_analysis(job_dir) if job_dir else {}

    for inst in need_blind:
        iid = inst["instance_id"]
        trial = trial_results.get(iid)

        if trial is None:
            results.append(FilterResult(
                instance_id=iid,
                dataset=dataset_name,
                passed=True,
                reason="no_trial",
                details={"note": "no trial result found in job directory"},
            ))
            continue

        passed, reason = _classify_trial(trial)
        results.append(FilterResult(
            instance_id=iid,
            dataset=dataset_name,
            passed=passed,
            reason=reason,
            details={
                "status": trial.get("status"),
                "reward": trial.get("reward"),
                "script_reward": trial.get("script_reward"),
                "has_code_change": trial.get("has_code_change"),
                "diff_bytes": trial.get("diff_bytes"),
            },
        ))

    return results
