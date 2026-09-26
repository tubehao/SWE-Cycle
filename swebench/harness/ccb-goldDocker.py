import docker
import argparse
import json
import sys
from pathlib import Path
from swebench.harness.constants import (
    DOCKER_PATCH,
    DOCKER_USER,
    DOCKER_WORKDIR,
    UTF8,
)
from swebench.harness.docker_utils import (
    copy_to_container,
)
from swebench.harness.docker_build import (
    build_container,
    build_env_images,
    setup_logger,
)
from swebench.harness.test_spec.test_spec import make_test_spec
from swebench.harness.utils import load_swebench_dataset

GIT_APPLY_CMDS = [
    "git apply --verbose",
    "git apply --verbose --reject",
    "patch --batch --fuzz=5 -p1 -i",
]

def create_gold_container(
    instance_id: str,
    dataset_name: str,
    split: str,
    run_id: str,
    force_rebuild: bool
):
    # 定义默认的命名空间和标签，确保 swebench 能找到基础镜像
    NAMESPACE = "swebench"
    INSTANCE_IMAGE_TAG = "latest"
    ENV_IMAGE_TAG = "latest"

    client = docker.from_env()

    print(f"[INFO] Loading dataset {dataset_name}...", file=sys.stderr)
    dataset = load_swebench_dataset(dataset_name, split)
    
    instance = next((i for i in dataset if i['instance_id'] == instance_id), None)
    if not instance:
        raise ValueError(f"Instance ID {instance_id} not found in {dataset_name}")

    gold_patch = instance['patch']

    # --- 修正点 1: 创建 TestSpec 时传入 namespace ---
    test_spec = make_test_spec(
        instance,
        namespace=NAMESPACE,
        instance_image_tag=INSTANCE_IMAGE_TAG,
        env_image_tag=ENV_IMAGE_TAG
    )

    log_dir = Path("logs") / run_id / instance_id
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger(instance_id, log_dir / "run.log")

    try:
        print(f"[INFO] Ensuring environment image exists for {instance_id}...", file=sys.stderr)
        
        # --- 修正点 2: 构建环境镜像时传入 namespace 和 tags ---
        build_env_images(
            client, 
            [instance], 
            force_rebuild=False,
            max_workers=1,
            namespace=NAMESPACE,           # 必须指定
            instance_image_tag=INSTANCE_IMAGE_TAG,
            env_image_tag=ENV_IMAGE_TAG
        )

        print(f"[INFO] Building container for {instance_id}...", file=sys.stderr)
        container = build_container(
            test_spec=test_spec,
            client=client,
            run_id=run_id,
            logger=logger,
            nocache=False,
            force_rebuild=force_rebuild
        )
        container.start()
        
        print(f"[INFO] Applying GOLD patch...", file=sys.stderr)
        
        patch_file = log_dir / "gold.patch"
        patch_file.write_text(gold_patch if gold_patch else "")
        
        copy_to_container(container, patch_file, Path(DOCKER_PATCH))
        
        applied_patch = False
        for git_apply_cmd in GIT_APPLY_CMDS:
            val = container.exec_run(
                f"{git_apply_cmd} {DOCKER_PATCH}",
                workdir=DOCKER_WORKDIR,
                user=DOCKER_USER,
            )
            if val.exit_code == 0:
                applied_patch = True
                break
        
        if not applied_patch:
            error_msg = f"Failed to apply gold patch: {val.output.decode(UTF8)}"
            logger.error(error_msg)
            # 如果需要调试失败的情况，可以注释掉下面这行 raise
            raise RuntimeError(error_msg)

        print(container.id)
        print(f"[SUCCESS] Container created. ID: {container.id}", file=sys.stderr)

    except Exception as e:
        logger.error(f"Error creating container: {e}")
        print(f"[ERROR] {e}", file=sys.stderr)
        raise e

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance_id", type=str, required=True)
    parser.add_argument("--dataset_name", type=str, default="SWE-bench/SWE-bench_Lite")
    parser.add_argument("--split", type=str, default="test")
    parser.add_argument("--run_id", type=str, default="debug_gold")
    parser.add_argument("--force_rebuild", action="store_true")

    args = parser.parse_args()
    
    create_gold_container(
        instance_id=args.instance_id,
        dataset_name=args.dataset_name,
        split=args.split,
        run_id=args.run_id,
        force_rebuild=args.force_rebuild
    )