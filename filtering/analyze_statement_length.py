#!/usr/bin/env python3
"""
分析三个数据集中 problem_statement 去掉代码后的纯文字长度。

去代码逻辑：
1. 移除 Markdown fenced code blocks（```...```）
2. 移除缩进代码块（连续的 4空格/tab 开头行）
3. 移除 inline code（`...`）
4. 移除 traceback 块（Traceback (most recent call last): 到下一个非缩进行）

输出按文字长度升序排序的 CSV，供人工选定长度阈值。

Usage:
  python filtering/analyze_statement_length.py
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 项目根目录
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 数据集配置
# ---------------------------------------------------------------------------
DATASETS = {
    "SWE-bench_Verified": {
        "type": "jsonl",
        "path": PROJECT_ROOT / "legacy" / "dataset" / "SWE-bench_Verified_Development.jsonl",
        "statement_field": "problem_statement",
    },
    "SWE-bench_Multilingual": {
        "type": "hf",
        "name": "SWE-bench/SWE-bench_Multilingual",
        "split": "test",
        "statement_field": "problem_statement",
    },
    "SWE-bench_Pro": {
        "type": "jsonl",
        "path": PROJECT_ROOT / "SWE-bench_Pro-os" / "helper_code" / "sweap_eval_full_v2.jsonl",
        "statement_field": "problem_statement",
    },
}

# ---------------------------------------------------------------------------
# 去代码函数
# ---------------------------------------------------------------------------

def strip_code_from_text(text: str) -> str:
    """从 problem_statement 中去除代码，只保留自然语言文字。

    处理顺序：
    1. fenced code blocks（```...```，含可选语言标记）
    2. traceback 块
    3. 缩进代码块（4空格/tab 开头的连续行）
    4. inline code（`...`）
    5. 压缩多余空行
    """
    if not text:
        return ""

    # 1. 移除 fenced code blocks（```...```）
    # 支持 ```python, ```js 等带语言标记的形式
    result = re.sub(r'```[^\n]*\n.*?```', '', text, flags=re.DOTALL)

    # 2. 移除 traceback 块
    # Traceback 开头，后续是缩进行（空格/tab 开头），直到遇到异常行（XxxError: ...）
    result = re.sub(
        r'Traceback \(most recent call last\):.*?(?=\n[^\s]|\Z)',
        '',
        result,
        flags=re.DOTALL,
    )

    # 3. 移除缩进代码块（连续的 4空格或 tab 开头的行）
    lines = result.split('\n')
    filtered_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # 检测缩进代码块：4空格或tab开头，且非空行
        if line and (line.startswith('    ') or line.startswith('\t')):
            # 跳过连续的缩进行
            while i < len(lines) and (
                lines[i].startswith('    ')
                or lines[i].startswith('\t')
                or lines[i].strip() == ''  # 缩进块中间的空行也跳过
            ):
                i += 1
                # 如果遇到非缩进非空行就停止
                if i < len(lines) and lines[i].strip() and not lines[i].startswith('    ') and not lines[i].startswith('\t'):
                    break
        else:
            filtered_lines.append(line)
            i += 1

    result = '\n'.join(filtered_lines)

    # 4. 移除 inline code（`...`），不跨行
    result = re.sub(r'`[^`\n]+`', '', result)

    # 5. 压缩多余空行为单个空行
    result = re.sub(r'\n{3,}', '\n\n', result)

    return result.strip()


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

def load_jsonl(path: Path) -> list[dict]:
    """加载 JSONL 文件，返回 dict 列表。"""
    instances = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                instances.append(obj)
            except json.JSONDecodeError:
                continue
    return instances


def load_hf_dataset(name: str, split: str) -> list[dict]:
    """从 HuggingFace 加载数据集。"""
    from datasets import load_dataset
    ds = load_dataset(name, split=split)
    return [dict(row) for row in ds]


# ---------------------------------------------------------------------------
# 主逻辑
# ---------------------------------------------------------------------------

def analyze_all() -> list[dict]:
    """分析所有数据集，返回按文字长度升序排列的记录列表。"""
    records = []

    for dataset_name, cfg in DATASETS.items():
        print(f"Loading {dataset_name}...", file=sys.stderr)

        if cfg["type"] == "jsonl":
            instances = load_jsonl(cfg["path"])
        else:
            instances = load_hf_dataset(cfg["name"], cfg["split"])

        print(f"  Loaded {len(instances)} instances", file=sys.stderr)

        field = cfg["statement_field"]
        for inst in instances:
            instance_id = inst.get("instance_id", "unknown")
            raw_statement = inst.get(field, "") or ""
            text_only = strip_code_from_text(raw_statement)

            records.append({
                "dataset": dataset_name,
                "instance_id": instance_id,
                "repo": inst.get("repo", ""),
                "raw_char_count": len(raw_statement),
                "text_char_count": len(text_only),
                "text_word_count": len(text_only.split()),
                "text_preview": text_only[:200].replace('\n', ' '),
            })

    # 按文字字符数升序排序
    records.sort(key=lambda r: r["text_char_count"])
    return records


def main():
    records = analyze_all()

    output_path = PROJECT_ROOT / "filtering" / "results" / "statement_length_analysis.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=[
            "dataset", "instance_id", "repo",
            "raw_char_count", "text_char_count", "text_word_count",
            "text_preview",
        ])
        writer.writeheader()
        writer.writerows(records)

    print(f"\nCSV written to: {output_path}", file=sys.stderr)
    print(f"Total records: {len(records)}", file=sys.stderr)

    # 终端输出前 50 条（最短的）
    print("\n" + "=" * 120)
    print(f"{'Dataset':<25} {'Instance ID':<50} {'Raw':>6} {'Text':>6} {'Words':>6}  Preview")
    print("-" * 120)
    for r in records[:50]:
        print(
            f"{r['dataset']:<25} "
            f"{r['instance_id']:<50} "
            f"{r['raw_char_count']:>6} "
            f"{r['text_char_count']:>6} "
            f"{r['text_word_count']:>6}  "
            f"{r['text_preview'][:60]}"
        )

    # 统计摘要
    print("\n" + "=" * 80)
    print("Distribution summary (text_char_count):")
    for ds_name in DATASETS:
        ds_records = [r for r in records if r["dataset"] == ds_name]
        if not ds_records:
            continue
        lengths = [r["text_char_count"] for r in ds_records]
        lengths.sort()
        n = len(lengths)
        print(f"\n  {ds_name} ({n} instances):")
        print(f"    min={lengths[0]}, p10={lengths[n//10]}, p25={lengths[n//4]}, "
              f"median={lengths[n//2]}, p75={lengths[3*n//4]}, max={lengths[-1]}")

        # 各阈值下会被筛掉的数量
        for threshold in [50, 100, 150, 200, 300, 500]:
            count = sum(1 for l in lengths if l < threshold)
            print(f"    < {threshold} chars: {count} instances ({100*count/n:.1f}%)")


if __name__ == "__main__":
    main()
