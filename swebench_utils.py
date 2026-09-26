"""
SWE-bench 工具函数（新系统保留部分）

从旧版 swebench_utils.py 中提取的、仍被新系统（build_all_docker.py）使用的函数。
"""

# 直接从 swebench 文件夹导入
try:
    from swebench.harness.utils import load_swebench_dataset
    SWEBENCH_AVAILABLE = True
except ImportError:
    SWEBENCH_AVAILABLE = False


def load_swebench_dataset_wrapper(dataset_name: str, split: str = "test"):
    """
    加载 SWE-bench 数据集的包装函数

    Args:
        dataset_name: 数据集名称
        split: 数据集分割

    Returns:
        dataset: 数据集列表

    Raises:
        ImportError: 如果 SWE-bench 模块不可用
    """
    if not SWEBENCH_AVAILABLE:
        raise ImportError(
            "SWE-bench modules are not available. "
            "Please ensure the swebench folder exists in the project directory."
        )

    return load_swebench_dataset(dataset_name, split)
