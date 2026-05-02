#!/usr/bin/env python3
"""
run_harbor.py — Unified entry point for CCB experiments via Harbor.

Replaces the old solve.py + eval.py workflow:
  1. Loads CCB dataset (JSONL)
  2. Calls ccb_adapter to generate Harbor task directories
  3. Ensures swebench Docker images are built
  4. Constructs a Harbor JobConfig
  5. Runs Harbor Job (solve + eval in one step)
  6. Optionally exports predictions.jsonl (legacy format)

Usage:
  # Basic run
  python run_harbor.py \\
    --dataset-path dataset/test_data-Development.jsonl \\
    --problem-type Development \\
    --agent claude-code

  # With pre-generated tasks
  python run_harbor.py \\
    --tasks-dir ./harbor_tasks_dev \\
    --problem-type Development \\
    --agent claude-code

  # Export predictions in legacy format
  python run_harbor.py \\
    --dataset-path dataset/test_data-Development.jsonl \\
    --problem-type Development \\
    --agent claude-code \\
    --export-predictions
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure harbor is importable — try installed package first, then local submodule
try:
    import harbor  # noqa: F401
except ImportError:
    _harbor_src = Path(__file__).parent / "harbor" / "src"
    if _harbor_src.exists():
        sys.path.insert(0, str(_harbor_src))

from ccb_adapter import CCBRecord, CCBToHarbor, load_instances_from_jsonl, load_instances_from_hf
from harbor.models.agent.name import AgentName
from model_registry import resolve_model

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# .env 文件加载
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    """读取项目根目录 .env 文件，将键值对注入 os.environ（不覆盖已有值）。"""
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


# ---------------------------------------------------------------------------
# Resume helpers
# ---------------------------------------------------------------------------

def find_latest_resumable_job(jobs_dir: Path) -> Optional[str]:
    """
    扫描 jobs_dir 下的子目录，找到最近一个可恢复的 job。

    Harbor 的 resume 判据是 job_dir/result.json 存在。
    目录名为时间戳格式（如 2026-04-16__15-56-38），按降序排列取最新的。

    Returns:
        job_name（目录名）如果找到可恢复的 job，否则 None。
    """
    if not jobs_dir.is_dir():
        return None

    # 按目录名降序排列（时间戳天然可排序）
    job_dirs = sorted(
        [d for d in jobs_dir.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )

    for job_dir in job_dirs:
        result_path = job_dir / "result.json"
        if result_path.exists():
            # 检查是否有未完成的 trial（即还有剩余任务需要运行）
            try:
                result_data = json.loads(result_path.read_text(encoding="utf-8"))
                finished_at = result_data.get("finished_at")
                # finished_at 为 None 表示 job 未正常结束（中断了）
                if finished_at is None:
                    return job_dir.name
            except (json.JSONDecodeError, OSError):
                continue
        # 没有 result.json 但有 config.json —— 说明 job 刚创建就被中断
        config_path = job_dir / "config.json"
        if config_path.exists() and not result_path.exists():
            return job_dir.name

    return None


def count_completed_trials(job_dir: Path) -> int:
    """统计 job 目录下已完成的 trial 数量（有 result.json 的子目录）。"""
    count = 0
    if not job_dir.is_dir():
        return count
    for trial_dir in job_dir.iterdir():
        if trial_dir.is_dir() and (trial_dir / "result.json").exists():
            count += 1
    return count


# ---------------------------------------------------------------------------
# Benchmark type inference
# ---------------------------------------------------------------------------

def infer_benchmark_type(dataset_name: str | None, dataset_path: Path | None) -> str:
    """根据数据集名称推导 benchmark 类型。仅 SWE-bench_Pro 走 pro 路径。"""
    if dataset_name:
        name_lower = dataset_name.lower()
        if "swe-bench_pro" in name_lower or "swebench_pro" in name_lower:
            return "swebench-pro"
        return "swebench"
    if dataset_path:
        path_lower = str(dataset_path).lower()
        if "swe-bench_pro" in path_lower or "swebench_pro" in path_lower or "sweap_eval" in path_lower:
            return "swebench-pro"
        return "swebench"
    return "swebench"


# ---------------------------------------------------------------------------
# Trial Diagnostics — classify "wrong answer" vs "API error / no work done"
# ---------------------------------------------------------------------------

# Status constants
STATUS_RESOLVED = "resolved"
STATUS_WRONG_ANSWER = "wrong_answer"
STATUS_NO_CHANGE_API_ERROR = "no_change_api_error"
STATUS_NO_CHANGE_UNKNOWN = "no_change_unknown"
STATUS_ERROR = "error"


def _detect_api_error(trial_dir: Path) -> Optional[str]:
    """
    Check multiple sources for evidence of API errors in a trial.

    Returns an error detail string if found, else None.
    Sources checked:
      1. trajectory.json — agent step messages containing "API Error"
      2. claude-code.txt — lines with subtype "api_retry" and is_error true
      3. codex.txt / opencode.txt — JSONL lines with API error keywords
    """
    # --- Check trajectory.json ---
    trajectory_path = trial_dir / "agent" / "trajectory.json"
    if trajectory_path.exists():
        try:
            traj = json.loads(trajectory_path.read_text(encoding="utf-8"))
            for step in traj.get("steps", []):
                msg = step.get("message", "")
                if isinstance(msg, str) and "API Error" in msg:
                    # Return the first API Error message found
                    return msg.strip()[:200]  # cap length
        except (json.JSONDecodeError, KeyError):
            pass

    # --- Check claude-code.txt ---
    cc_path = trial_dir / "agent" / "claude-code.txt"
    if cc_path.exists():
        try:
            for raw_line in cc_path.read_text(encoding="utf-8").splitlines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                # Look for api_retry entries that are errors
                if (
                    entry.get("subtype") == "api_retry"
                    or (entry.get("type") == "result" and entry.get("is_error") is True)
                ):
                    # Extract useful detail
                    error_msg = entry.get("error", "")
                    status = entry.get("error_status", "")
                    if error_msg or status:
                        return f"API Error: {status} {error_msg}".strip()[:200]
        except OSError:
            pass

    # --- Check codex.txt / opencode.txt (JSONL) ---
    for agent_log in ("codex.txt", "opencode.txt"):
        log_path = trial_dir / "agent" / agent_log
        if not log_path.exists():
            continue
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            for raw_line in text.splitlines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                # 尝试 JSON 解析（JSONL 格式）
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    # 非 JSON 行：直接检查文本中的 API 错误关键词
                    if "API Error" in raw_line or "rate limit" in raw_line.lower():
                        return raw_line[:200]
                    continue
                # JSON 行：检查常见错误字段
                err = entry.get("error") or entry.get("message", "")
                if isinstance(err, str) and (
                    "API Error" in err
                    or "rate limit" in err.lower()
                    or "authentication" in err.lower()
                ):
                    return f"API Error ({agent_log}): {err}".strip()[:200]
        except OSError:
            pass

    return None


def analyze_trials(job_dir: Path) -> List[Dict[str, Any]]:
    """
    Analyze every trial in *job_dir* and classify it into one of:

        resolved          — reward > 0
        wrong_answer      — reward == 0, prediction.diff non-empty
        no_change_api_error — reward == 0, diff empty, API Error detected
        no_change_unknown — reward == 0, diff empty, no clear API Error
        error             — exception_info is not null (infra failure)

    Returns a list of dicts (one per trial) and writes
    ``trial_analysis.jsonl`` into *job_dir*.
    """
    results: List[Dict[str, Any]] = []

    if not job_dir.is_dir():
        logger.warning(f"Job dir does not exist: {job_dir}")
        return results

    for trial_path in sorted(job_dir.iterdir()):
        result_file = trial_path / "result.json"
        if not result_file.exists():
            continue  # not a trial dir (e.g., config.json, result.json at job level)

        try:
            trial_result = json.loads(result_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Cannot read {result_file}: {e}")
            continue

        task_name = trial_result.get("task_name", "")
        trial_name = trial_result.get("trial_name", trial_path.name)

        # --- Reward ---
        reward = 0.0
        vr = trial_result.get("verifier_result") or {}
        rewards_map = vr.get("rewards") or {}
        if "reward" in rewards_map:
            try:
                reward = float(rewards_map["reward"])
            except (TypeError, ValueError):
                reward = 0.0

        # --- Exception info ---
        exception_info = trial_result.get("exception_info")

        # --- Diff size ---
        diff_path = trial_path / "artifacts" / "prediction.diff"
        diff_bytes = 0
        if diff_path.exists():
            diff_bytes = diff_path.stat().st_size

        has_code_change = diff_bytes > 0

        # --- Token counts ---
        agent_result = trial_result.get("agent_result") or {}
        n_input_tokens = agent_result.get("n_input_tokens", 0) or 0
        n_output_tokens = agent_result.get("n_output_tokens", 0) or 0

        # --- Classify ---
        api_error_detail: Optional[str] = None

        # AgentTimeoutError 不算 error：verifier 仍会正常评分
        is_non_timeout_error = (
            exception_info is not None
            and exception_info.get("exception_type", "") != "AgentTimeoutError"
        )

        if is_non_timeout_error:
            status = STATUS_ERROR
        elif reward > 0:
            status = STATUS_RESOLVED
        elif has_code_change:
            status = STATUS_WRONG_ANSWER
        else:
            # No code change — figure out why
            # Check token counts first (fast signal)
            if n_input_tokens == 0 and n_output_tokens == 0:
                api_error_detail = _detect_api_error(trial_path)
                if api_error_detail is None:
                    api_error_detail = "zero tokens (suspected API failure)"
                status = STATUS_NO_CHANGE_API_ERROR
            else:
                api_error_detail = _detect_api_error(trial_path)
                if api_error_detail is not None:
                    status = STATUS_NO_CHANGE_API_ERROR
                else:
                    status = STATUS_NO_CHANGE_UNKNOWN

        record = {
            "instance_id": task_name,
            "trial_name": trial_name,
            "status": status,
            "reward": reward,
            "was_timeout": trial_result.get("was_timeout", False),
            "has_code_change": has_code_change,
            "diff_bytes": diff_bytes,
            "api_error_detail": api_error_detail,
            "n_input_tokens": n_input_tokens,
            "n_output_tokens": n_output_tokens,
        }
        results.append(record)

        # Read per-task result.json if available (dual-track eval fields)
        result_json_path = trial_path / "artifacts" / "result.json"
        if result_json_path.exists():
            try:
                task_result = json.loads(result_json_path.read_text(encoding="utf-8"))
                record["script_reward"] = task_result.get("script_reward")
                record["agent_reward"] = task_result.get("agent_reward")
                record["agreement"] = task_result.get("agreement")
                record["task_type"] = task_result.get("task_type")
                # FullPipe 专项字段
                if task_result.get("task_type") == "fullpipe":
                    record["fp_raw_ratio"]             = task_result.get("raw_ratio")
                    record["fp_weighted_ratio"]        = task_result.get("weighted_ratio")
                    record["fp_completion_count"]      = task_result.get("completion_count")
                    record["fp_completion_multiplier"] = task_result.get("completion_multiplier")
                    record["fp_env_score"]             = task_result.get("env_score")
                    record["fp_code_score"]            = task_result.get("code_score")
                    record["fp_test_score"]            = task_result.get("test_score")
            except (json.JSONDecodeError, OSError):
                pass

        # Read eval_result.json if available (detailed eval agent scoring)
        eval_result_path = trial_path / "artifacts" / "eval_result.json"
        if eval_result_path.exists():
            try:
                eval_result = json.loads(eval_result_path.read_text(encoding="utf-8"))
                # 公共字段（所有题型）
                record["eval_score_ratio"] = eval_result.get("score_ratio")
                record["eval_verdict"]     = eval_result.get("verdict")
                record["eval_total_score"] = eval_result.get("total_score")
                record["eval_max_score"]   = eval_result.get("max_score")
                # 题型特定字段：单维度题型（development / testcase / environment）
                if "static_score" in eval_result:
                    record["eval_static_score"]  = eval_result.get("static_score")
                    record["eval_dynamic_score"] = eval_result.get("dynamic_score")
                # 题型特定字段：fullpipe 三维度
                for dim in ("env", "code", "test"):
                    if f"{dim}_score" in eval_result:
                        record[f"eval_{dim}_score"]         = eval_result.get(f"{dim}_score")
                        record[f"eval_{dim}_static_score"]  = eval_result.get(f"{dim}_static_score")
                        record[f"eval_{dim}_dynamic_score"] = eval_result.get(f"{dim}_dynamic_score")
            except (json.JSONDecodeError, OSError):
                pass

    # --- Write trial_analysis.jsonl ---
    analysis_path = job_dir / "trial_analysis.jsonl"
    try:
        with open(analysis_path, "w", encoding="utf-8") as f:
            for rec in results:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        logger.info(f"Trial analysis written to {analysis_path}")
    except OSError as e:
        logger.warning(f"Could not write {analysis_path}: {e}")

    # --- Write job_summary.json (enhanced: agent_reward + alignment details) ---
    _write_job_summary(job_dir, results)

    return results


def _write_job_summary(job_dir: Path, trial_analysis: List[Dict[str, Any]]) -> None:
    """
    在 job_dir 下写入 job_summary.json，包含：
    - reward_stats: script_reward / agent_reward 汇总
    - alignment_summary: agreed / disagreed 数量
    - disagreements: 每个不对齐条目的 instance_id、script_reward、agent_reward、score_diff
    """
    # --- 汇总 reward stats ---
    script_rewards = [r["script_reward"] for r in trial_analysis if r.get("script_reward") is not None]
    agent_rewards  = [r["agent_reward"]  for r in trial_analysis if r.get("agent_reward")  is not None]

    def _mean(vals: list) -> Optional[float]:
        return round(sum(vals) / len(vals), 4) if vals else None

    # --- 三态 agreement 统计 ---
    # agree / agent_higher / agent_lower / null(uncertain/failed)
    AGREE_STATES    = ("agree",)
    DISAGREE_STATES = ("agent_higher", "agent_lower")
    agreed            = [r for r in trial_analysis if r.get("agreement") in AGREE_STATES]
    disagreed         = [r for r in trial_analysis if r.get("agreement") in DISAGREE_STATES]
    disagree_higher   = [r for r in trial_analysis if r.get("agreement") == "agent_higher"]
    disagree_lower    = [r for r in trial_analysis if r.get("agreement") == "agent_lower"]
    eval_uncertain    = [r for r in trial_analysis if r.get("agent_eval_status") == "uncertain"]
    eval_failed       = [r for r in trial_analysis if r.get("agent_reward") is None
                                                   and r.get("script_reward") is not None
                                                   and r.get("agent_eval_status") != "uncertain"]

    # --- disagreement 详情 ---
    disagreement_details = []
    for r in disagreed:
        s = r.get("script_reward")
        a = r.get("agent_reward")
        diff: Optional[float] = None
        if s is not None and a is not None:
            try:
                diff = round(float(a) - float(s), 4)
            except (TypeError, ValueError):
                pass
        disagreement_details.append({
            "instance_id":    r.get("instance_id", ""),
            "trial_name":     r.get("trial_name", ""),
            "task_type":      r.get("task_type"),
            "agreement":      r.get("agreement"),
            "script_reward":  s,
            "agent_reward":   a,
            "score_diff":     diff,
            "status":         r.get("status"),
        })

    # --- eval_result.json 汇总（score_ratio + verdict 分布）---
    score_ratios = [r["eval_score_ratio"] for r in trial_analysis if r.get("eval_score_ratio") is not None]
    verdicts = [r["eval_verdict"] for r in trial_analysis if r.get("eval_verdict") is not None]
    verdict_counts: Dict[str, int] = {}
    for v in verdicts:
        verdict_counts[v] = verdict_counts.get(v, 0) + 1

    # --- FullPipe 专项汇总 ---
    fp_trials = [r for r in trial_analysis if r.get("task_type") == "fullpipe"]
    fp_weighted_ratios   = [r["fp_weighted_ratio"]   for r in fp_trials if r.get("fp_weighted_ratio")   is not None]
    fp_raw_ratios        = [r["fp_raw_ratio"]        for r in fp_trials if r.get("fp_raw_ratio")        is not None]
    fp_completion_counts = [r["fp_completion_count"] for r in fp_trials if r.get("fp_completion_count") is not None]
    fp_env_scores        = [r["fp_env_score"]        for r in fp_trials if r.get("fp_env_score")        is not None]
    fp_code_scores       = [r["fp_code_score"]       for r in fp_trials if r.get("fp_code_score")       is not None]
    fp_test_scores       = [r["fp_test_score"]       for r in fp_trials if r.get("fp_test_score")       is not None]
    fp_completion_dist: Dict[str, int] = {}
    for c in fp_completion_counts:
        fp_completion_dist[str(c)] = fp_completion_dist.get(str(c), 0) + 1

    summary = {
        "n_trials":        len(trial_analysis),
        "reward_stats": {
            "script_reward": {
                "mean":    _mean(script_rewards),
                "n_valid": len(script_rewards),
                "values":  {str(v): sum(1 for x in script_rewards if x == v)
                            for v in sorted(set(script_rewards))},
            },
            "agent_reward": {
                "mean":       _mean(agent_rewards),
                "n_valid":    len(agent_rewards),
                "n_failed":   len(eval_failed),   # eval agent 执行失败，无法评分
                "min":        round(min(agent_rewards), 4) if agent_rewards else None,
                "max":        round(max(agent_rewards), 4) if agent_rewards else None,
            },
        },
        "alignment_summary": {
            "n_agreed":           len(agreed),
            "n_disagreed":        len(disagreed),
            "n_agent_higher":     len(disagree_higher),   # agent 分高于脚本
            "n_agent_lower":      len(disagree_lower),    # agent 分低于脚本
            "n_eval_uncertain":   len(eval_uncertain),
            "n_eval_failed":      len(eval_failed),
            "agreement_rate": round(len(agreed) / (len(agreed) + len(disagreed)), 4)
                              if (agreed or disagreed) else None,
        },
        "disagreements": disagreement_details,
        "eval_result_stats": {
            "n_valid":          len(score_ratios),
            "score_ratio_mean": _mean(score_ratios),
            "verdict_counts":   verdict_counts,   # {"PASS": N, "FAIL": N, "UNCERTAIN": N}
        },
        "fullpipe_stats": {
            "n_trials":               len(fp_trials),
            "weighted_ratio_mean":    _mean(fp_weighted_ratios),
            "raw_ratio_mean":         _mean(fp_raw_ratios),
            "env_score_mean":         _mean(fp_env_scores),
            "code_score_mean":        _mean(fp_code_scores),
            "test_score_mean":        _mean(fp_test_scores),
            "completion_distribution": fp_completion_dist,  # {"3": N, "2": N, "1": N, "0": N}
        },
    }

    summary_path = job_dir / "job_summary.json"
    try:
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"Job summary written to {summary_path}")
    except OSError as e:
        logger.warning(f"Could not write {summary_path}: {e}")


# ---------------------------------------------------------------------------
# Helper: Ensure swebench Docker images exist
# ---------------------------------------------------------------------------

def ensure_swebench_images(
    instances: List[Dict[str, Any]],
    problem_type: str,
    force_rebuild: bool = False,
    max_workers: int = 4,
) -> None:
    """
    Ensure the required swebench base/env Docker images are built.

    - All types need base images.
    - Development/TestCase additionally need env images (with dependencies).
    """
    try:
        import docker
        from swebench.harness.docker_build import build_base_images, build_env_images
    except ImportError as e:
        logger.warning(f"Cannot check/build swebench images: {e}")
        return

    import config
    client = docker.from_env(timeout=config.DOCKER_CLIENT_TIMEOUT)

    # swebench requires explicit image tags (defaults to None which triggers assertion)
    IMAGE_TAG = "latest"

    logger.info(f"Ensuring swebench Docker images for {len(instances)} instances...")

    # Always build base images
    build_base_images(
        client, instances,
        force_rebuild=force_rebuild,
        instance_image_tag=IMAGE_TAG,
        env_image_tag=IMAGE_TAG,
    )

    # Development/TestCase also need env images (with installed dependencies)
    if problem_type in ("Development", "TestCase"):
        build_env_images(
            client, instances,
            force_rebuild=force_rebuild,
            max_workers=max_workers,
            instance_image_tag=IMAGE_TAG,
            env_image_tag=IMAGE_TAG,
        )

    logger.info("SWE-bench Docker images ready.")


# ---------------------------------------------------------------------------
# Helper: Parse key=value args
# ---------------------------------------------------------------------------

def parse_kv_args(kv_list: Optional[List[str]]) -> Dict[str, str]:
    """Parse a list of 'key=value' strings into a dict."""
    result = {}
    if not kv_list:
        return result
    for item in kv_list:
        if "=" in item:
            k, v = item.split("=", 1)
            result[k.strip()] = v.strip()
        else:
            logger.warning(f"Skipping malformed key=value arg: {item}")
    return result


# ---------------------------------------------------------------------------
# Helper: Print results summary
# ---------------------------------------------------------------------------

def print_results(
    job_result: Any,
    trial_analysis: Optional[List[Dict[str, Any]]] = None,
    job_dir: Optional[Path] = None,
) -> None:
    """Print a human-readable summary of the job results."""
    print("\n" + "=" * 80)
    print("Job Results Summary")
    print("=" * 80)

    stats = job_result.stats
    print(f"  Total trials: {stats.n_trials}")
    print(f"  Errors:       {stats.n_errors}")

    if stats.evals:
        for evals_key, eval_stats in stats.evals.items():
            print(f"\n  [{evals_key}]")
            print(f"    Trials: {eval_stats.n_trials}")
            print(f"    Errors: {eval_stats.n_errors}")
            if eval_stats.reward_stats:
                for reward_key, value_map in eval_stats.reward_stats.items():
                    print(f"    Reward '{reward_key}':")
                    for value, trial_names in sorted(value_map.items()):
                        print(f"      {value}: {len(trial_names)} trial(s)")
            if eval_stats.metrics:
                for metric in eval_stats.metrics:
                    name = metric.get("name", "unknown") if isinstance(metric, dict) else getattr(metric, "name", "unknown")
                    value = metric.get("value", "N/A") if isinstance(metric, dict) else getattr(metric, "value", "N/A")
                    print(f"    {name}: {value}")

    # ---- Trial Diagnostics ----
    if trial_analysis:
        counts = {
            STATUS_RESOLVED: 0,
            STATUS_WRONG_ANSWER: 0,
            STATUS_NO_CHANGE_API_ERROR: 0,
            STATUS_NO_CHANGE_UNKNOWN: 0,
            STATUS_ERROR: 0,
        }
        api_error_trials: List[Dict[str, Any]] = []

        for rec in trial_analysis:
            s = rec.get("status", STATUS_NO_CHANGE_UNKNOWN)
            counts[s] = counts.get(s, 0) + 1
            if s == STATUS_NO_CHANGE_API_ERROR:
                api_error_trials.append(rec)

        print(f"\n  Trial Diagnostics:")
        print(f"    \u2705 Resolved:                    {counts[STATUS_RESOLVED]}")
        print(f"    \u274c Wrong answer (has changes):  {counts[STATUS_WRONG_ANSWER]}")
        print(f"    \u26a0\ufe0f  No change (API error):      {counts[STATUS_NO_CHANGE_API_ERROR]}")
        print(f"    \u2753 No change (unknown):         {counts[STATUS_NO_CHANGE_UNKNOWN]}")
        print(f"    \U0001f4a5 Error (infra):               {counts[STATUS_ERROR]}")

        if api_error_trials:
            print(f"\n    API Error trials:")
            for rec in api_error_trials:
                detail = rec.get("api_error_detail", "unknown")
                print(f"      - {rec['instance_id']}: {detail}")

        # Agreement stats (three-state: agree / agent_higher / agent_lower / null)
        _AGREE    = ("agree",)
        _DISAGREE = ("agent_higher", "agent_lower")
        agreements    = [r for r in trial_analysis if r.get("agreement") in _AGREE]
        disagreements = [r for r in trial_analysis if r.get("agreement") in _DISAGREE]
        d_higher      = [r for r in trial_analysis if r.get("agreement") == "agent_higher"]
        d_lower       = [r for r in trial_analysis if r.get("agreement") == "agent_lower"]
        eval_uncertain = [r for r in trial_analysis if r.get("agent_eval_status") == "uncertain"]
        eval_failed   = [r for r in trial_analysis if r.get("agent_reward") is None
                                                    and r.get("script_reward") is not None
                                                    and r.get("agent_eval_status") != "uncertain"]
        if agreements or disagreements or eval_uncertain or eval_failed:
            total_alignable = len(agreements) + len(disagreements)
            rate_str = (f" ({round(len(agreements)/total_alignable*100)}%)"
                        if total_alignable else "")
            print(f"\n  Eval Agreement:")
            print(f"    ✅ Agreed:          {len(agreements)}{rate_str}")
            print(f"    ⬆️  Agent Higher:   {len(d_higher)}  (agent score > script)")
            print(f"    ⬇️  Agent Lower:    {len(d_lower)}  (agent score < script)")
            if eval_uncertain:
                print(f"    ❔ Uncertain:       {len(eval_uncertain)}")
            if eval_failed:
                print(f"    ❓ Eval failed:     {len(eval_failed)}")
            if disagreements:
                print(f"\n  Disagreement Details:")
                for r in disagreements:
                    iid = r.get("instance_id", r.get("trial_name", "?"))
                    agr = r.get("agreement", "?")
                    s   = r.get("script_reward")
                    a   = r.get("agent_reward")
                    print(f"    - {iid}: {agr}  script={s}  agent={a}")

        # Eval result stats (from eval_result.json — detailed scoring)
        score_ratios = [r["eval_score_ratio"] for r in trial_analysis if r.get("eval_score_ratio") is not None]
        verdicts_all = [r["eval_verdict"] for r in trial_analysis if r.get("eval_verdict") is not None]
        if score_ratios or verdicts_all:
            mean_ratio = round(sum(score_ratios) / len(score_ratios), 4) if score_ratios else None
            n_pass      = sum(1 for v in verdicts_all if v == "PASS")
            n_fail      = sum(1 for v in verdicts_all if v == "FAIL")
            n_uncertain = sum(1 for v in verdicts_all if v == "UNCERTAIN")
            print(f"\n  Eval Result (detailed scoring):")
            print(f"    Score ratio mean: {mean_ratio}  (n={len(score_ratios)})")
            print(f"    Verdict — PASS: {n_pass}  FAIL: {n_fail}  UNCERTAIN: {n_uncertain}")

        # FullPipe 专项统计
        fp_trials = [r for r in trial_analysis if r.get("task_type") == "fullpipe"]
        fp_wr = [r["fp_weighted_ratio"] for r in fp_trials if r.get("fp_weighted_ratio") is not None]
        if fp_trials:
            def _fp_mean(vals: list) -> str:
                return f"{round(sum(vals)/len(vals), 4)}" if vals else "N/A"
            fp_env   = [r["fp_env_score"]  for r in fp_trials if r.get("fp_env_score")  is not None]
            fp_code  = [r["fp_code_score"] for r in fp_trials if r.get("fp_code_score") is not None]
            fp_test  = [r["fp_test_score"] for r in fp_trials if r.get("fp_test_score") is not None]
            fp_cc    = [r["fp_completion_count"] for r in fp_trials if r.get("fp_completion_count") is not None]
            full3 = sum(1 for c in fp_cc if c == 3)
            part  = sum(1 for c in fp_cc if 0 < c < 3)
            none0 = sum(1 for c in fp_cc if c == 0)
            print(f"\n  FullPipe Stats (n={len(fp_trials)}):")
            print(f"    Weighted ratio mean: {_fp_mean(fp_wr)}  (ENV={_fp_mean(fp_env)}  CODE={_fp_mean(fp_code)}  TEST={_fp_mean(fp_test)})")
            print(f"    Completion — FULL(3/3): {full3}  PARTIAL: {part}  NONE: {none0}")

        if job_dir:
            print(f"\n  Analysis saved to: {job_dir / 'trial_analysis.jsonl'}")

    print("=" * 80)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unified CCB experiment runner via Harbor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Dataset / tasks input
    parser.add_argument(
        "--dataset-path", type=Path, default=None,
        help="Path to CCB JSONL dataset file",
    )
    parser.add_argument(
        "--tasks-dir", type=Path, default=None,
        help="Pre-generated Harbor tasks directory (skip generation)",
    )

    parser.add_argument(
        "--dataset", type=str, default=None,
        help="HuggingFace dataset name (e.g., 'princeton-nlp/SWE-bench_Verified', 'ScaleAI/SWE-bench_Pro')",
    )
    parser.add_argument(
        "--split", type=str, default="test",
        help="Dataset split to use when loading from HuggingFace (default: test)",
    )
    parser.add_argument(
        "--problem-type",
        choices=["Development", "TestCase", "Environment", "FullPipe"],
        default=None,
        help="Problem type (required when using --dataset-path or --dataset)",
    )

    # Agent configuration
    parser.add_argument(
        "--agent", default="claude-code",
        help="Harbor agent name (default: claude-code)",
    )
    parser.add_argument(
        "--model-name", default=None,
        help="Model short name from registry (e.g., gpt-5.4, claude-sonnet-4.6)",
    )
    parser.add_argument(
        "--eval-model", default="claude_sonnet_4_6",
        help="Set to 'none' to skip eval agent; actual model is configured via CCB_EVAL_MODEL env var",
    )
    parser.add_argument(
        "--ablation-eval-models", default=None,
        help="Comma-separated eval model names for ablation study (e.g., 'aws.claude-opus-4.5,aws.claude-sonnet-4.6,gpt-5.4'). Runs N eval agents in parallel per trial.",
    )
    parser.add_argument(
        "--agent-kwarg", "--ak", nargs="*", default=[],
        help="Agent kwargs as key=value pairs (e.g., max_turns=50)",
    )
    parser.add_argument(
        "--agent-env", "--ae", nargs="*", default=[],
        help="Agent environment variables as key=value pairs",
    )
    # Execution configuration
    parser.add_argument(
        "--output-dir", type=Path, default=Path("harbor_output"),
        help="Output directory for Harbor job results (default: harbor_output)",
    )
    parser.add_argument(
        "--n-concurrent", type=int, default=4,
        help="Number of concurrent trials (default: 4)",
    )
    parser.add_argument(
        "--timeout-multiplier", type=float, default=1.0,
        help="Multiply all timeouts by this factor (default: 1.0)",
    )
    parser.add_argument(
        "--force-build", action="store_true",
        help="Force rebuild Docker images",
    )
    parser.add_argument(
        "--no-delete", action="store_true",
        help="Don't delete containers after trial (useful for debugging)",
    )

    # Filtering
    parser.add_argument(
        "--instance-ids", nargs="+", default=None,
        help="Filter specific instance IDs",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum number of instances to run",
    )
    parser.add_argument(
        "--shuffle", action="store_true",
        help="Randomly shuffle instances before applying --limit (enables random sampling)",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="Random seed for --shuffle (for reproducibility)",
    )
    parser.add_argument(
        "--one-per-repo", action="store_true",
        help="Keep only one instance per repo (first after shuffle). Combine with --shuffle for random sampling.",
    )

    # Output
    parser.add_argument(
        "--export-predictions", action="store_true",
        help="Export predictions.jsonl in legacy format after run",
    )
    parser.add_argument(
        "--job-name", default=None,
        help="Custom job name (default: auto-generated timestamp)",
    )
    parser.add_argument(
        "--overwrite-tasks", action="store_true",
        help="Overwrite existing task directories during generation",
    )
    parser.add_argument(
        "--skip-image-build", action="store_true",
        help="Skip swebench Docker image building step",
    )

    # Docker image prebuild (复用)
    prebuild_group = parser.add_mutually_exclusive_group()
    prebuild_group.add_argument(
        "--prebuild", dest="prebuild", action="store_true", default=True,
        help="预构建 Docker image 并复用（默认）",
    )
    prebuild_group.add_argument(
        "--no-prebuild", dest="prebuild", action="store_false",
        help="禁用预构建，每次 trial 独立 build（旧行为）",
    )
    parser.add_argument(
        "--force-rebuild", action="store_true",
        help="强制重新构建所有 prebuilt Docker image",
    )
    parser.add_argument(
        "--prebuild-workers", type=int, default=4,
        help="预构建并行数（默认 4）",
    )

    parser.add_argument(
        "--generate-only", action="store_true",
        help="只生成 task 目录 + prebuild Docker image，不启动评测",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress trial progress displays",
    )
    parser.add_argument(
        "--n-attempts", type=int, default=1,
        help="Number of attempts per task (default: 1)",
    )

    # Blind mode (for contamination filtering)
    parser.add_argument(
        "--blind-mode", action="store_true",
        help="Blind mode: hide problem_statement from the agent instruction (for contamination detection)",
    )

    # Resume
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume from the latest interrupted job (auto-detect by job directory)",
    )

    args = parser.parse_args()

    # Validate args
    if not args.dataset_path and not args.tasks_dir and not args.dataset:
        parser.error("Must specify --dataset-path, --dataset, or --tasks-dir")

    if (args.dataset_path or args.dataset) and not args.problem_type:
        parser.error("--problem-type is required when using --dataset-path or --dataset")

    # Setup logging
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )

    # ---- Step 1: Generate Harbor task directories ----
    tasks_dir: Path
    instances: List[Dict[str, Any]] = []
    ablation_models: list[str] = []
    if args.ablation_eval_models:
        ablation_models = [m.strip() for m in args.ablation_eval_models.split(",")]
        if args.eval_model == "none":
            parser.error("--ablation-eval-models 不能与 --eval-model none 同时使用")

    if args.tasks_dir and args.tasks_dir.exists():
        tasks_dir = args.tasks_dir
        logger.info(f"Using pre-generated tasks from: {tasks_dir}")
    elif args.dataset_path or args.dataset:
        if args.dataset_path:
            if not args.dataset_path.exists():
                logger.error(f"Dataset file not found: {args.dataset_path}")
                return 1
            instances = load_instances_from_jsonl(args.dataset_path)
            logger.info(f"Loaded {len(instances)} instances from {args.dataset_path}")
        else:
            instances = load_instances_from_hf(args.dataset, args.split)
            logger.info(f"Loaded {len(instances)} instances from HuggingFace: {args.dataset} [{args.split}]")

        # Filter by instance IDs
        if args.instance_ids:
            id_set = set(args.instance_ids)
            instances = [i for i in instances if i["instance_id"] in id_set]
            logger.info(f"Filtered to {len(instances)} instances")

        # Shuffle before limit (enables random sampling)
        if args.shuffle:
            import random
            rng = random.Random(args.seed)
            rng.shuffle(instances)
            logger.info(f"Shuffled {len(instances)} instances (seed={args.seed})")

        # One-per-repo sampling: keep only the first instance seen for each repo
        # (combine with --shuffle to get a random instance per repo)
        if args.one_per_repo:
            seen_repos: set[str] = set()
            one_per: list[dict] = []
            for inst in instances:
                repo = inst.get("repo", "")
                if repo not in seen_repos:
                    seen_repos.add(repo)
                    one_per.append(inst)
            instances = one_per
            logger.info(f"One-per-repo: kept {len(instances)} instances from {len(seen_repos)} repos")

        # Apply limit
        if args.limit is not None:
            instances = instances[:args.limit]

        if not instances:
            logger.error("No instances to process")
            return 1

        # Generate Harbor tasks (segregate by problem_type to avoid cross-contamination)
        tasks_dir = args.output_dir / args.problem_type / "tasks"
        benchmark_type = infer_benchmark_type(args.dataset, args.dataset_path)

        # Ensure Docker images (swebench path only — pro path uses pre-built DockerHub images)
        if not args.skip_image_build and benchmark_type != "swebench-pro":
            ensure_swebench_images(
                instances,
                args.problem_type,
                force_rebuild=args.force_build,
            )
        adapter = CCBToHarbor(
            output_root=tasks_dir,
            problem_type=args.problem_type,
            timeout_multiplier=args.timeout_multiplier,
            eval_model=args.eval_model,
            benchmark_type=benchmark_type,
            blind_mode=args.blind_mode,
            ablation_eval_models=ablation_models,
        )
        records = [CCBRecord.from_dict(inst, args.problem_type) for inst in instances]
        successes, failures = adapter.generate_many(
            records, overwrite=args.overwrite_tasks,
        )
        logger.info(f"Generated {len(successes)} tasks, {len(failures)} failures")
        if failures:
            for iid, reason in failures:
                logger.warning(f"  Failed: {iid}: {reason}")
    else:
        logger.error("Cannot determine tasks directory")
        return 1

    # ---- Step 1.3: Prebuild Docker images (可选，默认开启) ----
    if args.prebuild:
        from prebuild_images import (
            prebuild_task_images,
            remove_image_tag_from_task_toml,
        )
        logger.info("Pre-building Docker images for task reuse...")
        pb_successes, pb_failures = prebuild_task_images(
            tasks_dir,
            force=args.force_rebuild,
            max_workers=args.prebuild_workers,
        )
        logger.info(
            f"Pre-build: {len(pb_successes)} ready, {len(pb_failures)} failed"
        )
    elif not args.prebuild:
        # --no-prebuild: 确保 task.toml 中没有 docker_image 残留
        from prebuild_images import remove_image_tag_from_task_toml
        for task_dir in tasks_dir.iterdir():
            if task_dir.is_dir():
                remove_image_tag_from_task_toml(task_dir)

    # ---- --generate-only: 生成 + prebuild 后退出 ----
    if args.generate_only:
        logger.info(f"--generate-only: task 目录已就绪: {tasks_dir}")
        return 0

    # ---- Step 1.5: Resume detection ----
    if args.resume and not args.job_name:
        jobs_dir = args.output_dir / (args.problem_type or "default") / "jobs"
        found_job_name = find_latest_resumable_job(jobs_dir)
        if found_job_name:
            args.job_name = found_job_name
            job_dir = jobs_dir / found_job_name
            n_done = count_completed_trials(job_dir)
            logger.info(f"Resume: found interrupted job '{found_job_name}' ({n_done} trials already completed)")
        else:
            logger.info("Resume: no interrupted job found, starting fresh")

    # ---- Step 2: Build Harbor JobConfig ----
    from harbor.models.job.config import (
        JobConfig,
        LocalDatasetConfig,
        OrchestratorConfig,
        RetryConfig,
    )
    from harbor.models.trial.config import (
        AgentConfig,
        ArtifactConfig,
        EnvironmentConfig,
        VerifierConfig,
    )

    agent_kwargs = parse_kv_args(args.agent_kwarg)
    agent_env = parse_kv_args(args.agent_env)
    agent_env.setdefault("OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS", "1800000")  # 30 min

    # 从模型注册表解析配置，注入 API 凭据和 limit
    model_for_harbor: str | None = None
    if args.model_name:
        profile = resolve_model(args.model_name)
        model_for_harbor = profile.harbor_model_name
        _api_key = os.environ.get(profile.api_key_env, "")
        _base_url = os.environ.get(profile.base_url_env, "")
        agent_env.setdefault("OPENAI_API_KEY", _api_key)
        agent_env.setdefault("OPENAI_BASE_URL", _base_url)
        os.environ.setdefault("OPENAI_API_KEY", _api_key)
        os.environ.setdefault("OPENAI_BASE_URL", _base_url)
        agent_kwargs["context_limit"] = str(profile.context_limit)
        agent_kwargs["output_limit"] = str(profile.output_limit)

    # Custom agent mapping: name -> import_path
    CUSTOM_AGENT_MAP = {
        "minimal-code-agent": "minimal_code_agent_harbor:MinimalCodeAgent",
    }

    agent_config_kwargs: Dict[str, Any] = {
        "model_name": model_for_harbor,
        "kwargs": agent_kwargs,
        "env": agent_env,
        # Agent setup timeout: raised from default 600s to 1800s.
        # The default 600s is too short for multilingual/pro tasks where the
        # Docker image is large and claude-code installation takes more time.
        "override_setup_timeout_sec": 1800,
    }

    if args.agent in AgentName.values():
        agent_config_kwargs["name"] = args.agent
    elif args.agent in CUSTOM_AGENT_MAP:
        agent_config_kwargs["import_path"] = CUSTOM_AGENT_MAP[args.agent]
    else:
        # Try to use directly as import_path (e.g., "my_module:MyAgent")
        agent_config_kwargs["import_path"] = args.agent

    job_config_kwargs: Dict[str, Any] = {
        "jobs_dir": args.output_dir / (args.problem_type or "default") / "jobs",
        "n_attempts": args.n_attempts,
        "timeout_multiplier": args.timeout_multiplier,
        "debug": args.debug,
        "datasets": [
            LocalDatasetConfig(path=tasks_dir.resolve()),
        ],
        "agents": [
            AgentConfig(**agent_config_kwargs),
        ],
        "environment": EnvironmentConfig(
            force_build=args.force_build,
            delete=not args.no_delete,
        ),
        "orchestrator": OrchestratorConfig(
            n_concurrent_trials=args.n_concurrent,
            quiet=args.quiet,
            retry=RetryConfig(
                max_retries=2,
                exclude_exceptions={
                    # Don't retry timeouts — they'll just time out again
                    "AgentTimeoutError",
                    "VerifierTimeoutError",
                    "EnvironmentStartTimeoutError",
                    "AgentSetupTimeoutError",
                    # Don't retry verifier parse / reward errors — deterministic
                    "RewardFileNotFoundError",
                    "RewardFileEmptyError",
                    "VerifierOutputParseError",
                },
            ),
        ),
        "artifacts": [
            ArtifactConfig(source="/logs/artifacts/prediction.diff"),
            ArtifactConfig(source="/logs/artifacts/result.json"),
            ArtifactConfig(source="/logs/artifacts/eval_result.json"),
            ArtifactConfig(source="/logs/artifacts/eval_trajectory.txt"),
            # Dual eval agent artifacts (FullPipe)
            ArtifactConfig(source="/logs/artifacts/eval_result_gold.json"),
            ArtifactConfig(source="/logs/artifacts/eval_result_blind.json"),
            ArtifactConfig(source="/logs/artifacts/eval_trajectory_gold.txt"),
            ArtifactConfig(source="/logs/artifacts/eval_trajectory_blind.txt"),
            ArtifactConfig(source="/logs/artifacts/eval_last_message_blind.txt"),
        ],
    }

    if args.job_name:
        job_config_kwargs["job_name"] = args.job_name

    # Filter tasks in Harbor's dataset config
    # --instance-ids: when using pre-generated --tasks-dir, tell Harbor which tasks to pick
    if args.tasks_dir and args.instance_ids:
        job_config_kwargs["datasets"][0].task_names = args.instance_ids

    # --limit: always cap the number of tasks Harbor will run.
    # Without this, Harbor scans ALL subdirs in tasks_dir (including leftovers
    # from previous runs), causing e.g. "1/11" progress when --limit 1.
    if args.limit is not None:
        job_config_kwargs["datasets"][0].n_tasks = args.limit

    job_config = JobConfig(**job_config_kwargs)

    # ---- Step 3: Run Harbor Job ----
    logger.info("Starting Harbor Job...")
    logger.info(f"  Agent: {args.agent}")
    logger.info(f"  Model: {args.model_name or '(default)'} ({model_for_harbor})")
    _actual_eval_model = os.environ.get("CCB_EVAL_MODEL") or os.environ.get("CCB_OPENCODE_MODEL", "aws.claude-sonnet-4.6")
    logger.info(f"  Eval model: {_actual_eval_model}")
    if ablation_models:
        logger.info(f"  Ablation eval models: {ablation_models}")
    logger.info(f"  Concurrent trials: {args.n_concurrent}")
    logger.info(f"  Prebuild: {'enabled' if args.prebuild else 'disabled'}")
    logger.info(f"  Tasks dir: {tasks_dir}")
    logger.info(f"  Output dir: {args.output_dir}")

    from harbor.job import Job

    try:
        job = Job(job_config)
    except FileExistsError as e:
        logger.error(f"Resume failed: job config mismatch — {e}")
        logger.error(
            "The previous job was started with different parameters. "
            "To resume, ensure all parameters (--limit, --model, --n-concurrent, etc.) "
            "match the original run. Alternatively, use a new --job-name to start fresh."
        )
        return 1

    if job.is_resuming:
        n_total = len(job)
        n_existing = n_total - len(job._remaining_trial_configs)
        logger.info(f"  Resuming: {n_existing}/{n_total} trials already completed, {len(job._remaining_trial_configs)} remaining")

    job_result = asyncio.run(job.run())

    # ---- Step 4: Export predictions (optional) ----
    if args.export_predictions:
        try:
            from export_predictions import export_from_job_dir
            predictions_path = args.output_dir / "predictions.jsonl"
            export_from_job_dir(job.job_dir, predictions_path)
            logger.info(f"Exported predictions to {predictions_path}")
        except Exception as e:
            logger.error(f"Failed to export predictions: {e}")

    # ---- Step 5: Analyze trials & print summary ----
    trial_analysis = analyze_trials(job.job_dir)
    print_results(job_result, trial_analysis=trial_analysis, job_dir=job.job_dir)

    # ---- Step 6: --no-delete container debug instructions ----
    if args.no_delete:
        print("\n" + "-" * 80)
        print("  Containers were NOT deleted (--no-delete).")
        print("  To inspect a trial container:")
        print()
        print("    # List surviving containers:")
        print(f"    docker ps -a --filter \"name={args.agent}\"")
        print()
        print("    # Enter a container:")
        print("    docker exec -it <container_id> /bin/bash")
        print("-" * 80)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
