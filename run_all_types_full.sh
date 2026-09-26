#!/bin/bash
# run_all_types_full.sh — 顺序运行四种题型评测（全量数据集，不做采样）
#
# 用法:
#   ./run_all_types_full.sh [dataset] [output-dir]
#
# 示例:
#   ./run_all_types_full.sh
#   ./run_all_types_full.sh princeton-nlp/SWE-bench_Verified harbor_output_full 2>&1 | tee harbor_output_full.log
#   ./run_all_types_full.sh ScaleAI/SWE-bench_Pro harbor_output_pro_full 2>&1 | tee harbor_output_pro_full.log

set -euo pipefail

DATASET="${1:-princeton-nlp/SWE-bench_Verified}"
OUTPUT_DIR="${2:-harbor_run_full}"

COMMON_ARGS=(
    --dataset "$DATASET"
    --agent claude-code
    --overwrite-tasks
    --resume
    --output-dir "$OUTPUT_DIR"
)

PROBLEM_TYPES=(Development TestCase Environment FullPipe)

echo "========================================"
echo "Dataset : $DATASET"
echo "Sampling: full dataset (no sampling)"
echo "Types   : ${PROBLEM_TYPES[*]}"
echo "Output  : $OUTPUT_DIR"
echo "========================================"

for TYPE in "${PROBLEM_TYPES[@]}"; do
    echo ""
    echo "-------- [$TYPE] 开始 $(date '+%Y-%m-%d %H:%M:%S') --------"
    python run_harbor.py "${COMMON_ARGS[@]}" --problem-type "$TYPE"
    echo "-------- [$TYPE] 完成 $(date '+%Y-%m-%d %H:%M:%S') --------"
done

echo ""
echo "========================================"
echo "全部题型运行完毕 $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================"
