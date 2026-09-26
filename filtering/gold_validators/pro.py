"""
Pro 数据集的 gold patch 两步验证。

使用 SWE-bench_Pro-os/swe_bench_pro_eval.py 的 eval_with_docker 流程：
- Step 1（无 gold）：空 patch → 验证 f2p fail, p2p pass
- Step 2（有 gold）：gold patch → 验证全部 pass
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from tqdm import tqdm

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATASET_PATH = PROJECT_ROOT / "dataset" / "swe-bench_pro.jsonl"
PRO_DIR = PROJECT_ROOT / "SWE-bench_Pro-os"
SCRIPTS_DIR = PRO_DIR / "run_scripts"
DEFAULT_DOCKERHUB_USERNAME = "jefzda"

sys.path.insert(0, str(PRO_DIR))
from swe_bench_pro_eval import eval_with_docker, create_entryscript  # noqa: E402
sys.path.pop(0)


def _load_pro_dataset() -> pd.DataFrame:
    """加载 Pro 数据集为 DataFrame。"""
    df = pd.read_json(DATASET_PATH, lines=True)
    df = df.fillna("")
    df = df.set_index("instance_id", drop=False)
    return df


def _get_f2p_p2p(sample: pd.Series) -> tuple[set[str], set[str]]:
    """从 sample 中提取 f2p 和 p2p 测试名集合。

    数据集中 FAIL_TO_PASS/PASS_TO_PASS 可能是 list 或 JSON string。
    """
    def _parse(val) -> set[str]:
        if isinstance(val, list):
            return set(val)
        if isinstance(val, str) and val.strip():
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    return set(parsed)
            except json.JSONDecodeError:
                pass
        return set()

    f2p = _parse(sample.get("FAIL_TO_PASS", sample.get("fail_to_pass", "")))
    p2p = _parse(sample.get("PASS_TO_PASS", sample.get("pass_to_pass", "")))
    return f2p, p2p


def _check_step1_output(
    output: dict | None,
    f2p: set[str],
    p2p: set[str],
) -> tuple[bool, str]:
    """检查 Step 1 结果：f2p 全部 fail，p2p 全部 pass。

    编译失败（no_tests_in_output / f2p_not_found）视为通过：
    测试代码引用了 gold patch 新增的符号，没有 gold patch 时编译不过，
    说明 f2p 确实无法 pass，符合 Step 1 预期。
    """
    if output is None:
        return False, "no_output"

    tests = output.get("tests", [])
    if not tests:
        return True, "no_tests_in_output (compilation failure, f2p cannot pass)"

    passed_tests = {t["name"] for t in tests if t["status"] == "PASSED"}
    failed_tests = {t["name"] for t in tests if t["status"] in ("FAILED", "ERROR")}
    all_test_names = {t["name"] for t in tests}

    # f2p 必须全部 fail（不在 passed 中）
    f2p_passed = f2p & passed_tests
    if f2p_passed:
        return False, f"f2p_has_pass: {len(f2p_passed)}/{len(f2p)} f2p tests passed unexpectedly"

    # f2p 测试不在结果中（编译失败等），视为通过
    f2p_found = f2p & all_test_names
    if not f2p_found and f2p:
        return True, f"f2p_not_found: {len(f2p)} f2p tests not in output (compilation failure, f2p cannot pass)"

    # p2p 必须全部 pass
    p2p_failed = p2p & failed_tests
    if p2p_failed:
        return False, f"p2p_has_fail: {len(p2p_failed)}/{len(p2p)} p2p tests failed"

    return True, "ok"


def _check_step2_output(
    output: dict | None,
    f2p: set[str],
    p2p: set[str],
) -> tuple[bool, str]:
    """检查 Step 2 结果：f2p ∪ p2p 全部 pass。"""
    if output is None:
        return False, "no_output"

    tests = output.get("tests", [])
    if not tests:
        return False, "no_tests_in_output"

    passed_tests = {t["name"] for t in tests if t["status"] == "PASSED"}
    all_required = f2p | p2p

    if all_required <= passed_tests:
        return True, "ok"

    missing = all_required - passed_tests
    return False, f"not_all_pass: {len(missing)}/{len(all_required)} tests not passed"


def _run_single(
    instance_id: str,
    patch: str,
    sample: pd.Series,
    output_dir: Path,
    prefix: str,
    dockerhub_username: str,
    redo: bool = False,
) -> dict | None:
    """运行单个实例的 Docker 评测。"""
    try:
        output = eval_with_docker(
            patch=patch,
            sample=sample,
            output_dir=str(output_dir),
            dockerhub_username=dockerhub_username,
            scripts_dir=str(SCRIPTS_DIR),
            prefix=prefix,
            redo=redo,
            block_network=False,
        )
        return output
    except Exception as e:
        logger.error(f"Error evaluating {instance_id}: {e}")
        return None


def run_step1(
    output_dir: Path,
    max_workers: int = 4,
    dockerhub_username: str = DEFAULT_DOCKERHUB_USERNAME,
    instance_ids: list[str] | None = None,
    redo: bool = False,
) -> dict[str, dict[str, Any]]:
    """运行 Step 1：无 gold patch，验证 f2p fail / p2p pass。"""
    prev_cwd = os.getcwd()
    os.chdir(PRO_DIR)
    try:
        return _run_step1_inner(output_dir, max_workers, dockerhub_username, instance_ids, redo)
    finally:
        os.chdir(prev_cwd)


def _run_step1_inner(
    output_dir: Path,
    max_workers: int,
    dockerhub_username: str,
    instance_ids: list[str] | None,
    redo: bool,
) -> dict[str, dict[str, Any]]:
    df = _load_pro_dataset()
    step_dir = output_dir / "pro" / "step1"
    step_dir.mkdir(parents=True, exist_ok=True)

    if instance_ids:
        df = df[df["instance_id"].isin(instance_ids)]

    logger.info(f"=== Pro Step 1: Running {len(df)} instances with empty patches ===")

    results = {}
    prefix = "step1"

    def _eval_one(row):
        iid = row["instance_id"]
        output = _run_single(
            iid, "", row, str(step_dir), prefix, dockerhub_username, redo
        )
        f2p, p2p = _get_f2p_p2p(row)
        passed, reason = _check_step1_output(output, f2p, p2p)
        return iid, {"passed": passed, "reason": reason}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_eval_one, df.loc[iid]): iid
            for iid in df.index
        }
        pbar = tqdm(
            concurrent.futures.as_completed(futures),
            total=len(futures),
            desc="Pro Step 1",
        )
        for future in pbar:
            try:
                iid, result = future.result()
                results[iid] = result
                passed_count = sum(1 for r in results.values() if r["passed"])
                pbar.set_postfix(passed=passed_count, total=len(results))
            except Exception as e:
                iid = futures[future]
                results[iid] = {"passed": False, "reason": f"exception: {e}"}

    report_path = output_dir / "pro" / "step1_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    logger.info(f"Step 1 report → {report_path}")

    passed_count = sum(1 for r in results.values() if r["passed"])
    logger.info(f"Step 1: {passed_count}/{len(results)} passed")
    return results


def run_step2(
    output_dir: Path,
    max_workers: int = 4,
    dockerhub_username: str = DEFAULT_DOCKERHUB_USERNAME,
    instance_ids: list[str] | None = None,
    redo: bool = False,
) -> dict[str, dict[str, Any]]:
    """运行 Step 2：gold patch，验证全部 pass。"""
    prev_cwd = os.getcwd()
    os.chdir(PRO_DIR)
    try:
        return _run_step2_inner(output_dir, max_workers, dockerhub_username, instance_ids, redo)
    finally:
        os.chdir(prev_cwd)


def _run_step2_inner(
    output_dir: Path,
    max_workers: int,
    dockerhub_username: str,
    instance_ids: list[str] | None,
    redo: bool,
) -> dict[str, dict[str, Any]]:
    df = _load_pro_dataset()
    step_dir = output_dir / "pro" / "step2"
    step_dir.mkdir(parents=True, exist_ok=True)

    if instance_ids:
        df = df[df["instance_id"].isin(instance_ids)]

    logger.info(f"=== Pro Step 2: Running {len(df)} instances with gold patches ===")

    # 加载 gold patches
    gold_patches = {}
    with open(DATASET_PATH, "r", encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            gold_patches[d["instance_id"]] = d.get("patch", "")

    results = {}
    prefix = "step2"

    def _eval_one(row):
        iid = row["instance_id"]
        patch = gold_patches.get(iid, "")
        output = _run_single(
            iid, patch, row, str(step_dir), prefix, dockerhub_username, redo
        )
        f2p, p2p = _get_f2p_p2p(row)
        passed, reason = _check_step2_output(output, f2p, p2p)
        return iid, {"passed": passed, "reason": reason}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_eval_one, df.loc[iid]): iid
            for iid in df.index
        }
        pbar = tqdm(
            concurrent.futures.as_completed(futures),
            total=len(futures),
            desc="Pro Step 2",
        )
        for future in pbar:
            try:
                iid, result = future.result()
                results[iid] = result
                passed_count = sum(1 for r in results.values() if r["passed"])
                pbar.set_postfix(passed=passed_count, total=len(results))
            except Exception as e:
                iid = futures[future]
                results[iid] = {"passed": False, "reason": f"exception: {e}"}

    report_path = output_dir / "pro" / "step2_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    logger.info(f"Step 2 report → {report_path}")

    passed_count = sum(1 for r in results.values() if r["passed"])
    logger.info(f"Step 2: {passed_count}/{len(results)} passed")
    return results


def validate(
    output_dir: Path,
    steps: list[str] | None = None,
    max_workers: int = 4,
    dockerhub_username: str = DEFAULT_DOCKERHUB_USERNAME,
    instance_ids: list[str] | None = None,
    redo: bool = False,
) -> dict[str, dict[str, Any]]:
    """运行完整 Pro 验证并汇总结果。"""
    if steps is None:
        steps = ["step1", "step2"]

    step1_results = {}
    step2_results = {}

    if "step1" in steps:
        step1_results = run_step1(output_dir, max_workers, dockerhub_username, instance_ids, redo)
    if "step2" in steps:
        step2_results = run_step2(output_dir, max_workers, dockerhub_username, instance_ids, redo)

    all_ids = set(step1_results.keys()) | set(step2_results.keys())
    validated = {}
    for iid in sorted(all_ids):
        s1 = step1_results.get(iid, {}).get("passed", False) if step1_results else True
        s2 = step2_results.get(iid, {}).get("passed", False) if step2_results else True
        validated[iid] = {
            "step1_passed": step1_results.get(iid, {}).get("passed"),
            "step1_reason": step1_results.get(iid, {}).get("reason"),
            "step2_passed": step2_results.get(iid, {}).get("passed"),
            "step2_reason": step2_results.get(iid, {}).get("reason"),
            "validated": s1 and s2,
        }

    validated_path = output_dir / "pro" / "validated.jsonl"
    validated_path.parent.mkdir(parents=True, exist_ok=True)
    with open(validated_path, "w", encoding="utf-8") as f:
        for iid, info in validated.items():
            f.write(json.dumps({"instance_id": iid, **info}, ensure_ascii=False) + "\n")
    logger.info(f"Validated results → {validated_path}")

    passed_count = sum(1 for v in validated.values() if v["validated"])
    logger.info(f"Pro: {passed_count}/{len(validated)} validated")
    return validated
