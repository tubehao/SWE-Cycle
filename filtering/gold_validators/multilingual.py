"""
Multilingual 数据集的 gold patch 两步验证。

使用标准 swebench harness 流程：
- Step 1（无 gold）：空 patch → 直接调用 run_instances 验证 f2p fail, p2p pass
- Step 2（有 gold）：gold patch → 调用 run_evaluation.main 验证全部 pass

注意：Step 1 不能用 run_evaluation.main，因为 get_dataset_from_preds 会过滤掉空 patch。
"""

from __future__ import annotations

import json
import logging
import platform
import resource
import threading
from pathlib import Path
from typing import Any

import docker
from tqdm.auto import tqdm

from swebench.harness.constants import (
    FAIL_TO_PASS,
    KEY_INSTANCE_ID,
    KEY_MODEL,
    KEY_PREDICTION,
    LOG_REPORT,
    PASS_TO_PASS,
    RUN_EVALUATION_LOG_DIR,
)
from swebench.harness.docker_build import build_env_images
from swebench.harness.docker_utils import list_images, should_remove
from swebench.harness.run_evaluation import (
    main as run_evaluation_main,
    run_instance,
)
from swebench.harness.test_spec.test_spec import make_test_spec
from swebench.harness.utils import load_swebench_dataset, run_threadpool

logger = logging.getLogger(__name__)

DATASET_PATH = Path(__file__).resolve().parent.parent.parent / "dataset" / "multilingual.jsonl"
MODEL_NAME = "gold_validation"
NAMESPACE = "swebench"


def _generate_gold_predictions(dataset_path: str, output_path: Path) -> None:
    """生成 gold patch 的 predictions JSONL（用于 Step 2）。"""
    dataset = load_swebench_dataset(dataset_path, "test")
    with open(output_path, "w", encoding="utf-8") as f:
        for inst in dataset:
            pred = {
                KEY_INSTANCE_ID: inst[KEY_INSTANCE_ID],
                KEY_PREDICTION: inst["patch"],
                KEY_MODEL: MODEL_NAME,
            }
            f.write(json.dumps(pred, ensure_ascii=False) + "\n")
    logger.info(f"Generated {len(dataset)} gold predictions → {output_path}")


def _parse_reports(run_id: str) -> dict[str, dict[str, Any]]:
    """解析 swebench harness 输出的每个实例的 report.json。

    Returns:
        {instance_id: report_content}
    """
    log_base = RUN_EVALUATION_LOG_DIR / run_id / MODEL_NAME
    results = {}
    if not log_base.exists():
        logger.warning(f"Log directory not found: {log_base}")
        return results

    for instance_dir in log_base.iterdir():
        if not instance_dir.is_dir():
            continue
        report_file = instance_dir / LOG_REPORT
        if report_file.exists():
            try:
                report = json.loads(report_file.read_text(encoding="utf-8"))
                instance_id = instance_dir.name
                if instance_id in report:
                    results[instance_id] = report[instance_id]
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to parse report for {instance_dir.name}: {e}")
    return results


def _check_step1(report: dict[str, Any]) -> tuple[bool, str]:
    """检查 Step 1 结果：f2p 全部 fail，p2p 全部 pass。

    Returns:
        (passed, reason)
    """
    if not report.get("patch_successfully_applied"):
        return False, "patch_not_applied"

    tests_status = report.get("tests_status")
    if not tests_status:
        return False, "no_tests_status"

    f2p = tests_status.get(FAIL_TO_PASS, {})
    p2p = tests_status.get(PASS_TO_PASS, {})

    f2p_success = f2p.get("success", [])
    f2p_failure = f2p.get("failure", [])
    p2p_success = p2p.get("success", [])
    p2p_failure = p2p.get("failure", [])

    # f2p 必须全部 fail（在 failure 列表中）
    if f2p_success:
        return False, f"f2p_has_success: {len(f2p_success)} tests passed unexpectedly"

    if not f2p_failure:
        return False, "f2p_empty: no f2p tests found"

    # p2p 必须全部 pass（在 success 列表中）
    if p2p_failure:
        return False, f"p2p_has_failure: {len(p2p_failure)} tests failed unexpectedly"

    return True, "ok"


def _check_step2(report: dict[str, Any]) -> tuple[bool, str]:
    """检查 Step 2 结果：全部 pass（resolved=True）。

    Returns:
        (passed, reason)
    """
    if not report.get("patch_successfully_applied"):
        return False, "patch_not_applied"

    if report.get("resolved"):
        return True, "ok"

    tests_status = report.get("tests_status")
    if tests_status:
        f2p = tests_status.get(FAIL_TO_PASS, {})
        p2p = tests_status.get(PASS_TO_PASS, {})
        f2p_failure = f2p.get("failure", [])
        p2p_failure = p2p.get("failure", [])
        details = []
        if f2p_failure:
            details.append(f"f2p_failure={len(f2p_failure)}")
        if p2p_failure:
            details.append(f"p2p_failure={len(p2p_failure)}")
        return False, f"not_resolved: {', '.join(details) if details else 'unknown'}"

    return False, "not_resolved: no_tests_status"


def run_step1(
    output_dir: Path,
    max_workers: int = 4,
    timeout: int = 1800,
    instance_ids: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """运行 Step 1：无 gold patch，验证 f2p fail / p2p pass。

    直接调用 run_instance（绕过 get_dataset_from_preds 的空 patch 过滤）。
    """
    dataset_path = str(DATASET_PATH)
    run_id = "gold_val_ml_step1"
    step_dir = output_dir / "multilingual" / "step1"
    step_dir.mkdir(parents=True, exist_ok=True)

    dataset = load_swebench_dataset(dataset_path, "test", instance_ids)
    predictions = {
        inst[KEY_INSTANCE_ID]: {
            KEY_INSTANCE_ID: inst[KEY_INSTANCE_ID],
            KEY_PREDICTION: "",
            KEY_MODEL: MODEL_NAME,
        }
        for inst in dataset
    }

    logger.info(f"=== Multilingual Step 1: Running {len(dataset)} instances with empty patches ===")

    client = docker.from_env(timeout=600)
    if platform.system() == "Linux":
        resource.setrlimit(resource.RLIMIT_NOFILE, (4096, 4096))

    test_specs = [
        make_test_spec(inst, namespace=NAMESPACE)
        for inst in dataset
    ]

    build_env_images(client, dataset, False, max_workers, NAMESPACE, "latest", "latest")

    existing_images = {
        tag for i in client.images.list(all=True) for tag in i.tags
    }

    instance_log_dir = RUN_EVALUATION_LOG_DIR / run_id

    payloads = []
    for spec in test_specs:
        payloads.append((
            spec,
            predictions[spec.instance_id],
            should_remove(spec.instance_image_key, "instance", False, existing_images),
            False,
            client,
            run_id,
            timeout,
            False,
            instance_log_dir,
        ))

    stats = {"✓": 0, "✖": 0, "error": 0}
    pbar = tqdm(total=len(payloads), desc="ML Step 1", postfix=stats)
    lock = threading.Lock()

    def _run_with_progress(*args):
        result = run_instance(*args)
        with lock:
            if result["completed"]:
                stats["✓" if result.get("fail_to_pass_all_failed") else "✖"] += 1
            else:
                stats["error"] += 1
            pbar.set_postfix(stats)
            pbar.update()
        return result

    run_threadpool(_run_with_progress, payloads, max_workers)
    pbar.close()

    reports = _parse_reports(run_id)
    results = {}
    for instance_id, report in reports.items():
        passed, reason = _check_step1(report)
        results[instance_id] = {
            "passed": passed,
            "reason": reason,
            "report": report,
        }

    # 标记未产生 report 的实例为失败
    for inst in dataset:
        iid = inst[KEY_INSTANCE_ID]
        if iid not in results:
            results[iid] = {"passed": False, "reason": "no_report", "report": {}}

    report_path = output_dir / "multilingual" / "step1_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    logger.info(f"Step 1 report → {report_path}")

    passed_count = sum(1 for r in results.values() if r["passed"])
    logger.info(f"Step 1: {passed_count}/{len(results)} passed")
    return results


def run_step2(
    output_dir: Path,
    max_workers: int = 4,
    timeout: int = 1800,
    instance_ids: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """运行 Step 2：gold patch，验证全部 pass。"""
    dataset_path = str(DATASET_PATH)
    run_id = "gold_val_ml_step2"
    step_dir = output_dir / "multilingual" / "step2"
    step_dir.mkdir(parents=True, exist_ok=True)

    preds_path = step_dir / "gold_predictions.jsonl"
    _generate_gold_predictions(dataset_path, preds_path)

    logger.info("=== Multilingual Step 2: Running with gold patches ===")
    run_evaluation_main(
        dataset_name=dataset_path,
        split="test",
        instance_ids=instance_ids,
        predictions_path=str(preds_path),
        max_workers=max_workers,
        force_rebuild=False,
        cache_level="instance",
        clean=False,
        open_file_limit=4096,
        run_id=run_id,
        timeout=timeout,
        namespace="swebench",
        rewrite_reports=False,
        modal=False,
    )

    reports = _parse_reports(run_id)
    results = {}
    for instance_id, report in reports.items():
        passed, reason = _check_step2(report)
        results[instance_id] = {
            "passed": passed,
            "reason": reason,
            "report": report,
        }

    report_path = output_dir / "multilingual" / "step2_report.json"
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
    timeout: int = 1800,
    instance_ids: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """运行完整 Multilingual 验证并汇总结果。"""
    if steps is None:
        steps = ["step1", "step2"]

    step1_results = {}
    step2_results = {}

    if "step1" in steps:
        step1_results = run_step1(output_dir, max_workers, timeout, instance_ids)
    if "step2" in steps:
        step2_results = run_step2(output_dir, max_workers, timeout, instance_ids)

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

    validated_path = output_dir / "multilingual" / "validated.jsonl"
    validated_path.parent.mkdir(parents=True, exist_ok=True)
    with open(validated_path, "w", encoding="utf-8") as f:
        for iid, info in validated.items():
            f.write(json.dumps({"instance_id": iid, **info}, ensure_ascii=False) + "\n")
    logger.info(f"Validated results → {validated_path}")

    passed_count = sum(1 for v in validated.values() if v["validated"])
    logger.info(f"Multilingual: {passed_count}/{len(validated)} validated")
    return validated
