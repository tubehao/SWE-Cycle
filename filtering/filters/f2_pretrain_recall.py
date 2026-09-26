"""
f2_pretrain_recall.py — Pre-train 数据污染检测

通过提供 instance_id + repo + version 等少量上下文，让 longcat 模型"回忆"问题内容，
然后结合文本相似度 + Claude Sonnet 判断来检测该题目是否已被 pre-train 到模型中。

判断方式（两者结合）：
  1. 文本相似度：longcat 回忆内容与原始 problem_statement 的 ROUGE-L
  2. LLM 判断：Claude Sonnet 对比回忆内容与原文，判断是否构成实质性记忆

两个信号都指向"污染"时才标记为 contaminated。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from filtering.config import (
    LONGCAT_API_BASE_URL,
    LONGCAT_API_KEY,
    LONGCAT_MODEL_NAME,
    SONNET_API_KEY,
    SONNET_MODEL_NAME,
)
from filtering.filters import FilterResult

logger = logging.getLogger(__name__)

# API 调用配置
_REQUEST_TIMEOUT = 120  # 单次请求超时（秒）
_RETRY_MAX = 3          # 最大重试次数
_RETRY_DELAY = 5        # 重试间隔（秒）
_CALL_INTERVAL = 0.5    # 两次 API 调用之间的间隔（秒）


# ---------------------------------------------------------------------------
# 文本相似度计算
# ---------------------------------------------------------------------------

def _compute_rouge_l(reference: str, hypothesis: str) -> float:
    """计算 ROUGE-L F1 分数（基于最长公共子序列）。

    不依赖外部库，纯 Python 实现。
    """
    if not reference or not hypothesis:
        return 0.0

    ref_tokens = reference.lower().split()
    hyp_tokens = hypothesis.lower().split()

    if not ref_tokens or not hyp_tokens:
        return 0.0

    # LCS 长度（DP）
    m, n = len(ref_tokens), len(hyp_tokens)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if ref_tokens[i - 1] == hyp_tokens[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    lcs_len = dp[m][n]
    precision = lcs_len / n if n > 0 else 0.0
    recall = lcs_len / m if m > 0 else 0.0

    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


# ---------------------------------------------------------------------------
# Longcat 回忆调用
# ---------------------------------------------------------------------------

def _call_longcat_recall(instance_id: str, repo: str, version: str) -> str:
    """调用 longcat 模型，让其根据 instance_id 等线索回忆问题内容。

    Args:
        instance_id: 实例 ID（如 "astropy__astropy-13033"）
        repo: 仓库名（如 "astropy/astropy"）
        version: 版本号

    Returns:
        longcat 回忆生成的文本
    """
    prompt = (
        f"I'm going to give you an identifier for a GitHub issue. "
        f"Please recall everything you know about this issue, including "
        f"the problem description, expected behavior, and any code involved.\n\n"
        f"Repository: {repo}\n"
        f"Version: {version}\n"
        f"Issue identifier: {instance_id}\n\n"
        f"Please describe the issue in detail:"
    )

    return _chat_completion(
        api_key=LONGCAT_API_KEY,
        model=LONGCAT_MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4096,
    )


# ---------------------------------------------------------------------------
# 通用 chat completion 调用
# ---------------------------------------------------------------------------

def _chat_completion(
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 4096,
) -> str:
    """调用 OpenAI 兼容的 chat/completions 接口，含重试逻辑。"""
    for attempt in range(1, _RETRY_MAX + 1):
        try:
            resp = httpx.post(
                f"{LONGCAT_API_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                },
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            logger.warning(
                f"API call failed (attempt {attempt}/{_RETRY_MAX}): {e}"
            )
            if attempt < _RETRY_MAX:
                time.sleep(_RETRY_DELAY * attempt)
            else:
                raise


# ---------------------------------------------------------------------------
# Claude Sonnet 污染判断
# ---------------------------------------------------------------------------

def _call_sonnet_judge(original: str, recalled: str) -> dict[str, Any]:
    """用 Claude Sonnet 判断 longcat 的回忆是否构成对原文的实质性记忆。

    Returns:
        {"contaminated": bool, "confidence": float, "reasoning": str}
    """
    prompt = (
        "You are evaluating whether an AI model has memorized a specific GitHub issue "
        "from its pre-training data.\n\n"
        "## Original Issue\n"
        f"{original[:3000]}\n\n"
        "## Model's Recall (given only the issue ID and repo name)\n"
        f"{recalled[:3000]}\n\n"
        "## Task\n"
        "Determine if the model's recall demonstrates substantive memorization of the "
        "original issue. Consider:\n"
        "- Does the recall contain specific details (error messages, code snippets, "
        "variable names) that match the original?\n"
        "- Could the recall be a generic guess based on the repo/issue number?\n"
        "- Are the key technical details accurately reproduced?\n\n"
        "Respond in JSON format ONLY, no other text:\n"
        '{"contaminated": true/false, "confidence": 0.0-1.0, "reasoning": "..."}'
    )

    raw = _chat_completion(
        api_key=SONNET_API_KEY,
        model=SONNET_MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024,
    )

    # 尝试从响应中提取 JSON
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Sonnet 可能在 JSON 外面包了 markdown 代码块
        import re
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        logger.warning(f"Failed to parse Sonnet judge response: {raw[:200]}")
        return {"contaminated": False, "confidence": 0.0, "reasoning": f"parse_error: {raw[:200]}"}


# ---------------------------------------------------------------------------
# 主逻辑
# ---------------------------------------------------------------------------

# ROUGE-L 阈值：超过此值 + Sonnet 判断为 contaminated 时，标记为污染
ROUGE_L_THRESHOLD = 0.3


def run(instances: list[dict], dataset_name: str) -> list[FilterResult]:
    """执行 pre-train 污染检测。

    Args:
        instances: 通过上一步筛选的实例列表
        dataset_name: 数据集名称

    Returns:
        每个实例的 FilterResult
    """
    results = []

    for inst in instances:
        iid = inst["instance_id"]
        repo = inst.get("repo", "")
        version = inst.get("version", "")
        original_statement = inst.get("problem_statement", "")

        try:
            # Step 1: 让 longcat 回忆
            recalled_text = _call_longcat_recall(iid, repo, version)

            # Step 2: 文本相似度
            rouge_l = _compute_rouge_l(original_statement, recalled_text)

            # Step 3: Sonnet 判断
            judge_result = _call_sonnet_judge(original_statement, recalled_text)

            # Step 4: 综合判断 — 两者都指向污染才标记
            is_contaminated = (
                rouge_l >= ROUGE_L_THRESHOLD
                and judge_result.get("contaminated", False)
            )

            results.append(FilterResult(
                instance_id=iid,
                dataset=dataset_name,
                passed=not is_contaminated,
                reason="contaminated" if is_contaminated else "ok",
                details={
                    "rouge_l": round(rouge_l, 4),
                    "judge_contaminated": judge_result.get("contaminated"),
                    "judge_confidence": judge_result.get("confidence"),
                    "judge_reasoning": judge_result.get("reasoning", ""),
                    "recalled_text_preview": recalled_text[:500],
                },
            ))

            # 控制 API 调用频率
            time.sleep(_CALL_INTERVAL)

        except Exception as e:
            logger.warning(f"Error processing {iid}: {e}")
            results.append(FilterResult(
                instance_id=iid,
                dataset=dataset_name,
                passed=True,
                reason="error",
                details={"error": str(e)},
            ))

    return results
