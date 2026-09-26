#!/bin/bash
# run_all_types.sh — 顺序运行四种题型评测（每个仓库各取一题）
#
# 用法:
#   ./run_all_types.sh [dataset] [output-dir] [seed]
#
# 示例:
#   ./run_all_types.sh                                                        # 默认
#   ./run_all_types.sh princeton-nlp/SWE-bench_Verified
#   ./run_all_types.sh SWE-bench/SWE-bench_Multilingual harbor_output_multilingual 2>&1 | tee harbor_output_multilingual.log
#   ./run_all_types.sh ScaleAI/SWE-bench_Pro harbor_output_pro 2>&1 | tee harbor_output_pro.log

set -euo pipefail

DATASET="${1:-princeton-nlp/SWE-bench_Verified}"
OUTPUT_DIR="${2:-harbor_run_py}"
SEED="${3:-42}"

COMMON_ARGS=(
    --dataset "$DATASET"
    --agent claude-code
    --one-per-repo
    --shuffle
    --seed "$SEED"
    --overwrite-tasks
    --output-dir "$OUTPUT_DIR"
)

PROBLEM_TYPES=(Development TestCase Environment FullPipe)

echo "========================================"
echo "Dataset : $DATASET"
echo "Sampling: one per repo (shuffle seed=$SEED)"
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
