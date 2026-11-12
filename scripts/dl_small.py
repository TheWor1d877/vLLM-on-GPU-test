#!/usr/bin/env python3
"""小模型整目录下载（ModelScope API 列文件，顺序下+退避重试）。

用法：python3 dl_small.py <model_id> <输出目录>
只依赖标准库。适用于 0.5B 这类单分片小模型；7B 大分片用 dl_model.py。
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

model_id, out = sys.argv[1], Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
api = f"https://modelscope.cn/api/v1/models/{model_id}/repo/files?Recursive=true"
files = json.load(urllib.request.urlopen(api, timeout=60))["Data"]["Files"]
for f in files:
    if f.get("Type") != "blob":
        continue
    name = f["Path"]
    dest = out / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size == f.get("Size"):
        print(f"{name}: already complete", flush=True)
        continue
    url = f"https://modelscope.cn/models/{model_id}/resolve/master/{name}"
    for attempt in range(5):
        try:
            data = urllib.request.urlopen(url, timeout=120).read()
            dest.write_bytes(data)
            break
        except Exception:
            time.sleep(10 * (attempt + 1))
    print(f"{name}: {dest.stat().st_size} bytes", flush=True)
print("ALL DONE", flush=True)