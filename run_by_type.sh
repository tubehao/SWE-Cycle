#!/bin/bash
# run_by_type.sh — 运行指定题型评测（全量数据集）
#
# 用法:
#   ./run_by_type.sh [--type TYPE]... [--agent AGENT] [dataset] [output-dir]
#
# 示例:
#   ./run_by_type.sh --type Development
#   ./run_by_type.sh --type Development --type TestCase
#   ./run_by_type.sh --agent opencode --type Environment
#   ./run_by_type.sh --type FullPipe princeton-nlp/SWE-bench_Verified my_output 2>&1 | tee my_output.log
#   ./run_by_type.sh                   # 默认运行全部题型，agent 为 claude-code

set -euo pipefail

TYPES=()
AGENT="claude-code"

# 解析命名参数
while [[ $# -gt 0 ]]; do
    case "$1" in
        --type)
            TYPES+=("$2")
            shift 2
            ;;
        --agent)
            AGENT="$2"
            shift 2
            ;;
        *)
            break
            ;;
    esac
done

# 剩余位置参数
DATASET="${1:-princeton-nlp/SWE-bench_Verified}"
OUTPUT_DIR="${2:-harbor_run_full}"

# 未指定 --type 则运行全部
if [[ ${#TYPES[@]} -eq 0 ]]; then
    TYPES=(Development TestCase Environment FullPipe)
fi

COMMON_ARGS=(
    --dataset "$DATASET"
    --agent "$AGENT"
    --overwrite-tasks
    --resume
    --output-dir "$OUTPUT_DIR"
)

echo "========================================"
echo "Dataset : $DATASET"
echo "Agent   : $AGENT"
echo "Sampling: full dataset (no sampling)"
echo "Types   : ${TYPES[*]}"
echo "Output  : $OUTPUT_DIR"
echo "========================================"

for TYPE in "${TYPES[@]}"; do
    echo ""
    echo "-------- [$TYPE] 开始 $(date '+%Y-%m-%d %H:%M:%S') --------"
    python run_harbor.py "${COMMON_ARGS[@]}" --problem-type "$TYPE"
    echo "-------- [$TYPE] 完成 $(date '+%Y-%m-%d %H:%M:%S') --------"
done

echo ""
echo "========================================"
echo "全部题型运行完毕 $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================"
