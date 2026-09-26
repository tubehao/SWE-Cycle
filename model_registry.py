"""
模型注册表：维护可用模型的配置映射。

通过 --model-name 指定短名（如 gpt-5.4），系统自动查找 provider、API 地址、
context/output limit 等参数。

新增模型只需在 MODEL_REGISTRY 中添加一行即可。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelProfile:
    """单个模型的完整配置。"""

    provider: str        # OpenCode provider 名称，如 "customproxy"
    model_id: str        # 传给 API 的实际模型 ID，如 "gpt-5.4"
    context_limit: int   # 上下文窗口大小
    output_limit: int    # 最大输出 token 数
    base_url_env: str = "CCB_PROXY_BASE_URL"  # 从哪个环境变量读取 base URL
    api_key_env: str = "CCB_PROXY_API_KEY"    # 从哪个环境变量读取 API key

    @property
    def harbor_model_name(self) -> str:
        """生成 Harbor AgentConfig 需要的 model_name 格式。"""
        return f"{self.provider}/{self.model_id}"


# ---------------------------------------------------------------------------
# 模型注册表
# ---------------------------------------------------------------------------
# 新增模型：添加一行即可。
# base_url_env / api_key_env 默认走 CCB_PROXY，如果某个模型走不同的 proxy，
# 可以单独指定（如 base_url_env="MY_OTHER_PROXY_URL"）。
# ---------------------------------------------------------------------------

MODEL_REGISTRY: dict[str, ModelProfile] = {
    "gpt-5.4": ModelProfile(
        provider="customproxy",
        model_id="gpt-5.4",
        context_limit=1_050_000,
        output_limit=65_536,
    ),
    "gpt-4.1": ModelProfile(
        provider="customproxy",
        model_id="gpt-4.1",
        context_limit=128_000,
        output_limit=32_768,
    ),
    "claude-sonnet-4.6": ModelProfile(
        provider="customproxy",
        model_id="aws.claude-sonnet-4.6",
        context_limit=200_000,
        output_limit=64_000,
    ),
    "glm-5.1": ModelProfile(
        provider="customproxy",
        model_id="glm-5.1",
        context_limit=200_000,
        output_limit=16_384,
    ),
    "kimi-k2.6": ModelProfile(
        provider="customproxy",
        model_id="kimi-k2.6",
        context_limit=256_000,
        output_limit=16_384,
    ),
    "minimax-m2.7": ModelProfile(
        provider="customproxy",
        model_id="MiniMax-M2.7",
        context_limit=200_000,
        output_limit=16_384,
    ),
    "qwen3.5": ModelProfile(
        provider="customproxy",
        model_id="qwen3.5-baidu",
        context_limit=256_000,
        output_limit=16_384,
        base_url_env="CCB_PROXY_BASE_URL",
        api_key_env="CCB_QWEN_API_KEY",
    ),
    "deepseek-chat": ModelProfile(
        provider="customproxy",
        model_id="deepseek-chat",
        context_limit=128_000,
        output_limit=16_384,
        base_url_env="CCB_DEEPSEEK_BASE_URL",
        api_key_env="CCB_DEEPSEEK_API_KEY",
    ),
}


def resolve_model(name: str) -> ModelProfile:
    """根据短名查找模型配置。

    Raises:
        ValueError: 模型名不在注册表中，列出所有可用模型。
    """
    profile = MODEL_REGISTRY.get(name)
    if profile is None:
        available = ", ".join(sorted(MODEL_REGISTRY))
        raise ValueError(
            f"未知模型 '{name}'。可用模型: {available}"
        )
    return profile
