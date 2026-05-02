# SWE-Cycle

A multi-dimensional AI Agent evaluation framework that assesses code agents across four task types: **Development**, **TestCase**, **Environment**, and **FullPipe**.

Built on the [Harbor](https://github.com/harbor-ai/harbor) orchestration framework, SWE-Cycle runs agents in isolated Docker containers and evaluates their outputs via a dual-track system (script-based + agent-based evaluation).

---

## Architecture

```
run_harbor.py          # Entry point: load data → generate tasks → run Harbor Job → aggregate results
ccb_adapter.py         # JSONL/HuggingFace → Harbor task directory converter (core: CCBToHarbor)
config.py              # Global config (models, paths, prompts)
ccb_templates/         # Jinja2 templates (Dockerfile / instruction.md / test.sh)
  ├── development/     # Task type 1: Code fix
  ├── testcase/        # Task type 2: Test writing
  ├── environment/     # Task type 3: Environment setup
  └── fullpipe/        # Task type 4: Full pipeline
harbor/                # Harbor framework (Docker lifecycle, Trial orchestration)
dataset/               # Local JSONL datasets
swebench/              # SWE-bench evaluation utilities
SWE-bench_Pro-os/      # SWE-bench Pro dataset support
filtering/             # Dataset filtering pipeline
```

## Task Types

| Type | Description | Evaluation |
|------|-------------|------------|
| Development | Fix code bugs given a problem statement | Script (pytest F2P/P2P) + Eval Agent |
| TestCase | Write tests that catch a known bug | Script (F2P detection) + Eval Agent |
| Environment | Configure project environment from scratch | Script (test pass) + Eval Agent |
| FullPipe | Complete pipeline: env + code + test | Eval Agent only (3-axis scoring: ENV/CODE/TEST) |

## Setup

### Prerequisites

- Python 3.12
- Docker
- [uv](https://github.com/astral-sh/uv) (recommended)

### Installation

```bash
cp .env.example .env
# Edit .env with your API keys

uv sync
```

### Running Evaluations

```bash
# Run a single task type on SWE-bench Verified
uv run python run_harbor.py \
  --dataset-path dataset/verified.jsonl \
  --problem-type Development \
  --agent claude-code \
  --n-attempts 1

# Run all task types
bash run_all_types.sh
```

## Configuration

All sensitive configuration is read from environment variables (see `.env.example`):

- `OPENAI_BASE_URL` / `OPENAI_API_KEY` — for OpenAI-compatible eval models
- `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` — for Claude Code agent
- `CCB_PROXY_API_KEY` — for eval agent (OpenCode)

## Datasets

The `dataset/` directory contains:

- `verified.jsonl` — SWE-bench Verified subset
- `multilingual.jsonl` — Multi-language instances
- `swe-bench_pro.jsonl` — SWE-bench Pro subset

## Third-Party Licenses

This project builds upon the following open-source assets:

| Asset | License | URL |
|-------|---------|-----|
| SWE-bench | MIT | https://github.com/princeton-nlp/SWE-bench |
| SWE-bench Verified | MIT | https://github.com/princeton-nlp/SWE-bench |
| SWE-bench Pro | MIT | https://github.com/SWE-bench/SWE-bench-Pro |
| SWE-bench Multilingual | MIT | https://github.com/multi-swe-bench/multi-swe-bench |
| Harbor | Apache-2.0 | https://github.com/harbor-ai/harbor |
| OpenCode | MIT | https://github.com/opencode-ai/opencode |

All source datasets are derived from publicly available GitHub repositories under their respective open-source licenses. Our benchmark instances consist of metadata (commit hashes, issue descriptions, test patches) referencing these repositories and do not redistribute proprietary code.

## License

MIT
