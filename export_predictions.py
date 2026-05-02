#!/usr/bin/env python3
"""
export_predictions.py — Export Harbor trial results to legacy predictions.jsonl format.

Reads Harbor job/trial directories and outputs a predictions.jsonl file compatible
with the old eval.py workflow.

Usage:
  python export_predictions.py --job-dir harbor_output/jobs/<job_name> --output predictions.jsonl
  python export_predictions.py --trials-dir harbor_output/jobs/<job_name> --output predictions.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _read_reward(trial_dir: Path) -> Optional[float]:
    """Read reward value from verifier/reward.txt."""
    reward_path = trial_dir / "verifier" / "reward.txt"
    if reward_path.exists():
        try:
            return float(reward_path.read_text().strip())
        except (ValueError, OSError):
            pass
    return None


def _read_prediction_diff(trial_dir: Path) -> str:
    """Read the prediction diff artifact."""
    # Check artifacts directory
    diff_path = trial_dir / "artifacts" / "prediction.diff"
    if diff_path.exists():
        return diff_path.read_text(encoding="utf-8", errors="replace")
    return ""


def _read_trial_result(trial_dir: Path) -> Optional[Dict[str, Any]]:
    """Read the trial result.json."""
    result_path = trial_dir / "result.json"
    if result_path.exists():
        try:
            return json.loads(result_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _read_trial_config(trial_dir: Path) -> Optional[Dict[str, Any]]:
    """Read the trial config.json."""
    config_path = trial_dir / "config.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _extract_instance_id_from_trial(trial_dir: Path) -> Optional[str]:
    """
    Extract instance_id from a trial directory.
    Looks in:
    1. Trial result.json (trial_name or task info)
    2. Trial config.json (task path)
    3. The trial directory name itself
    """
    # Try result.json
    result = _read_trial_result(trial_dir)
    if result:
        trial_name = result.get("trial_name", "")
        # trial_name format: "instance_id__<shortuuid>"
        if "__" in trial_name:
            return trial_name.rsplit("__", 1)[0]

    # Try config.json -> task -> path
    config = _read_trial_config(trial_dir)
    if config:
        task = config.get("task", {})
        task_path = task.get("path", "")
        if task_path:
            # Task path is the task directory name, which is the instance_id
            return Path(task_path).name

    # Fallback: use directory name
    name = trial_dir.name
    if "__" in name:
        return name.rsplit("__", 1)[0]

    return name


def export_from_job_dir(
    job_dir: Path,
    output_path: Path,
    model_name: str = "harbor-agent",
) -> List[Dict[str, Any]]:
    """
    Traverse Harbor job directory and export predictions in legacy format.

    Args:
        job_dir: Path to Harbor job directory (contains trial subdirectories)
        output_path: Path to write predictions.jsonl
        model_name: Model name to use in predictions (default: harbor-agent)

    Returns:
        List of prediction dicts
    """
    job_dir = Path(job_dir)
    if not job_dir.exists():
        logger.error(f"Job directory not found: {job_dir}")
        return []

    predictions: List[Dict[str, Any]] = []

    # Iterate over trial subdirectories
    for trial_dir in sorted(job_dir.iterdir()):
        if not trial_dir.is_dir():
            continue

        # Skip non-trial directories (config.json, result.json at job level)
        result_path = trial_dir / "result.json"
        if not result_path.exists():
            continue

        instance_id = _extract_instance_id_from_trial(trial_dir)
        if not instance_id:
            logger.warning(f"Could not extract instance_id from trial: {trial_dir}")
            continue

        # Read prediction diff
        model_patch = _read_prediction_diff(trial_dir)

        # Read reward
        reward = _read_reward(trial_dir)

        # Read result for more info
        result = _read_trial_result(trial_dir)

        prediction = {
            "instance_id": instance_id,
            "model_name_or_path": model_name,
            "model_patch": model_patch,
        }

        # Add optional fields
        if reward is not None:
            prediction["reward"] = reward
            prediction["resolved"] = reward > 0

        if result:
            # Extract agent info
            agent_info = result.get("agent_info", {})
            if agent_info:
                prediction["agent_name"] = agent_info.get("name", "")
                model_info = agent_info.get("model_info", {})
                if model_info and model_info.get("name"):
                    prediction["model_name_or_path"] = model_info["name"]

            # Extract timing info
            if result.get("duration_sec") is not None:
                prediction["duration_sec"] = result["duration_sec"]

        predictions.append(prediction)
        logger.debug(f"Exported: {instance_id} (reward={reward})")

    # Write output
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        for pred in predictions:
            f.write(json.dumps(pred, ensure_ascii=False) + "\n")

    logger.info(
        f"Exported {len(predictions)} predictions to {output_path}"
    )

    return predictions


def collect_eval_results(job_dir: Path) -> Dict[str, Any]:
    """
    Collect evaluation results from Harbor trials into a summary dict.

    Returns:
        Dict with:
        - total: total number of trials
        - resolved: number of resolved (reward > 0)
        - failed: number of failed (reward == 0)
        - errors: number of error trials (no reward)
        - resolve_rate: resolution rate
        - instances: dict of instance_id -> {resolved, reward, duration_sec}
    """
    job_dir = Path(job_dir)
    results: Dict[str, Any] = {
        "total": 0,
        "resolved": 0,
        "failed": 0,
        "errors": 0,
        "resolve_rate": 0.0,
        "instances": {},
    }

    for trial_dir in sorted(job_dir.iterdir()):
        if not trial_dir.is_dir():
            continue
        if not (trial_dir / "result.json").exists():
            continue

        results["total"] += 1
        instance_id = _extract_instance_id_from_trial(trial_dir)
        reward = _read_reward(trial_dir)
        trial_result = _read_trial_result(trial_dir)

        instance_info: Dict[str, Any] = {"reward": reward}

        if reward is not None:
            instance_info["resolved"] = reward > 0
            if reward > 0:
                results["resolved"] += 1
            else:
                results["failed"] += 1
        else:
            instance_info["resolved"] = False
            results["errors"] += 1

        if trial_result and trial_result.get("duration_sec") is not None:
            instance_info["duration_sec"] = trial_result["duration_sec"]

        if instance_id:
            results["instances"][instance_id] = instance_info

    if results["total"] > 0:
        results["resolve_rate"] = results["resolved"] / results["total"]

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export Harbor trial results to legacy predictions.jsonl format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--job-dir", type=Path, required=True,
        help="Path to Harbor job directory",
    )
    parser.add_argument(
        "--output", "-o", type=Path, default=Path("predictions.jsonl"),
        help="Output predictions file path (default: predictions.jsonl)",
    )
    parser.add_argument(
        "--model-name", default="harbor-agent",
        help="Model name to use in predictions (default: harbor-agent)",
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="Also print evaluation summary",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    predictions = export_from_job_dir(args.job_dir, args.output, args.model_name)

    if args.summary:
        results = collect_eval_results(args.job_dir)
        print("\nEvaluation Summary:")
        print(f"  Total:    {results['total']}")
        print(f"  Resolved: {results['resolved']}")
        print(f"  Failed:   {results['failed']}")
        print(f"  Errors:   {results['errors']}")
        print(f"  Rate:     {results['resolve_rate']:.2%}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
