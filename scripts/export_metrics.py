#!/usr/bin/env python3
"""从 Prometheus 把全部 vllm:* 时间序列导出为 JSON，一个指标一个文件。

作用与验收判据见 docs/metrics.md
"""
import argparse
import json
import os
import re
import sys

import requests


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prom-url", default="http://localhost:9090")
    ap.add_argument("--start", type=float, required=True, help="Unix 时间戳")
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--step", default="1s")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    # 通配导出全部 vllm 指标：指标名跨版本有改名（如 gpu_cache_usage_perc 与
    # gpu_cache_usage_gauge），写死单名会漏数据，改名问题留给画图脚本按前缀匹配。
    resp = requests.get(
        f"{args.prom_url}/api/v1/query_range",
        params={
            "query": '{__name__=~"vllm:.+"}',
            "start": args.start,
            "end": args.end,
            "step": args.step,
        },
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()["data"]["result"]

    grouped = {}
    for series in result:
        name = series["metric"]["__name__"]
        grouped.setdefault(name, []).append(series)

    for name, series_list in grouped.items():
        fname = re.sub(r"[^a-zA-Z0-9_]", "_", name) + ".json"
        with open(os.path.join(args.out_dir, fname), "w") as f:
            json.dump(series_list, f)
        print(f"[export] {name}: {len(series_list)} 条序列 -> {fname}")

    if not grouped:
        print("[export] 没导出到任何 vllm 指标，检查 Prometheus 是否在跑、vLLM 是否被抓到",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
