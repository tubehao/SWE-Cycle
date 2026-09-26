"""
f1_verified_whitelist.py — Verified 白名单过滤

仅对 SWE-bench_Verified 数据集生效：
  - 加载 legacy/dataset/SWE-bench_Verified_Environment.jsonl 中的 486 个 instance_id 作为白名单
  - HuggingFace 上的 500 条中，不在白名单内的 14 条被筛掉
  - Multilingual 和 Pro 数据集不受影响，全量通过
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from filtering.config import VERIFIED_WHITELIST_PATH
from filtering.filters import FilterResult

logger = logging.getLogger(__name__)


def load_whitelist(path: Path) -> set[str]:
    """从 JSONL 文件加载 instance_id 白名单集合。"""
    ids = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                iid = obj.get("instance_id")
                if iid:
                    ids.add(iid)
            except json.JSONDecodeError:
                continue
    logger.info(f"Loaded whitelist: {len(ids)} instance_ids from {path}")
    return ids


def run(instances: list[dict], dataset_name: str) -> list[FilterResult]:
    """执行白名单过滤。

    Args:
        instances: 数据集实例列表（dict，含 instance_id）
        dataset_name: 数据集名称

    Returns:
        每个实例的 FilterResult
    """
    results = []

    # 仅 Verified 数据集需要白名单过滤
    if dataset_name != "SWE-bench_Verified":
        for inst in instances:
            results.append(FilterResult(
                instance_id=inst["instance_id"],
                dataset=dataset_name,
                passed=True,
                reason="ok",
                details={"note": "whitelist filter only applies to SWE-bench_Verified"},
            ))
        return results

    # 加载白名单
    whitelist = load_whitelist(VERIFIED_WHITELIST_PATH)

    for inst in instances:
        iid = inst["instance_id"]
        if iid in whitelist:
            results.append(FilterResult(
                instance_id=iid,
                dataset=dataset_name,
                passed=True,
                reason="ok",
            ))
        else:
            results.append(FilterResult(
                instance_id=iid,
                dataset=dataset_name,
                passed=False,
                reason="not_in_whitelist",
                details={"note": "instance_id not found in SWE-bench_Verified_Environment.jsonl"},
            ))

    return results
