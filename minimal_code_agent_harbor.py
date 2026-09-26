#!/usr/bin/env python3
"""
MinimalCodeAgent Harbor Adapter

Implements Harbor's BaseInstalledAgent interface for MinimalCodeAgent,
enabling it to be used as a Harbor agent within the CCB workflow.

MinimalCodeAgent is an ADK-based web service (not a CLI). Its workflow:
  1. docker_start.sh → tmux: start MCP server + ADK API server → HTTP port 8080
  2. run_agent.py → HTTP POST to localhost:8080/run
  3. Dependencies: conda env `minimalcodeagent`, miniconda, tmux, lsof
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any

import sys

# Ensure harbor is importable
try:
    from harbor.agents.installed.base import BaseInstalledAgent, ExecInput
    from harbor.models.agent.context import AgentContext
except ImportError:
    _harbor_src = Path(__file__).parent / "harbor" / "src"
    if _harbor_src.exists():
        sys.path.insert(0, str(_harbor_src))
    from harbor.agents.installed.base import BaseInstalledAgent, ExecInput
    from harbor.models.agent.context import AgentContext


class MinimalCodeAgent(BaseInstalledAgent):
    """
    Harbor adapter for MinimalCodeAgent (ADK-based web service).

    This agent starts an ADK API server in a tmux session, waits for it to
    become ready, then sends the instruction via HTTP.
    """

    def __init__(self, *args, **kwargs):
        self._eval_model_name = kwargs.pop("eval_model_name", None)
        super().__init__(*args, **kwargs)

    @property
    def eval_model_name(self):
        return self._eval_model_name

    @staticmethod
    def name() -> str:
        return "minimal-code-agent"

    @property
    def _install_agent_template_path(self) -> Path:
        return Path(__file__).parent / "install-minimal-code-agent.sh.j2"

    def get_version_command(self) -> str | None:
        return None  # MinimalCodeAgent doesn't have a version command

    async def setup(self, environment: "BaseEnvironment") -> None:
        """Upload MinimalCodeAgent source dir, then run install.sh."""
        from harbor.environments.base import BaseEnvironment as _BE  # noqa: F401

        # 1. Upload Minimal-CodeAgent source code to container
        mca_src = Path(__file__).parent / "Minimal-CodeAgent"
        if not mca_src.exists():
            raise FileNotFoundError(
                f"Minimal-CodeAgent source directory not found: {mca_src}"
            )
        await environment.upload_dir(
            source_dir=mca_src,
            target_dir="/MinimalCodeAgent",
        )

        # 2. Run standard install.sh (installs miniconda, conda env, tmux, pip deps)
        await super().setup(environment)

    def create_run_agent_commands(self, instruction: str) -> list[ExecInput]:
        """
        Create commands to:
        1. Start the ADK service via docker_start.sh
        2. Wait for port 8080 to be ready
        3. Run the agent with the instruction
        """
        escaped = shlex.quote(instruction)
        model_name = self.model_name or "claude"

        env = {
            "ADK_MODEL": model_name,
            "ADK_EVAL_MODEL": self.eval_model_name or "",
            "CODE_AGENT_WORKSPACE_DIR": "/testbed",
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or "",
            "ANTHROPIC_AUTH_TOKEN": os.environ.get("ANTHROPIC_AUTH_TOKEN", ""),
            "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", ""),
        }

        return [
            # Start the ADK service in tmux
            ExecInput(
                command=(
                    "source /opt/miniconda3/etc/profile.d/conda.sh && "
                    "conda activate minimalcodeagent && "
                    "export CODE_AGENT_WORKSPACE_DIR=/testbed && "
                    "cd /MinimalCodeAgent && "
                    f"bash docker_start.sh {shlex.quote(model_name)} /testbed 8080 "
                    "/MinimalCodeAgent minimalcodeagent"
                ),
                env=env,
            ),
            # Wait for port + run agent
            ExecInput(
                command=(
                    "source /opt/miniconda3/etc/profile.d/conda.sh && "
                    "conda activate minimalcodeagent && "
                    # Wait for port 8080 to be ready (up to 60s)
                    "for i in $(seq 1 30); do "
                    "  lsof -Pi :8080 -sTCP:LISTEN -t >/dev/null 2>&1 && break; "
                    "  sleep 2; "
                    "done && "
                    # Run the agent with the instruction
                    f"python /MinimalCodeAgent/run_agent.py --prompt {escaped} "
                    "--workdir /testbed --port 8080 --agent code_agent_local"
                ),
                env=env,
            ),
        ]

    def populate_context_post_run(self, context: AgentContext) -> None:
        """
        MinimalCodeAgent doesn't produce a standard trajectory format.
        This is a no-op.
        """
        pass
