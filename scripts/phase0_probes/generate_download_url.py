#!/usr/bin/env python3
"""Phase 0 探针 ② 辅助: 本地签一个 R2 GET presigned URL.

项目方在海外/本地跑, 输出的 `export PRESIGNED_URL=...` 一行发给测试者.

用法:
    export R2_ENDPOINT='https://<account>.r2.cloudflarestorage.com'
    export R2_ACCESS_KEY_ID='...'
    export R2_SECRET_ACCESS_KEY='...'
    export R2_ARTIFACTS_BUCKET='avt-artifacts'    # 默认
    export R2_TEST_KEY='probe/sample_100mb.bin'   # 默认
    export EXPIRES=21600                           # 默认 6 小时
    python3 generate_download_url.py
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    try:
        import boto3
        from botocore.client import Config
    except ImportError:
        print("错误: 需要 boto3 (pip install boto3)", file=sys.stderr)
        return 2

    required = ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"错误: 未设置环境变量: {', '.join(missing)}", file=sys.stderr)
        print("参考 README.md § 探针 ②", file=sys.stderr)
        return 2

    bucket = os.environ.get("R2_ARTIFACTS_BUCKET", "avt-artifacts")
    key = os.environ.get("R2_TEST_KEY", "probe/sample_100mb.bin")
    expires = int(os.environ.get("EXPIRES", "21600"))  # 6 小时,够三网从容跑

    client = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4", region_name="auto"),
    )

    # 先 head_object 确认文件存在,避免给测试者一个 404 URL
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        size_mb = head.get("ContentLength", 0) / 1024 / 1024
    except Exception as exc:
        print(f"错误: 无法 head {bucket}/{key}: {exc}", file=sys.stderr)
        print(f"请先上传 100MB 测试样本 (README.md § A1):", file=sys.stderr)
        print(
            f"  aws s3 cp ./sample_100mb.bin s3://{bucket}/{key} "
            f"--endpoint-url='{os.environ['R2_ENDPOINT']}' --region=auto",
            file=sys.stderr,
        )
        return 1

    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires,
    )

    print("# --- Phase 0 探针 ② 预签名 URL ---")
    print(f"# Bucket   : {bucket}")
    print(f"# Key      : {key}")
    print(f"# Size     : {size_mb:.1f} MB")
    print(f"# Expires  : {expires // 60} 分钟 ({expires} 秒)")
    print(f"# 发给测试者的命令(一整行):")
    print()
    print(f"export PRESIGNED_URL='{url}'")
    print()
    print("# 测试者然后跑:")
    print("#   bash probe2_r2_download.sh 电信")
    print("#   bash probe2_r2_download.sh 联通")
    print("#   bash probe2_r2_download.sh 移动")
    return 0


if __name__ == "__main__":
    sys.exit(main())
