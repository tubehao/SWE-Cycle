"""
filtering/config.py — 筛选管道共享配置
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FILTERING_ROOT = Path(__file__).resolve().parent
RESULTS_DIR = FILTERING_ROOT / "results"

# 白名单文件（SWE-bench_Verified 的 486 条）
VERIFIED_WHITELIST_PATH = PROJECT_ROOT / "legacy" / "dataset" / "SWE-bench_Verified_Environment.jsonl"

# Pro 本地数据集
PRO_DATASET_PATH = PROJECT_ROOT / "SWE-bench_Pro-os" / "helper_code" / "sweap_eval_full_v2.jsonl"

# ---------------------------------------------------------------------------
# 数据集定义
# ---------------------------------------------------------------------------

DATASET_CONFIGS = {
    "SWE-bench_Verified": {
        "type": "hf",
        "name": "princeton-nlp/SWE-bench_Verified",
        "split": "test",
    },
    "SWE-bench_Multilingual": {
        "type": "hf",
        "name": "SWE-bench/SWE-bench_Multilingual",
        "split": "test",
    },
    "SWE-bench_Pro": {
        "type": "jsonl",
        "path": PRO_DATASET_PATH,
    },
}

# ---------------------------------------------------------------------------
# LLM 配置（通过环境变量设置）
# ---------------------------------------------------------------------------

import os

LONGCAT_API_BASE_URL: str = os.environ.get("OPENAI_BASE_URL", "")
LONGCAT_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
LONGCAT_MODEL_NAME: str = os.environ.get("FILTER_MODEL_NAME", "deepseek-chat")

LONGCAT_OPENCODE_MODEL: str = "customproxy/deepseek-chat"

SONNET_MODEL_NAME: str = os.environ.get("SONNET_MODEL_NAME", "claude-sonnet-4-6-20250514")
SONNET_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
