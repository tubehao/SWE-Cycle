"""
filtering/filters/ — 筛选器模块

每个筛选器接收实例列表，返回 FilterResult 列表。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class FilterResult:
    """单个实例的筛选结果。"""

    instance_id: str
    dataset: str          # "SWE-bench_Verified" / "SWE-bench_Multilingual" / "SWE-bench_Pro"
    passed: bool          # True=保留, False=筛掉
    reason: str           # 筛掉原因（passed=True 时为 "ok"）
    details: dict = field(default_factory=dict)  # 额外信息


def save_results(results: list[FilterResult], path: Path) -> None:
    """将筛选结果写入 JSONL 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
    logger.info(f"Saved {len(results)} results to {path}")


def load_results(path: Path) -> list[FilterResult]:
    """从 JSONL 文件加载筛选结果。"""
    results = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            results.append(FilterResult(**d))
    return results


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """加载 JSONL 文件，返回 dict 列表。"""
    instances = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                instances.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return instances


def load_hf_dataset(name: str, split: str) -> list[dict[str, Any]]:
    """从 HuggingFace 加载数据集。"""
    from datasets import load_dataset

    ds = load_dataset(name, split=split)
    instances = [dict(row) for row in ds]
    logger.info(f"Loaded {len(instances)} instances from HuggingFace: {name} [{split}]")
    return instances


def summarize_results(results: list[FilterResult], filter_name: str) -> dict:
    """生成单个 filter 的统计摘要。"""
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    filtered = total - passed

    # 按 reason 分组
    reason_counts: dict[str, int] = {}
    for r in results:
        if not r.passed:
            reason_counts[r.reason] = reason_counts.get(r.reason, 0) + 1

    # 按 dataset 分组
    by_dataset: dict[str, dict] = {}
    for r in results:
        ds = r.dataset
        if ds not in by_dataset:
            by_dataset[ds] = {"total": 0, "passed": 0, "filtered": 0}
        by_dataset[ds]["total"] += 1
        if r.passed:
            by_dataset[ds]["passed"] += 1
        else:
            by_dataset[ds]["filtered"] += 1

    return {
        "filter": filter_name,
        "total": total,
        "passed": passed,
        "filtered": filtered,
        "reason_counts": reason_counts,
        "by_dataset": by_dataset,
    }
