#!/usr/bin/env python3
"""
SWE-Cycle Data Collector
从 SWE-bench 系列数据集采集 GitHub PR 信息，用于分析开发周期特征。

Usage:
    export GITHUB_TOKEN=your_token  # 可选，但建议设置（5000 req/h vs 60 req/h）
    python swe_cycle_data_collector.py --dataset verified --output verified_enriched.csv
    python swe_cycle_data_collector.py --dataset multilingual --output multilingual_enriched.csv
    python swe_cycle_data_collector.py --dataset pro --output pro_enriched.csv
"""

import os
import re
import time
import argparse
import requests
import pandas as pd
from tqdm import tqdm
from datasets import load_dataset
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')


# GitHub API 配置
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
HEADERS = {
    'Accept': 'application/vnd.github.v3+json',
}
if GITHUB_TOKEN:
    HEADERS['Authorization'] = f'token {GITHUB_TOKEN}'
    print("✓ Using GitHub token (5000 requests/hour)")
else:
    print("⚠ No GitHub token found (60 requests/hour). Set GITHUB_TOKEN for faster collection.")


# 数据集配置
DATASETS = {
    'verified': 'princeton-nlp/SWE-bench_Verified',
    'multilingual': 'SWE-bench/SWE-bench_Multilingual',
    'pro': 'ScaleAI/SWE-bench_Pro',
    'lite': 'princeton-nlp/SWE-bench_Lite',
    'full': 'princeton-nlp/SWE-bench',
}


def parse_instance_id_standard(instance_id: str):
    """解析标准格式: django__django-12345 -> (django/django, 12345)"""
    parts = instance_id.rsplit("-", 1)
    repo = parts[0].replace("__", "/")
    pr_num = int(parts[1])
    return repo, pr_num


def parse_instance_id_pro(instance_id: str, repo: str):
    """解析 Pro 格式: instance_repo__repo-commit_hash-version -> commit_hash"""
    match = re.search(r'-([a-f0-9]{40})(?:-|$)', instance_id)
    if match:
        return match.group(1)
    return None


def get_pr_number_from_commit(repo: str, commit_sha: str) -> int:
    """通过 commit 找关联的 PR number"""
    url = f"https://api.github.com/repos/{repo}/commits/{commit_sha}/pulls"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 200 and r.json():
            return r.json()[0].get("number")
    except Exception as e:
        print(f"Error getting PR from commit: {e}")
    return None


def get_pr_info(repo: str, pr_num: int) -> dict:
    """获取 PR 详细信息"""
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_num}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 200:
            pr = r.json()
            created = pr.get("created_at")
            merged = pr.get("merged_at")

            # 计算周期天数
            cycle_days = None
            if created and merged:
                try:
                    created_dt = datetime.fromisoformat(created.replace('Z', '+00:00'))
                    merged_dt = datetime.fromisoformat(merged.replace('Z', '+00:00'))
                    cycle_days = (merged_dt - created_dt).days
                except:
                    pass

            return {
                "pr_number": pr_num,
                "files_changed": pr.get("changed_files"),
                "additions": pr.get("additions"),
                "deletions": pr.get("deletions"),
                "commits": pr.get("commits"),
                "review_comments": pr.get("review_comments"),
                "comments": pr.get("comments"),  # issue-style comments
                "created_at": created[:10] if created else None,
                "merged_at": merged[:10] if merged else None,
                "cycle_days": cycle_days,
                "state": pr.get("state"),
                "merged": pr.get("merged"),
            }
        elif r.status_code == 403:
            # Rate limit
            reset_time = int(r.headers.get('X-RateLimit-Reset', 0))
            wait_time = max(reset_time - time.time(), 60)
            print(f"\n⚠ Rate limited. Waiting {wait_time:.0f}s...")
            time.sleep(wait_time)
            return get_pr_info(repo, pr_num)  # Retry
        elif r.status_code == 404:
            return {"error": "PR not found"}
    except Exception as e:
        return {"error": str(e)}
    return {"error": f"HTTP {r.status_code}"}


def collect_dataset(dataset_name: str, output_file: str, limit: int = None) -> pd.DataFrame:
    """采集指定数据集的 PR 信息，支持断点恢复"""

    if dataset_name not in DATASETS:
        raise ValueError(f"Unknown dataset: {dataset_name}. Available: {list(DATASETS.keys())}")

    # 断点恢复：加载已有输出文件
    existing_df = None
    done_ids = set()
    if os.path.exists(output_file):
        existing_df = pd.read_csv(output_file)
        done_ids = set(existing_df['instance_id'])
        print(f"✓ Resuming: {len(done_ids)} instances already processed in {output_file}")

    print(f"\n{'='*60}")
    print(f"Loading {dataset_name}...")
    ds = load_dataset(DATASETS[dataset_name], split='test')
    total = len(ds) if limit is None else min(limit, len(ds))
    remaining = sum(1 for i in range(total) if ds[i]['instance_id'] not in done_ids)
    print(f"Total instances: {len(ds)}, processing: {total}, remaining: {remaining}")
    print('='*60)

    if remaining == 0:
        print("✓ All instances already processed, nothing to do.")
        return existing_df

    is_pro = dataset_name == 'pro'
    results = []

    for i in tqdm(range(total), desc=f"Collecting {dataset_name}"):
        item = ds[i]
        instance_id = item['instance_id']

        if instance_id in done_ids:
            continue

        if is_pro:
            repo = item['repo']
            commit = parse_instance_id_pro(instance_id, repo)
            if not commit:
                results.append({
                    "instance_id": instance_id,
                    "repo": repo,
                    "error": "could not parse commit"
                })
                continue
            pr_num = get_pr_number_from_commit(repo, commit)
            if not pr_num:
                results.append({
                    "instance_id": instance_id,
                    "repo": repo,
                    "error": "no PR found for commit"
                })
                continue
        else:
            repo, pr_num = parse_instance_id_standard(instance_id)

        info = get_pr_info(repo, pr_num)

        result = {
            "instance_id": instance_id,
            "repo": repo,
            **info
        }
        results.append(result)

        # 简单的 rate limit 保护
        if not GITHUB_TOKEN and (i + 1) % 50 == 0:
            print(f"\n⏳ Pausing to avoid rate limit...")
            time.sleep(60)

    new_df = pd.DataFrame(results)
    if existing_df is not None:
        return pd.concat([existing_df, new_df], ignore_index=True)
    return new_df


def analyze_by_repo(df: pd.DataFrame) -> pd.DataFrame:
    """按 repo 分组统计"""

    # 过滤掉有 error 的行
    valid_df = df[df['error'].isna()] if 'error' in df.columns else df

    stats = valid_df.groupby('repo').agg({
        'instance_id': 'count',
        'files_changed': ['mean', 'median', 'max'],
        'additions': ['mean', 'median', 'max'],
        'deletions': ['mean', 'median', 'max'],
        'commits': ['mean', 'median', 'max'],
        'review_comments': ['mean', 'median', 'max', lambda x: (x > 0).sum()],
        'cycle_days': ['mean', 'median', 'max'],
    }).round(1)

    # Flatten column names
    stats.columns = ['_'.join(col).strip('_') for col in stats.columns]
    stats = stats.rename(columns={
        'instance_id_count': 'count',
        'review_comments_<lambda_0>': 'has_review_count'
    })

    return stats.sort_values('count', ascending=False)


def print_summary(df: pd.DataFrame, dataset_name: str):
    """打印摘要统计"""

    valid_df = df[df['error'].isna()] if 'error' in df.columns else df
    error_count = len(df) - len(valid_df)

    print(f"\n{'='*60}")
    print(f"Summary: {dataset_name}")
    print('='*60)
    print(f"Total instances: {len(df)}")
    print(f"Successfully fetched: {len(valid_df)}")
    print(f"Errors: {error_count}")

    if len(valid_df) == 0:
        return

    print(f"\n--- PR Metrics ---")
    print(f"Files changed:    mean={valid_df['files_changed'].mean():.1f}, median={valid_df['files_changed'].median():.0f}, max={valid_df['files_changed'].max()}")
    print(f"Additions:        mean={valid_df['additions'].mean():.1f}, median={valid_df['additions'].median():.0f}, max={valid_df['additions'].max()}")
    print(f"Commits:          mean={valid_df['commits'].mean():.1f}, median={valid_df['commits'].median():.0f}, max={valid_df['commits'].max()}")
    print(f"Review comments:  mean={valid_df['review_comments'].mean():.1f}, median={valid_df['review_comments'].median():.0f}, max={valid_df['review_comments'].max()}")
    print(f"Cycle days:       mean={valid_df['cycle_days'].mean():.1f}, median={valid_df['cycle_days'].median():.0f}, max={valid_df['cycle_days'].max()}")

    print(f"\n--- Cycle Indicators ---")
    has_review = (valid_df['review_comments'] > 0).sum()
    has_iteration = (valid_df['commits'] > 1).sum()
    long_cycle = (valid_df['cycle_days'] >= 1).sum()

    print(f"Has review (review_comments > 0):  {has_review}/{len(valid_df)} ({100*has_review/len(valid_df):.1f}%)")
    print(f"Has iteration (commits > 1):       {has_iteration}/{len(valid_df)} ({100*has_iteration/len(valid_df):.1f}%)")
    print(f"Long cycle (cycle_days >= 1):      {long_cycle}/{len(valid_df)} ({100*long_cycle/len(valid_df):.1f}%)")

    # Full cycle: 满足所有条件
    full_cycle = valid_df[
        (valid_df['review_comments'] > 0) &
        (valid_df['commits'] > 1) &
        (valid_df['cycle_days'] >= 1)
    ]
    print(f"Full cycle (all above):            {len(full_cycle)}/{len(valid_df)} ({100*len(full_cycle)/len(valid_df):.1f}%)")


def main():
    parser = argparse.ArgumentParser(description='Collect GitHub PR info for SWE-bench datasets')
    parser.add_argument('--dataset', type=str, default='verified',
                        choices=list(DATASETS.keys()),
                        help='Dataset to collect')
    parser.add_argument('--output', type=str, default=None,
                        help='Output CSV file path')
    parser.add_argument('--limit', type=int, default=None,
                        help='Limit number of instances to collect (for testing)')
    parser.add_argument('--by-repo', action='store_true',
                        help='Also output per-repo statistics')

    args = parser.parse_args()
    output_file = args.output or f"{args.dataset}_enriched.csv"

    # 采集数据（支持断点恢复）
    df = collect_dataset(args.dataset, output_file, limit=args.limit)

    # 保存完整结果（含 error 行，用于断点恢复）
    df.to_csv(output_file, index=False)
    print(f"\n✓ Saved to {output_file}")

    # 打印原始摘要
    print_summary(df, args.dataset)

    # 过滤统计
    print(f"\n--- Filtering ---")
    total = len(df)

    error_df = df[df['error'].notna()] if 'error' in df.columns else pd.DataFrame()
    if len(error_df) > 0:
        print(f"Errors ({len(error_df)}):")
        for reason, count in error_df['error'].value_counts().items():
            print(f"  - {reason}: {count}")

    valid_df = df[df['error'].isna()] if 'error' in df.columns else df.copy()
    too_complex = valid_df[valid_df['files_changed'] > 50]
    if len(too_complex) > 0:
        print(f"Too complex (files_changed > 50): {len(too_complex)}")

    filtered_df = valid_df[valid_df['files_changed'] <= 50]
    if 'error' in filtered_df.columns:
        filtered_df = filtered_df.drop(columns=['error'])

    print(f"Total: {total} → Removed: {len(error_df) + len(too_complex)} → Remaining: {len(filtered_df)}")

    filtered_output = output_file.replace('.csv', '_filtered.csv')
    filtered_df.to_csv(filtered_output, index=False)
    print(f"✓ Filtered results saved to {filtered_output}")

    # 按 repo 统计
    if args.by_repo:
        repo_stats = analyze_by_repo(df)
        repo_output = output_file.replace('.csv', '_by_repo.csv')
        repo_stats.to_csv(repo_output)
        print(f"\n✓ Per-repo stats saved to {repo_output}")
        print("\n--- Top repos by count ---")
        print(repo_stats[['count', 'review_comments_mean', 'commits_mean', 'cycle_days_mean']].head(10))


if __name__ == '__main__':
    main()
