#!/usr/bin/env python3
"""
CCB Adapter: SWE-Cycle → Harbor Task Directory Converter

Converts swe-cycle JSONL instances into Harbor-compatible task directories.
Each task directory contains:
  instruction.md   - solve prompt (varies by problem_type)
  task.toml        - timeout and resource config
  environment/
    Dockerfile     - build instructions (FROM swebench image + patch application)
    *.diff         - patch files (if needed)
  tests/
    test.sh        - verification script (collect diff + run eval + write reward)
    config.json    - original SWE-bench instance data
  solution/
    solve.sh       - ground-truth patch (optional)

Usage:
  python ccb_adapter.py \\
    --dataset-path dataset/test_data-Development.jsonl \\
    --problem-type Development \\
    --output-dir ./harbor_tasks_dev

  python ccb_adapter.py \\
    --dataset-path dataset/test_data-Development.jsonl \\
    --problem-type Development \\
    --output-dir ./harbor_tasks_dev \\
    --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict, List, Optional, Tuple

from jinja2 import Environment, FileSystemLoader

from swebench.harness.constants import MAP_REPO_TO_EXT

logger = logging.getLogger(__name__)

# SWE-bench Pro 数据集中 language 字段为 None 的仓库的语言映射。
# 这些仓库在 sweap_eval_full_v2.jsonl 中 language=None，且 MAP_REPO_TO_EXT 中也没有，
# 若不补充则会 fallback 为 "py"，导致 Dockerfile 走错语言分支（尤其是环境剥离逻辑）。
PRO_REPO_LANGUAGE_MAP: dict[str, str] = {
    "ansible/ansible":              "py",
    "internetarchive/openlibrary":  "py",
    "qutebrowser/qutebrowser":      "py",
    "flipt-io/flipt":               "go",
    "gravitational/teleport":       "go",
    "future-architect/vuls":        "go",
    "navidrome/navidrome":          "go",
    "protonmail/webclients":        "ts",
    "element-hq/element-web":       "ts",
    "tutao/tutanota":               "ts",
    "NodeBB/NodeBB":                "js",
}

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _normalize_test_list(val: Any) -> List[str]:
    """统一处理三种 F2P/P2P 格式，返回 list[str]，空/None 返回 []。

    支持：
      - None / "" → []
      - list       → list[str]（已是列表，直接转 str）
      - JSON 字符串 → json.loads → list[str]
      - Python list 字符串（单引号）→ ast.literal_eval → list[str]
    """
    import ast as _ast
    if not val:
        return []
    if isinstance(val, list):
        return [str(x) for x in val]
    try:
        result = json.loads(val)
        if isinstance(result, list):
            return [str(x) for x in result]
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        result = _ast.literal_eval(val)
        if isinstance(result, list):
            return [str(x) for x in result]
    except (ValueError, SyntaxError):
        pass
    return [str(val)]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CCBRecord:
    """Encapsulates a single CCB instance's data."""
    instance_id: str
    repo: str
    version: str
    base_commit: str
    problem_statement: str
    problem_json: str          # JSON string of the problem description
    patch: Optional[str]       # code patch (gold solution)
    test_patch: Optional[str]  # test patch
    problem_type: str          # Development / TestCase / Environment / FullPipe
    difficulty: str = "hard"
    resolved_commit: Optional[str] = None
    hints_text: Optional[str] = None
    FAIL_TO_PASS: Optional[str] = None
    PASS_TO_PASS: Optional[str] = None
    language: str = "py"
    # Pro-specific fields (None for swebench instances)
    run_script: Optional[str] = None          # run_script.sh content or path
    parsing_script: Optional[str] = None      # parser.py content or path
    before_repo_set_cmd: Optional[str] = None
    selected_test_files_to_run: Optional[str] = None
    behavior_spec: Optional[str] = None  # FullPipe-specific: behavior specification
    # Raw instance dict for config.json
    raw_instance: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any], problem_type: str) -> "CCBRecord":
        """Create a CCBRecord from a JSONL dict row.

        Tolerates missing 'version' (Pro datasets may lack it) and missing
        'problem_json'.  When problem_statement is absent, attempts to
        reconstruct it from problem_json as a fallback.
        """
        # Fallback: extract problem_statement from problem_json if missing
        problem_statement = d.get("problem_statement", "")
        if not problem_statement and d.get("problem_json"):
            try:
                pj = json.loads(d["problem_json"]) if isinstance(d["problem_json"], str) else d["problem_json"]
                parts = []
                if pj.get("background"):
                    parts.append(pj["background"])
                if pj.get("task"):
                    parts.append(pj["task"])
                problem_statement = "\n\n".join(parts)
            except (json.JSONDecodeError, TypeError):
                pass

        return cls(
            instance_id=d["instance_id"],
            repo=d["repo"],
            version=d.get("version", ""),
            base_commit=d["base_commit"],
            problem_statement=problem_statement,
            problem_json=d.get("problem_json", "{}"),
            patch=d.get("patch"),
            test_patch=d.get("test_patch"),
            problem_type=problem_type,
            difficulty=d.get("difficulty", "hard"),
            resolved_commit=d.get("resolved_commit"),
            hints_text=d.get("hints_text"),
            FAIL_TO_PASS=d.get("FAIL_TO_PASS") or d.get("fail_to_pass"),
            PASS_TO_PASS=d.get("PASS_TO_PASS") or d.get("pass_to_pass"),
            language=(d.get("language")
                      or PRO_REPO_LANGUAGE_MAP.get(d.get("repo", ""), None)
                      or MAP_REPO_TO_EXT.get(d.get("repo", ""), "py")),
            run_script=d.get("run_script"),
            parsing_script=d.get("parsing_script"),
            before_repo_set_cmd=d.get("before_repo_set_cmd"),
            selected_test_files_to_run=d.get("selected_test_files_to_run"),
            behavior_spec=d.get("behavior_spec"),
            raw_instance=d,
        )


class HarborTaskPaths:
    """Convenience paths for writing a Harbor task directory."""

    def __init__(self, task_dir: Path) -> None:
        self.task_dir = Path(task_dir)
        self.environment_dir = self.task_dir / "environment"
        self.tests_dir = self.task_dir / "tests"
        self.solution_dir = self.task_dir / "solution"

        self.instruction_path = self.task_dir / "instruction.md"
        self.config_path = self.task_dir / "task.toml"

        # Create directories
        self.environment_dir.mkdir(parents=True, exist_ok=True)
        self.tests_dir.mkdir(parents=True, exist_ok=True)
        self.solution_dir.mkdir(parents=True, exist_ok=True)

        self.test_sh_path = self.tests_dir / "test.sh"
        self.config_json_path = self.tests_dir / "config.json"
        self.dockerfile_path = self.environment_dir / "Dockerfile"
        self.solve_sh_path = self.solution_dir / "solve.sh"


# ---------------------------------------------------------------------------
# Test command generation (adapted from Harbor's swebench adapter utils.py)
# ---------------------------------------------------------------------------

def get_test_cmd_and_files(
    test_patch: str, repo: str, version: str,
) -> Tuple[str, List[str]]:
    """
    Extract the raw test_cmd and test_files for a SWE-bench instance.

    Returns:
        (test_cmd, test_files) — e.g. ("python -m pytest -xvs", ["tests/test_foo.py"])
    Used by Environment type to run tests directly in the agent's testbed
    without the full swebench pipeline (reset/apply test_patch).
    """
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
    from swebench.harness.test_spec.python import get_test_directives

    raw_test_cmd = MAP_REPO_VERSION_TO_SPECS[repo][version]["test_cmd"]
    # test_cmd may be a list (multilingual repos), standardize to str
    if isinstance(raw_test_cmd, list):
        test_cmd = " && ".join(raw_test_cmd)
    else:
        test_cmd = raw_test_cmd
    test_files = get_test_directives({"repo": repo, "test_patch": test_patch})
    return test_cmd, test_files


def get_test_commands(
    test_patch: str, repo: str, version: str, base_commit: str,
    instance: Dict[str, Any],
    skip_install: bool = False,
) -> str:
    """
    Generate the test commands script for swebench-style evaluation.
    Uses swebench library's make_test_spec().eval_script_list which
    correctly handles all languages (Python with conda, Go/Java/JS/etc without).

    Args:
        instance: Full SWE-bench instance dict (needed by make_test_spec).
        skip_install: If True, skip repo-specific install commands.
            Used for Environment type where the agent already configured
            the environment — re-installing would overwrite agent's setup.
    """
    test_spec = get_swebench_test_spec(instance)
    eval_script_list = list(test_spec.eval_script_list)

    # skip_install: Environment type — agent already configured environment,
    # don't re-install which would overwrite agent's setup.
    if skip_install:
        from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
        install_cmd = MAP_REPO_VERSION_TO_SPECS[repo][version].get("install", "")
        if install_cmd and install_cmd in eval_script_list:
            eval_script_list.remove(install_cmd)

    # Build shell script from eval_script_list
    repo_directory = "/testbed"
    lines = [
        "#!/bin/bash",
        "set -uo pipefail -x",
        f"cd {repo_directory}",
        "",
        "LOG_FILE=$(mktemp)",
        "export LOG_FILE",
        "exec 3>&1 4>&2",
        'exec > >(tee "$LOG_FILE") 2>&1',
        "",
    ]
    lines.extend(eval_script_list)
    lines.extend([
        "",
        "exec 1>&3 2>&4",
        "exec 1>&3 2>&4",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Docker image resolution
# ---------------------------------------------------------------------------

def get_swebench_test_spec(instance: Dict[str, Any]):
    """
    Get the full TestSpec for a SWE-bench instance.
    Returns the TestSpec object which contains image keys, scripts, etc.
    """
    from swebench.harness.test_spec.test_spec import make_test_spec
    return make_test_spec(
        instance,
        instance_image_tag="latest",
        env_image_tag="latest",
    )


def get_swebench_image_names(instance: Dict[str, Any]) -> Tuple[str, str]:
    """
    Get (base_image_key, env_image_key) for a SWE-bench instance.
    Uses swebench's make_test_spec.
    """
    test_spec = get_swebench_test_spec(instance)
    return test_spec.base_image_key, test_spec.env_image_key


def get_install_repo_script(instance: Dict[str, Any]) -> str:
    """
    Get the install_repo_script (setup_repo.sh) for a SWE-bench instance.
    This script clones the repo, checks out the correct commit, and installs deps.
    It is used in the instance image layer (env -> instance).
    """
    test_spec = get_swebench_test_spec(instance)
    return test_spec.install_repo_script


def get_setup_env_script(instance: Dict[str, Any]) -> str:
    """
    Get the setup_env_script for a SWE-bench instance.
    This script creates the conda environment and installs pip dependencies.
    It is used in the env image layer (base -> env).
    Needed for Environment/FullPipe which start FROM base_image.
    """
    test_spec = get_swebench_test_spec(instance)
    return test_spec.setup_env_script


def get_repo_only_setup_script(instance: Dict[str, Any]) -> str:
    """
    Generate a setup_repo.sh that ONLY does git clone + checkout + cleanup,
    without conda activate or package installation.

    This is needed for Environment and FullPipe problem types which start
    FROM base_image (no testbed conda env). The full install_repo_script
    tries to `conda activate testbed` which fails on base_image.

    For Environment: agent is supposed to configure the env.
    For FullPipe: agent is supposed to do everything.

    Uses the same truncation logic as solve.py / harbor_agent_builder.py:
    agent_docker_utils.truncate_repo_script_for_environment().
    """
    from agent_docker_utils import truncate_repo_script_for_environment

    test_spec = get_swebench_test_spec(instance)
    language = getattr(test_spec, 'language', instance.get('language', 'py'))

    # Truncate: remove conda activate / install steps, keep only git clone + checkout
    truncated_list = truncate_repo_script_for_environment(
        test_spec.repo_script_list, language
    )
    return "\n".join(["#!/bin/bash", "set -euxo pipefail"] + truncated_list) + "\n"


def get_pro_image_name(
    instance: Dict[str, Any],
    dockerhub_username: str = "jefzda",
) -> str:
    """
    Get the DockerHub image URI for a SWE-bench Pro instance.
    Uses the helper from SWE-bench_Pro-os.
    """
    pro_dir = Path(__file__).parent / "SWE-bench_Pro-os"
    import sys
    sys.path.insert(0, str(pro_dir))
    try:
        from helper_code.image_uri import get_dockerhub_image_uri
        return get_dockerhub_image_uri(
            instance["instance_id"],
            dockerhub_username,
            instance.get("repo", ""),
        )
    finally:
        sys.path.pop(0)


def get_pro_scripts_dir() -> Path:
    """Get the default Pro run_scripts directory path."""
    return Path(__file__).parent / "SWE-bench_Pro-os" / "run_scripts"


def load_pro_scripts(
    instance_id: str,
    scripts_dir: Optional[Path] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Load run_script.sh and parser.py for a Pro instance.

    Returns:
        (run_script_content, parser_content) — None if file not found.
    """
    if scripts_dir is None:
        scripts_dir = get_pro_scripts_dir()
    scripts_dir = Path(scripts_dir)
    instance_dir = scripts_dir / instance_id

    run_script_content = None
    parser_content = None

    run_script_path = instance_dir / "run_script.sh"
    if run_script_path.exists():
        run_script_content = run_script_path.read_text(encoding="utf-8")

    parser_path = instance_dir / "parser.py"
    if parser_path.exists():
        parser_content = parser_path.read_text(encoding="utf-8")

    return run_script_content, parser_content


def get_instance_image_name(instance: Dict[str, Any]) -> str:
    """Get the full instance image name (env image with repo setup)."""
    from swebench.harness.test_spec.test_spec import make_test_spec
    spec = make_test_spec(
        instance,
        namespace="swebench",
        instance_image_tag="latest",
        env_image_tag="latest",
    )
    return spec.instance_image_key.replace("arm64", "x86_64")


# ---------------------------------------------------------------------------
# Instruction generation helpers
# ---------------------------------------------------------------------------


def _get_language_context(language: str) -> Dict[str, str]:
    """Get language-specific context for templates."""
    import config as ccb_config

    return {
        "language": language,
        "language_display": ccb_config.LANGUAGE_DISPLAY_NAME.get(language, language),
        "available_tools": ccb_config.LANGUAGE_AVAILABLE_TOOLS.get(language, "N/A"),
        "language_specific_instructions": ccb_config.LANGUAGE_ENV_INSTRUCTIONS.get(
            language, "Install all dependencies based on the project configuration files."
        ),
    }


# ---------------------------------------------------------------------------
# opencode 二进制安装辅助
# ---------------------------------------------------------------------------

# 预下载的 opencode 二进制路径（版本固定，避免 npm 安装不稳定）
# glibc 版用于 Debian/Ubuntu 容器，musl 版用于 Alpine 容器
_OPENCODE_BINARY = Path(__file__).parent / "vendor" / "opencode" / "opencode"
_OPENCODE_MUSL_BINARY = Path(__file__).parent / "vendor" / "opencode" / "opencode-musl"


def _link_opencode_binary(environment_dir: Path) -> None:
    """将预下载的 opencode 二进制（glibc + musl）硬链接到 Docker build context。

    硬链接不占额外磁盘空间。若源和目标不在同一文件系统则回退到复制。
    两个版本都会被放入 build context，Dockerfile 中根据容器 libc 类型自动选择。
    """
    for src, name in [(_OPENCODE_BINARY, "opencode"), (_OPENCODE_MUSL_BINARY, "opencode-musl")]:
        dst = environment_dir / name
        if dst.exists():
            continue
        if not src.exists():
            raise FileNotFoundError(
                f"预下载的 opencode 二进制不存在: {src}\n"
                f"请先下载 glibc 和 musl 两个版本到 vendor/opencode/\n"
                f"glibc: opencode-linux-x64.tar.gz\n"
                f"musl:  opencode-linux-x64-baseline-musl.tar.gz"
            )
        try:
            os.link(src, dst)
        except OSError:
            # 跨文件系统时硬链接失败，回退到复制
            shutil.copy2(src, dst)


# ---------------------------------------------------------------------------
# swebench grading 零依赖脚本
# ---------------------------------------------------------------------------

_SWEBENCH_GRADING_SCRIPT = Path(__file__).parent / "ccb_templates" / "_shared" / "swebench_grading.py"


def _copy_swebench_grading(template_dir: Path, environment_dir: Path) -> None:
    """将零依赖 swebench_grading.py 复制到 Docker build context。

    仅 Development/Environment 题型的 swebench 路径需要此脚本（替代原 uv run parser.py）。
    """
    src = template_dir / "_shared" / "swebench_grading.py"
    dst = environment_dir / "swebench_grading.py"
    if dst.exists():
        return
    if not src.exists():
        raise FileNotFoundError(
            f"零依赖 swebench grading 脚本不存在: {src}\n"
            f"请确认 ccb_templates/_shared/swebench_grading.py 已创建。"
        )
    shutil.copy2(src, dst)


# ---------------------------------------------------------------------------
# Main converter class
# ---------------------------------------------------------------------------

class CCBToHarbor:
    """SWE-Cycle dataset -> Harbor task directories converter."""

    # Default timeouts (seconds)
    DEFAULT_AGENT_TIMEOUT = 5400           # 1.5h (Dev/TC/Env solving agent)
    DEFAULT_AGENT_TIMEOUT_FULLPIPE = 10800 # 3h   (FP solving agent — 综合题型需更多时间)
    DEFAULT_BUILD_TIMEOUT = 1800           # 30 min

    # script_eval + eval_agent 超时（verifier_timeout 由它们推导）
    SCRIPT_EVAL_TIMEOUT = 1800             # 30 min（script_eval 不应阻塞 eval_agent）
    EVAL_AGENT_TIMEOUT = 5400              # 1.5h (Dev/TC/Env eval agent)
    EVAL_AGENT_TIMEOUT_FULLPIPE = 10800    # 3h   (FP eval agent — 三维打分更复杂)
    VERIFIER_MARGIN = 300                  # 5 min margin（Phase 0 copy 等开销）

    DEFAULT_VERIFIER_TIMEOUT = SCRIPT_EVAL_TIMEOUT + EVAL_AGENT_TIMEOUT + VERIFIER_MARGIN  # 7500s
    DEFAULT_VERIFIER_TIMEOUT_FULLPIPE = EVAL_AGENT_TIMEOUT_FULLPIPE + VERIFIER_MARGIN      # 11100s

    def __init__(
        self,
        output_root: Path,
        problem_type: str,
        template_dir: Optional[Path] = None,
        agent_timeout: float = DEFAULT_AGENT_TIMEOUT,
        verifier_timeout: float = DEFAULT_VERIFIER_TIMEOUT,
        build_timeout: float = DEFAULT_BUILD_TIMEOUT,
        timeout_multiplier: float = 1.0,
        eval_model: str = "claude_sonnet_4_6",
        benchmark_type: str = "swebench",
        blind_mode: bool = False,
        ablation_eval_models: list[str] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.problem_type = problem_type
        self.eval_model = eval_model
        self.benchmark_type = benchmark_type
        self.blind_mode = blind_mode
        self.ablation_eval_models = ablation_eval_models or []

        self.opencode_api_key = (
            os.environ.get("CCB_PROXY_API_KEY")
            or os.environ.get("CCB_OPENCODE_API_KEY")
            or ""
        )
        if not self.opencode_api_key:
            logger.warning(
                "CCB_PROXY_API_KEY 未设置，请在 .env 中配置。"
            )

        # 从环境变量读取 eval agent 模型名（优先读 .env 新名，兼容旧名）
        _default_model = "aws.claude-sonnet-4.6"
        self.opencode_model = (
            os.environ.get("CCB_EVAL_MODEL")
            or os.environ.get("CCB_OPENCODE_MODEL")
            or _default_model
        )
        if self.opencode_model == _default_model:
            logger.warning(
                "CCB_EVAL_MODEL 未设置，使用内置默认模型 %s；"
                "服务器部署时请在 .env 中设置 CCB_EVAL_MODEL。",
                _default_model,
            )

        # FullPipe 使用更长的 solving agent 和 verifier 超时
        is_fullpipe = (problem_type == "FullPipe")
        if agent_timeout == self.DEFAULT_AGENT_TIMEOUT and is_fullpipe:
            agent_timeout = self.DEFAULT_AGENT_TIMEOUT_FULLPIPE
        self.agent_timeout = agent_timeout * timeout_multiplier

        if verifier_timeout == self.DEFAULT_VERIFIER_TIMEOUT and is_fullpipe:
            verifier_timeout = self.DEFAULT_VERIFIER_TIMEOUT_FULLPIPE
        self.verifier_timeout = verifier_timeout * timeout_multiplier
        self.build_timeout = build_timeout * timeout_multiplier

        # Setup Jinja2 environment
        if template_dir is None:
            template_dir = Path(__file__).parent / "ccb_templates"
        self.template_dir = template_dir

        self.jinja_env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            keep_trailing_newline=True,
        )

    def _get_template_subdir(self) -> str:
        """Map problem_type to template subdirectory."""
        mapping = {
            "Development": "development",
            "TestCase": "testcase",
            "Environment": "environment",
            "FullPipe": "fullpipe",
        }
        return mapping.get(self.problem_type, "development")

    def _get_docker_image(self, record: CCBRecord) -> Tuple[str, str]:
        """
        Get the appropriate Docker image for the problem type.
        Returns (base_image, env_image).
        For Pro instances, returns (pro_image, pro_image) — both point to the
        pre-built DockerHub image.
        """
        if self.benchmark_type == "swebench-pro":
            pro_image = get_pro_image_name(record.raw_instance)
            return pro_image, pro_image
        base_image, env_image = get_swebench_image_names(record.raw_instance)
        return base_image, env_image

    def _generate_test_commands(
        self, record: CCBRecord, skip_install: bool = False,
    ) -> str:
        """Generate swebench test commands for eval."""
        if not record.test_patch:
            return "echo 'No test_patch available, skipping swebench eval'"
        return get_test_commands(
            test_patch=record.test_patch,
            repo=record.repo,
            version=record.version,
            base_commit=record.base_commit,
            instance=record.raw_instance,
            skip_install=skip_install,
        )

    def generate_task(self, record: CCBRecord, overwrite: bool = False) -> Path:
        """
        Generate a single Harbor task directory for a CCB instance.

        Args:
            record: CCBRecord instance
            overwrite: If True, overwrite existing task directory

        Returns:
            Path to the created task directory
        """
        task_dir = self.output_root / record.instance_id
        if task_dir.exists():
            if not overwrite:
                logger.info(f"Task dir already exists, skipping: {task_dir}")
                return task_dir
            shutil.rmtree(task_dir)

        paths = HarborTaskPaths(task_dir)
        subdir = self._get_template_subdir()
        benchmark = self.benchmark_type
        base_image, env_image = self._get_docker_image(record)

        # ---- 0. Pro: load and write run_script.sh + parser.py into tests/ ----
        if benchmark == "swebench-pro":
            run_script_content, parser_content = load_pro_scripts(
                record.instance_id
            )
            if run_script_content:
                # Add --maxWorkers=1 --forceExit to jest commands to prevent
                # hanging in Docker's limited-resource environment
                # (matches Harbor official swebenchpro adapter behavior)
                run_script_content = run_script_content.replace(
                    "npx jest --verbose --silent",
                    "npx jest --verbose --silent --maxWorkers=1 --forceExit",
                )
                rs_path = paths.tests_dir / "run_script.sh"
                rs_path.write_text(run_script_content, encoding="utf-8")
                rs_path.chmod(0o755)
            else:
                raise FileNotFoundError(
                    f"Pro instance {record.instance_id}: run_script.sh not found "
                    f"in {get_pro_scripts_dir() / record.instance_id}. "
                    f"Pro evaluation requires per-instance run_script.sh."
                )
            if parser_content:
                pp_path = paths.tests_dir / "parser.py"
                pp_path.write_text(parser_content, encoding="utf-8")
                pp_path.chmod(0o755)
            else:
                raise FileNotFoundError(
                    f"Pro instance {record.instance_id}: parser.py not found "
                    f"in {get_pro_scripts_dir() / record.instance_id}. "
                    f"Pro evaluation requires per-instance parser.py."
                )

        # ---- 1. instruction.md ----
        instruction_template = self.jinja_env.get_template(f"{subdir}/instruction.md.j2")

        lang_ctx = _get_language_context(record.language)

        # Resolve native test_cmd for TestCase/FullPipe instructions so agent
        # knows which test runner to use in eval.sh (avoids wrong framework).
        # swebench path: read from MAP_REPO_VERSION_TO_SPECS.
        # Pro path: not standardised — leave empty (agent uses project conventions).
        native_test_cmd = ""
        if (self.problem_type in ("TestCase", "FullPipe")
                and benchmark != "swebench-pro"
                and record.repo and record.version):
            try:
                from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
                native_test_cmd = (
                    MAP_REPO_VERSION_TO_SPECS
                    .get(record.repo, {})
                    .get(record.version, {})
                    .get("test_cmd", "")
                )
            except Exception:
                pass

        instruction_content = instruction_template.render(
            problem_statement=record.problem_statement,
            repo=record.repo,
            version=record.version,
            base_commit=record.base_commit,
            instance_id=record.instance_id,
            benchmark=benchmark,
            blind_mode=self.blind_mode,
            # Native test command for TestCase/FullPipe (empty string for Pro)
            test_cmd=native_test_cmd,
            # Pro-specific fields
            requirements=record.raw_instance.get("requirements", ""),
            interface=record.raw_instance.get("interface", ""),
            behavior_spec=record.behavior_spec or "",
            **lang_ctx,
        )
        paths.instruction_path.write_text(instruction_content, encoding="utf-8")

        # ---- 2. task.toml ----
        task_toml_template = self.jinja_env.get_template("task.toml.j2")
        task_toml_content = task_toml_template.render(
            difficulty=record.difficulty or "hard",
            problem_type=self.problem_type,
            agent_timeout=str(int(self.agent_timeout)),
            verifier_timeout=str(int(self.verifier_timeout)),
            build_timeout=str(int(self.build_timeout)),
        )
        paths.config_path.write_text(task_toml_content, encoding="utf-8")

        # ---- 3. tests/config.json (raw SWE-bench instance data) ----
        paths.config_json_path.write_text(
            json.dumps(record.raw_instance, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # ---- 4. environment/Dockerfile ----
        self._write_dockerfile(record, paths, base_image, env_image, subdir)

        # ---- 5. tests/test.sh ----
        self._write_test_sh(record, paths, subdir)

        # ---- 6. solution/solve.sh (ground-truth patch) ----
        self._write_solve_sh(record, paths)

        logger.info(f"Generated Harbor task: {task_dir}")
        return task_dir

    def _write_dockerfile(
        self,
        record: CCBRecord,
        paths: HarborTaskPaths,
        base_image: str,
        env_image: str,
        subdir: str,
    ) -> None:
        """Generate and write the Dockerfile for this task."""
        dockerfile_template = self.jinja_env.get_template(f"{subdir}/Dockerfile.j2")
        benchmark = self.benchmark_type

        # ---- Pro path: pre-built DockerHub image, no swebench setup ----
        if benchmark == "swebench-pro":
            pro_image = base_image  # For Pro, _get_docker_image already returns pro image

            # Copy instruction.md (solving agent's prompt) into Docker build context
            # so the eval agent can read the task description from {{ workdir }}/instruction.md
            shutil.copy2(paths.instruction_path, paths.environment_dir / "instruction.md")

            # Render eval_prompt.md into tests_dir (Verifier uploads it to /tests/ after agent runs)
            eval_prompt_template = self.jinja_env.get_template(f"{subdir}/eval_prompt.md.j2")
            _eval_timeout_for_prompt = (self.EVAL_AGENT_TIMEOUT_FULLPIPE
                                        if self.problem_type == "FullPipe"
                                        else self.EVAL_AGENT_TIMEOUT)
            eval_prompt_content = eval_prompt_template.render(
                benchmark="pro",
                instance_id=record.instance_id,
                workdir="/app",
                language=record.language,
                fail_to_pass_list=_normalize_test_list(record.FAIL_TO_PASS),
                pass_to_pass_list=_normalize_test_list(record.PASS_TO_PASS),
                eval_agent_timeout=_eval_timeout_for_prompt,
            )
            (paths.tests_dir / "eval_prompt.md").write_text(eval_prompt_content, encoding="utf-8")

            # Render blind eval_prompt (no gold.patch reference) for dual-agent eval
            if self.problem_type in ("FullPipe", "Development", "TestCase"):
                eval_prompt_blind_template = self.jinja_env.get_template(f"{subdir}/eval_prompt_blind.md.j2")
                eval_prompt_blind_content = eval_prompt_blind_template.render(
                    benchmark="pro",
                    instance_id=record.instance_id,
                    workdir="/app",
                    language=record.language,
                    fail_to_pass_list=_normalize_test_list(record.FAIL_TO_PASS),
                    pass_to_pass_list=_normalize_test_list(record.PASS_TO_PASS),
                    eval_agent_timeout=_eval_timeout_for_prompt,
                )
                (paths.tests_dir / "eval_prompt_blind.md").write_text(eval_prompt_blind_content, encoding="utf-8")

            # Pro: determine patches to COPY + apply in Dockerfile (build-time only).
            # gold.patch / eval_prompt.md / test_patch (Development) are in tests_dir,
            # injected by Verifier upload_dir after agent runs — not in Dockerfile.
            test_patch_file = None
            code_patch_file = None
            if self.problem_type == "Development":
                # Development: test_patch NOT applied at build time (anti-leakage).
                # Written to tests_dir; Verifier uploads to /tests/ after agent runs.
                # script_eval.sh applies it at eval time before running tests.
                if record.test_patch:
                    (paths.tests_dir / "test_patch.diff").write_text(record.test_patch, encoding="utf-8")
                gold_parts = [p for p in [record.patch, record.test_patch] if p]
                if gold_parts:
                    (paths.tests_dir / "gold.patch").write_text("\n".join(gold_parts), encoding="utf-8")
            elif self.problem_type == "TestCase":
                # TestCase (Pro): COPY and apply code_patch in Dockerfile.
                # Agent sees fixed code and writes tests against it.
                if record.patch:
                    patch_path = paths.environment_dir / "code_patch.diff"
                    patch_path.write_text(record.patch, encoding="utf-8")
                    code_patch_file = "code_patch.diff"
                # Gold patch written to tests_dir (Verifier uploads after agent runs)
                gold_parts = [p for p in [record.patch, record.test_patch] if p]
                if gold_parts:
                    (paths.tests_dir / "gold.patch").write_text("\n".join(gold_parts), encoding="utf-8")
                # Copy judge_testcase.py into Docker build context (required by Dockerfile COPY)
                shutil.copy2(
                    self.template_dir / "testcase" / "judge_testcase.py",
                    paths.environment_dir / "judge_testcase.py",
                )
            elif self.problem_type == "Environment":
                if record.patch:
                    patch_path = paths.environment_dir / "code_patch.diff"
                    patch_path.write_text(record.patch, encoding="utf-8")
                    code_patch_file = "code_patch.diff"
                if record.test_patch:
                    patch_path = paths.environment_dir / "test_patch.diff"
                    patch_path.write_text(record.test_patch, encoding="utf-8")
                    test_patch_file = "test_patch.diff"
                # Environment: no gold patch needed for eval agent
            elif self.problem_type == "FullPipe":
                # FullPipe: gold patch written to tests_dir (Verifier uploads after agent runs)
                gold_parts = [p for p in [record.patch, record.test_patch] if p]
                if gold_parts:
                    (paths.tests_dir / "gold.patch").write_text("\n".join(gold_parts), encoding="utf-8")

            # FullPipe (Pro): codex exec CLI used for eval (no MinimalCodeAgent needed)
            minimal_code_agent_dir = None

            # Pro: copy run_script.sh and parser.py into build context
            # (already written to tests/ by generate_task, copy to environment/ for Docker COPY)
            has_run_script = False
            has_parser = False
            src_rs = paths.tests_dir / "run_script.sh"
            if src_rs.exists():
                shutil.copy2(src_rs, paths.environment_dir / "run_script.sh")
                has_run_script = True
            src_pp = paths.tests_dir / "parser.py"
            if src_pp.exists():
                shutil.copy2(src_pp, paths.environment_dir / "parser.py")
                has_parser = True

            # Build the Dockerfile-time before_repo_set_cmd: reset repo to base_commit.
            # Matches Harbor official swebenchpro adapter (adapter.py line 293-296):
            #   git reset --hard {base_commit} && git clean -fd && git checkout {base_commit}
            # Do NOT include the last line of before_repo_set_cmd — it checks out gold test
            # files from the resolved commit, which would leak test info to the agent.
            base_commit = record.base_commit or ""
            if base_commit:
                before_repo_set_cmd_docker = (
                    f"git reset --hard {base_commit} && "
                    f"git clean -fd && "
                    f"git checkout {base_commit}"
                )
            else:
                before_repo_set_cmd_docker = ""

            # 将预下载的 opencode 二进制硬链接到 Docker build context（版本固定，离线安装）
            _link_opencode_binary(paths.environment_dir)

            dockerfile_content = dockerfile_template.render(
                benchmark="pro",
                pro_image=pro_image,
                env_image=env_image,
                base_image=base_image,
                image=pro_image,
                workdir="/app",
                test_patch_file=test_patch_file,
                code_patch_file=code_patch_file,
                gold_patch_file=None,
                has_run_script=has_run_script,
                has_parser=has_parser,
                minimal_code_agent_dir=minimal_code_agent_dir,
                before_repo_set_cmd_docker=before_repo_set_cmd_docker,
                language=record.language,
            )
            paths.dockerfile_path.write_text(dockerfile_content, encoding="utf-8")
            self._write_runtime_compose(paths, workdir="/app")
            return

        # ---- swebench path (existing Python logic) ----
        # ---- Generate setup_repo.sh (swebench instance-level init) ----
        # swebench has 3 image layers: base -> env -> instance.
        # base: ubuntu + conda; env: base + conda env + pip deps; instance: env + git clone + checkout.
        # Our Dockerfiles start FROM env_image (or base_image), so we need to run the
        # instance-level setup_repo.sh to clone and set up the git repo at /testbed.
        # For Environment and FullPipe: we start FROM base_image which lacks the
        # testbed conda env. The full install_repo_script tries `conda activate testbed`
        # which fails. Use a stripped-down script that only does git clone + checkout.
        if self.problem_type in ("Environment", "FullPipe"):
            install_repo_script = get_repo_only_setup_script(record.raw_instance)
        else:
            install_repo_script = get_install_repo_script(record.raw_instance)
        setup_repo_path = paths.environment_dir / "setup_repo.sh"
        setup_repo_path.write_text(install_repo_script, encoding="utf-8")

        # Copy instruction.md (solving agent's prompt) into Docker build context
        # so the eval agent can read the task description from /testbed/instruction.md
        shutil.copy2(paths.instruction_path, paths.environment_dir / "instruction.md")

        # Render eval_prompt.md into tests_dir (Verifier uploads it to /tests/ after agent runs)
        eval_prompt_template = self.jinja_env.get_template(f"{subdir}/eval_prompt.md.j2")
        _eval_timeout_for_prompt = (self.EVAL_AGENT_TIMEOUT_FULLPIPE
                                    if self.problem_type == "FullPipe"
                                    else self.EVAL_AGENT_TIMEOUT)
        eval_prompt_content = eval_prompt_template.render(
            benchmark="swebench",
            instance_id=record.instance_id,
            workdir="/testbed",
            language=record.language,
            fail_to_pass_list=_normalize_test_list(record.FAIL_TO_PASS),
            pass_to_pass_list=_normalize_test_list(record.PASS_TO_PASS),
            eval_agent_timeout=_eval_timeout_for_prompt,
        )
        (paths.tests_dir / "eval_prompt.md").write_text(eval_prompt_content, encoding="utf-8")

        # Render blind eval_prompt (no gold.patch reference) for dual-agent eval
        if self.problem_type in ("FullPipe", "Development", "TestCase"):
            eval_prompt_blind_template = self.jinja_env.get_template(f"{subdir}/eval_prompt_blind.md.j2")
            eval_prompt_blind_content = eval_prompt_blind_template.render(
                benchmark="swebench",
                instance_id=record.instance_id,
                workdir="/testbed",
                language=record.language,
                fail_to_pass_list=_normalize_test_list(record.FAIL_TO_PASS),
                pass_to_pass_list=_normalize_test_list(record.PASS_TO_PASS),
                eval_agent_timeout=_eval_timeout_for_prompt,
            )
            (paths.tests_dir / "eval_prompt_blind.md").write_text(eval_prompt_blind_content, encoding="utf-8")

        # Determine which patches to copy into build context (for RUN git apply)
        # gold.patch / eval_prompt.md / test_patch (Development) are in tests_dir,
        # injected by Verifier upload_dir after agent runs — not in Dockerfile.
        test_patch_file = None
        code_patch_file = None

        if self.problem_type == "Development":
            # Development: test_patch and gold.patch written to tests_dir (anti-leakage).
            # Verifier uploads to /tests/ after agent runs.
            # swebench eval_script_list handles test_patch apply at eval time.
            gold_parts = [p for p in [record.patch, record.test_patch] if p]
            if gold_parts:
                (paths.tests_dir / "gold.patch").write_text("\n".join(gold_parts), encoding="utf-8")
            if record.test_patch:
                (paths.tests_dir / "test_patch.diff").write_text(record.test_patch, encoding="utf-8")

        elif self.problem_type == "TestCase":
            # TestCase: COPY and apply code_patch in Dockerfile.
            # Agent sees fixed code and writes tests against it.
            if record.patch:
                patch_path = paths.environment_dir / "code_patch.diff"
                patch_path.write_text(record.patch, encoding="utf-8")
                code_patch_file = "code_patch.diff"
            # Gold patch written to tests_dir (Verifier uploads after agent runs)
            gold_parts = [p for p in [record.patch, record.test_patch] if p]
            if gold_parts:
                (paths.tests_dir / "gold.patch").write_text("\n".join(gold_parts), encoding="utf-8")
            # Copy judge_testcase.py into Docker build context (required by Dockerfile COPY)
            shutil.copy2(
                self.template_dir / "testcase" / "judge_testcase.py",
                paths.environment_dir / "judge_testcase.py",
            )

        elif self.problem_type == "Environment":
            # Environment: apply both code+test patches
            if record.patch:
                patch_path = paths.environment_dir / "code_patch.diff"
                patch_path.write_text(record.patch, encoding="utf-8")
                code_patch_file = "code_patch.diff"
            if record.test_patch:
                patch_path = paths.environment_dir / "test_patch.diff"
                patch_path.write_text(record.test_patch, encoding="utf-8")
                test_patch_file = "test_patch.diff"
            # Environment: no gold patch needed for eval agent

        elif self.problem_type == "FullPipe":
            # FullPipe: gold patch written to tests_dir (Verifier uploads after agent runs)
            gold_parts = [p for p in [record.patch, record.test_patch] if p]
            if gold_parts:
                (paths.tests_dir / "gold.patch").write_text("\n".join(gold_parts), encoding="utf-8")

        # Choose base or env image depending on problem type
        if self.problem_type in ("Development", "TestCase"):
            image = env_image
        else:
            image = base_image

        # 将预下载的 opencode 二进制硬链接到 Docker build context（版本固定，离线安装）
        _link_opencode_binary(paths.environment_dir)

        # Development/Environment 题型：复制零依赖 swebench grading 脚本到 build context
        if self.problem_type in ("Development", "Environment"):
            _copy_swebench_grading(self.template_dir, paths.environment_dir)

        dockerfile_content = dockerfile_template.render(
            benchmark="swebench",
            env_image=env_image,
            base_image=base_image,
            image=image,
            test_patch_file=test_patch_file,
            test_patch_copy_only_file=None,
            code_patch_file=code_patch_file,
            gold_patch_file=None,
        )
        paths.dockerfile_path.write_text(dockerfile_content, encoding="utf-8")
        self._write_runtime_compose(paths, workdir="/testbed")

    @staticmethod
    def _write_runtime_compose(paths: HarborTaskPaths, workdir: str) -> None:
        """生成 environment/docker-compose.yaml，通过 volume mount 注入运行时文件。

        instruction.md 需要在容器启动时即可用（solving agent 依赖它），
        但其内容变化不应触发 Docker image 重建。
        eval_prompt.md 已移至 tests_dir，由 Verifier upload_dir 在 agent 跑完后注入。
        """
        compose_content = (
            "services:\n"
            "  main:\n"
            "    volumes:\n"
            f"      - ./instruction.md:{workdir}/instruction.md:ro\n"
        )
        compose_path = paths.environment_dir / "docker-compose.yaml"
        compose_path.write_text(compose_content, encoding="utf-8")

    def _write_test_sh(
        self,
        record: CCBRecord,
        paths: HarborTaskPaths,
        subdir: str,
    ) -> None:
        """Generate and write test.sh (orchestrator) plus script_eval.sh and agent_eval.sh."""
        test_sh_template = self.jinja_env.get_template(f"{subdir}/test.sh.j2")
        benchmark = self.benchmark_type

        # Pro-specific template variables
        selected_test_files = ""
        before_repo_set_cmd = ""

        if benchmark == "swebench-pro":
            # Parse selected_test_files_to_run
            raw_test_files = record.selected_test_files_to_run or ""
            if isinstance(raw_test_files, str):
                try:
                    parsed = json.loads(raw_test_files)
                    if isinstance(parsed, list):
                        selected_test_files = ",".join(parsed)
                    else:
                        selected_test_files = str(parsed)
                except (json.JSONDecodeError, ValueError):
                    selected_test_files = raw_test_files
            elif isinstance(raw_test_files, list):
                selected_test_files = ",".join(raw_test_files)

            # before_repo_set_cmd: the Pro dataset provides a multi-line script
            # whose last line is:
            #   git checkout <resolved_commit> -- <test_files>
            # This restores the gold test files from the resolved commit.
            #
            # - Development: agent IS allowed to modify test files (test_patch may
            #   contain intentionally buggy expectations that the agent must fix),
            #   so we must NOT restore test files before running. Drop entirely.
            # - TestCase: agent writes the test files themselves — those files ARE
            #   the evaluation target. Restoring gold test files defeats the purpose
            #   of evaluating the agent's test quality. Drop entirely.
            # - Environment: agent does NOT write test files (only configures the
            #   environment). Restoring gold test files is correct and desired.
            before_repo_set_cmd = ""
            if self.problem_type == "Environment":
                raw_cmd = record.before_repo_set_cmd or ""
                if raw_cmd:
                    # Take only the last line (as in legacy eval_pro.py)
                    before_repo_set_cmd = raw_cmd.strip().split("\n")[-1]

            # FullPipe (Pro): use EvalAgent for LLM-based 6-point scoring
            use_eval_agent = (self.problem_type == "FullPipe")

            _eval_timeout = (self.EVAL_AGENT_TIMEOUT_FULLPIPE
                             if self.problem_type == "FullPipe"
                             else self.EVAL_AGENT_TIMEOUT)
            render_kwargs_pro = dict(
                benchmark="pro",
                instance_id=record.instance_id,
                workdir="/app",
                selected_test_files=selected_test_files,
                before_repo_set_cmd=before_repo_set_cmd,
                language=record.language,
                # Pass empty defaults for swebench-specific vars
                test_commands="",
                test_cmd="",
                test_files="",
                use_eval_agent=use_eval_agent,
                eval_model=self.eval_model,
                opencode_model=self.opencode_model,
                opencode_api_key=self.opencode_api_key,
                fail_to_pass_list=_normalize_test_list(record.FAIL_TO_PASS),
                pass_to_pass_list=_normalize_test_list(record.PASS_TO_PASS),
                script_eval_timeout=self.SCRIPT_EVAL_TIMEOUT,
                eval_agent_timeout=_eval_timeout,
                ablation_eval_models=self.ablation_eval_models,
            )

            test_sh_content = test_sh_template.render(**render_kwargs_pro)
            paths.test_sh_path.write_text(test_sh_content, encoding="utf-8")
            paths.test_sh_path.chmod(0o755)

            # Write script_eval.sh (not for FullPipe which has no script eval track)
            if self.problem_type != "FullPipe":
                script_eval_template = self.jinja_env.get_template(f"{subdir}/script_eval.sh.j2")
                script_eval_content = script_eval_template.render(**render_kwargs_pro)
                script_eval_path = paths.tests_dir / "script_eval.sh"
                script_eval_path.write_text(script_eval_content, encoding="utf-8")
                script_eval_path.chmod(0o755)

            # Write agent_eval.sh
            agent_eval_template = self.jinja_env.get_template(f"{subdir}/agent_eval.sh.j2")
            agent_eval_content = agent_eval_template.render(**render_kwargs_pro)
            agent_eval_path = paths.tests_dir / "agent_eval.sh"
            agent_eval_path.write_text(agent_eval_content, encoding="utf-8")
            agent_eval_path.chmod(0o755)

            return

        # ---- swebench path (existing) ----
        # Generate test commands for swebench eval
        test_commands = ""
        test_cmd = ""
        test_files = ""
        use_eval_agent = False

        if self.problem_type == "Development":
            if record.test_patch:
                test_commands = self._generate_test_commands(record)
            else:
                test_commands = "echo 'No test_patch available' && exit 1"
        elif self.problem_type == "Environment":
            # Environment: use full swebench eval_script_list (skip_install=True so the
            # agent's configured environment is not overwritten by a fresh pip install).
            # This is language-agnostic and works for Java/Rust/Go/Python alike.
            if record.test_patch:
                test_commands = self._generate_test_commands(record, skip_install=True)
            else:
                test_commands = "echo 'No test_patch available' && exit 1"
        elif self.problem_type == "TestCase":
            # TestCase: agent writes eval.sh, test.sh just runs it
            test_commands = ""
        elif self.problem_type == "FullPipe":
            # FullPipe: use EvalAgent by default
            use_eval_agent = True
            if record.test_patch:
                test_commands = self._generate_test_commands(record)

        _eval_timeout = (self.EVAL_AGENT_TIMEOUT_FULLPIPE
                         if self.problem_type == "FullPipe"
                         else self.EVAL_AGENT_TIMEOUT)
        render_kwargs_swe = dict(
            benchmark="swebench",
            instance_id=record.instance_id,
            test_commands=test_commands,
            test_cmd=test_cmd,
            test_files=test_files,
            use_eval_agent=use_eval_agent,
            eval_model=self.eval_model,
            opencode_model=self.opencode_model,
            opencode_api_key=self.opencode_api_key,
            language=record.language,
            fail_to_pass_list=_normalize_test_list(record.FAIL_TO_PASS),
            pass_to_pass_list=_normalize_test_list(record.PASS_TO_PASS),
            script_eval_timeout=self.SCRIPT_EVAL_TIMEOUT,
            eval_agent_timeout=_eval_timeout,
            ablation_eval_models=self.ablation_eval_models,
        )

        test_sh_content = test_sh_template.render(**render_kwargs_swe)
        paths.test_sh_path.write_text(test_sh_content, encoding="utf-8")
        paths.test_sh_path.chmod(0o755)

        # Write script_eval.sh (not for FullPipe which has no script eval track)
        if self.problem_type != "FullPipe":
            script_eval_template = self.jinja_env.get_template(f"{subdir}/script_eval.sh.j2")
            script_eval_content = script_eval_template.render(**render_kwargs_swe)
            script_eval_path = paths.tests_dir / "script_eval.sh"
            script_eval_path.write_text(script_eval_content, encoding="utf-8")
            script_eval_path.chmod(0o755)

        # Write agent_eval.sh
        agent_eval_template = self.jinja_env.get_template(f"{subdir}/agent_eval.sh.j2")
        agent_eval_content = agent_eval_template.render(**render_kwargs_swe)
        agent_eval_path = paths.tests_dir / "agent_eval.sh"
        agent_eval_path.write_text(agent_eval_content, encoding="utf-8")
        agent_eval_path.chmod(0o755)

    def _write_solve_sh(self, record: CCBRecord, paths: HarborTaskPaths) -> None:
        """Write the ground-truth solution script."""
        benchmark = self.benchmark_type
        workdir = "/app" if benchmark == "swebench-pro" else "/testbed"
        patch_text = (record.patch or "").strip()
        if patch_text:
            solve_content = dedent(f"""\
                #!/bin/bash
                set -euo pipefail

                cat > {workdir}/solution_patch.diff << '__SOLUTION__'
                {patch_text}
                __SOLUTION__

                cd {workdir}
                patch --fuzz=5 -p1 -i {workdir}/solution_patch.diff
            """)
        else:
            solve_content = dedent("""\
                #!/bin/bash
                echo "No gold patch available for this instance"
            """)

        paths.solve_sh_path.write_text(solve_content, encoding="utf-8")
        paths.solve_sh_path.chmod(0o755)

    def generate_many(
        self,
        records: List[CCBRecord],
        overwrite: bool = False,
    ) -> Tuple[List[Path], List[Tuple[str, str]]]:
        """
        Batch-generate Harbor task directories.

        Returns:
            (success_paths, failures[(instance_id, reason), ...])
        """
        successes: List[Path] = []
        failures: List[Tuple[str, str]] = []

        for idx, record in enumerate(records, 1):
            try:
                path = self.generate_task(record, overwrite=overwrite)
                logger.info(f"[{idx}/{len(records)}] OK   {record.instance_id} -> {path}")
                successes.append(path)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                logger.error(f"[{idx}/{len(records)}] FAIL {record.instance_id}: {msg}")
                failures.append((record.instance_id, msg))

        return successes, failures


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_instances_from_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Load instances from a JSONL file."""
    instances = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(f"Skip invalid JSON line in {path}: {e}")
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("instance_id") and obj.get("repo") and obj.get("base_commit") is not None:
                instances.append(obj)
            else:
                logger.warning(f"Skip line missing instance_id/repo/base_commit in {path}")
    return instances


def load_instances_from_hf(dataset_name: str, split: str = "test") -> List[Dict[str, Any]]:
    """Load instances from a HuggingFace dataset.

    Supports:
      - princeton-nlp/SWE-bench_Verified
      - ScaleAI/SWE-bench_Pro
      - Any HuggingFace dataset with instance_id, repo, base_commit fields
    """
    from datasets import load_dataset
    ds = load_dataset(dataset_name, split=split)
    instances = []
    for row in ds:
        d = dict(row)
        if not (d.get("instance_id") and d.get("repo") and d.get("base_commit") is not None):
            continue
        # Pro datasets may not have 'version' field
        if "version" not in d:
            d["version"] = ""
        instances.append(d)
    logger.info(f"Loaded {len(instances)} instances from HuggingFace: {dataset_name} [{split}]")
    return instances


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="CCB Adapter: Convert SWE-Cycle instances to Harbor task directories",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dataset-path", type=Path, required=True,
        help="Path to JSONL dataset file",
    )
    parser.add_argument(
        "--problem-type", required=True,
        choices=["Development", "TestCase", "Environment", "FullPipe"],
        help="Problem type to generate tasks for",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("harbor_tasks"),
        help="Output directory for Harbor task directories (default: harbor_tasks)",
    )
    parser.add_argument(
        "--instance-ids", nargs="+", default=None,
        help="Filter specific instance IDs",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Overwrite existing task directories",
    )
    parser.add_argument(
        "--timeout-multiplier", type=float, default=1.0,
        help="Multiply all timeouts by this factor (default: 1.0)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Only show what would be generated, don't create files",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum number of instances to convert",
    )
    parser.add_argument(
        "--eval-model", default="claude_sonnet_4_6",
        help="Model name for EvalAgent in FullPipe test.sh (default: claude_sonnet_4_6)",
    )
    parser.add_argument(
        "--ablation-eval-models", default=None,
        help="Comma-separated eval model names for ablation study",
    )
    parser.add_argument(
        "--benchmark", default=None,
        choices=["swebench", "swebench-pro"],
        help="Benchmark type override. If omitted, inferred from --dataset-path name (default: swebench)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    # Load instances
    if not args.dataset_path.exists():
        logger.error(f"Dataset file not found: {args.dataset_path}")
        return 1

    instances = load_instances_from_jsonl(args.dataset_path)
    logger.info(f"Loaded {len(instances)} instances from {args.dataset_path}")

    # Filter by instance IDs
    if args.instance_ids:
        id_set = set(args.instance_ids)
        instances = [inst for inst in instances if inst["instance_id"] in id_set]
        logger.info(f"Filtered to {len(instances)} instances")

    # Apply limit
    if args.limit is not None:
        instances = instances[:args.limit]

    if not instances:
        logger.error("No instances to convert")
        return 1

    # Create records
    records = [
        CCBRecord.from_dict(inst, args.problem_type)
        for inst in instances
    ]

    if args.dry_run:
        print(f"Would generate {len(records)} Harbor task directories:")
        for r in records:
            print(f"  {args.output_dir / r.instance_id}/")
        print(f"\nProblem type: {args.problem_type}")
        print(f"Output dir: {args.output_dir}")
        return 0

    # Determine benchmark type: --benchmark > infer from --dataset-path > default
    if args.benchmark:
        benchmark_type = args.benchmark
    elif args.dataset_path:
        path_lower = str(args.dataset_path).lower()
        if "swe-bench_pro" in path_lower or "swebench_pro" in path_lower or "sweap_eval" in path_lower:
            benchmark_type = "swebench-pro"
        else:
            benchmark_type = "swebench"
    else:
        benchmark_type = "swebench"

    # Generate tasks
    ablation_models = [m.strip() for m in args.ablation_eval_models.split(",")] if args.ablation_eval_models else []
    adapter = CCBToHarbor(
        output_root=args.output_dir,
        problem_type=args.problem_type,
        timeout_multiplier=args.timeout_multiplier,
        eval_model=args.eval_model,
        benchmark_type=benchmark_type,
        ablation_eval_models=ablation_models,
    )
    successes, failures = adapter.generate_many(records, overwrite=args.overwrite)

    # Summary
    print(f"\nDone. Success: {len(successes)}  Failures: {len(failures)}")
    if failures:
        print("Failures:")
        for iid, reason in failures:
            print(f"  - {iid}: {reason}")

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
