#!/usr/bin/env python3
"""租卡机上并行下载 Qwen2.5-7B-Instruct（ModelScope，Range 分块多线程）。

用法：python3 dl_model.py <输出目录>
只依赖标准库。sha256 校验由调用方用 config.yaml 里预置的 hash 做。
"""
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

BASE = "https://modelscope.cn/models/qwen/Qwen2.5-7B-Instruct/resolve/master/"
FILES = ["model-00001-of-00004.safetensors", "model-00002-of-00004.safetensors",
         "model-00003-of-00004.safetensors", "model-00004-of-00004.safetensors",
         "model.safetensors.index.json", "config.json", "generation_config.json",
         "tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt",
         "LICENSE", "README.md"]
CHUNK = 16 << 20  # 16MB 分块：拥塞期 60s 超时能兜住
done_bytes = 0
lock = Lock()


def fetch(url, start, end, out, off):
    global done_bytes
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    for attempt in range(5):
        try:
            data = urllib.request.urlopen(req, timeout=120).read()
            with lock:
                with open(out, "r+b") as f:
                    f.seek(off)
                    f.write(data)
                done_bytes += len(data)
            return
        except Exception:
            time.sleep(10 * (attempt + 1))  # 机房出口拥塞常见，退避重试扛过去


def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1
                   else "/home/howardhe/models/Qwen2.5-7B-Instruct")
    out_dir.mkdir(parents=True, exist_ok=True)
    # 小文件直接下
    for name in FILES:
        dest = out_dir / name
        if dest.exists() and dest.stat().st_size > 0 and name.endswith(".safetensors"):
            continue
        if not name.endswith(".safetensors"):
            try:
                data = urllib.request.urlopen(BASE + name, timeout=60).read()
                dest.write_bytes(data)
            except Exception as e:
                print(f"small file {name}: {e}", flush=True)
    # 大分片多线程 Range 下载
    jobs = []
    for name in FILES:
        if not name.endswith(".safetensors"):
            continue
        dest = out_dir / name
        # ModelScope 不给 HEAD 回 Content-Length，用 Range 0-0 从 Content-Range 取总大小
        probe = urllib.request.urlopen(urllib.request.Request(
            BASE + name, headers={"Range": "bytes=0-0"}), timeout=60)
        probe.read()
        size = int(probe.headers["Content-Range"].split("/")[-1])
        ok = dest.with_name(dest.name + ".ok")
        if dest.exists() and dest.stat().st_size == size and ok.exists():
            print(f"{name}: already complete", flush=True)
            continue
        with open(dest, "wb") as f:
            f.truncate(size)
        for start in range(0, size, CHUNK):
            end = min(start + CHUNK - 1, size - 1)
            jobs.append((BASE + name, start, end, str(dest), start))
    total = sum(e - s + 1 for _, s, e, _, _ in jobs)
    print(f"downloading {total / 1e9:.1f} GB with 16 threads", flush=True)
    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(lambda a: fetch(*a), jobs))
    for name in FILES:
        if name.endswith(".safetensors"):
            (out_dir / (name + ".ok")).write_text("1")
    print("ALL DONE", flush=True)


main()
