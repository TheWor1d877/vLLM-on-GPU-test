#!/usr/bin/env python3
"""脚本 2：正式执行驱动。自动起 Prometheus、dmon、vLLM，按档位跑并发扫描与
现象 B/A，导出指标，产物落在 results/<日期时间>/。

用法、配置字段与验收判据见 docs/run-bench.md
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent


def expand(p):
    return str(Path(os.path.expanduser(str(p))))


def sh(cmd):
    return subprocess.check_output(cmd, shell=True, text=True).strip()


def wait_health(port, timeout_s=900):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if requests.get(f"http://localhost:{port}/health", timeout=2).ok:
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    raise RuntimeError(f"vLLM 服务 {timeout_s}s 内没就绪，看 vllm 日志排查")


def gpu_mem_used_mb():
    out = sh("nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits")
    return int(out.splitlines()[0])


def model_hashes(model_dir):
    """对权重文件逐个算 sha256。7B 上建议本地算好写进 config.model_sha256。"""
    hashes = {}
    for f in sorted(Path(model_dir).glob("*.safetensors")):
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 22), b""):
                h.update(block)
        hashes[f.name] = h.hexdigest()
    return hashes


def parse_mem_from_log(log_path):
    """从 vLLM 启动日志解析权重占用与 KV 池预算（GiB）。解析不到返回 None，
    调用方不写 memory_breakdown，报告显存账卡隐藏，不回填假数。"""
    text = Path(log_path).read_text(errors="ignore")
    w = re.search(r"(?:Model loading took|Loading model weights took)\s+([\d.]+)\s*Gi?B", text)
    kv = re.search(r"Available KV cache memory:\s*([\d.]+)\s*Gi?B", text)
    return (float(w.group(1)) if w else None, float(kv.group(1)) if kv else None)


class Server:
    """vLLM 服务生命周期。日志单独落盘，KV 池容量从日志里解析。"""

    def __init__(self, cfg, log_path):
        self.cfg = cfg
        self.log_path = Path(log_path)
        self.proc = None
        self.log_fh = None

    def start(self, extra_args=()):
        venv = expand(self.cfg["venv"])
        cmd = [f"{venv}/bin/vllm", "serve", expand(self.cfg["model"]),
               "--served-model-name", self.cfg["served_name"],
               "--port", str(self.cfg["port"])]
        cmd += list(self.cfg.get("server_args", [])) + list(extra_args)
        # flashinfer 采样内核要 JIT（nvcc），本机 nvcc 12.4 太老、venv 自带
        # CUDA 头文件不全，两条路都编不过；关掉后走 vLLM 自带原生采样。
        env = os.environ.copy()
        env.setdefault("VLLM_USE_FLASHINFER_SAMPLER", "0")
        self.log_fh = open(self.log_path, "wb")
        print(f"[server] {' '.join(cmd)}")
        self.proc = subprocess.Popen(cmd, stdout=self.log_fh, stderr=subprocess.STDOUT, env=env)
        wait_health(self.cfg["port"])

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait()
            self.proc = None
            self.log_fh.close()

    def kv_cache_info(self):
        text = self.log_path.read_text(errors="ignore")
        m = re.findall(r"(GPU KV cache size: [^\n]+)", text)
        return m[-1] if m else None


def bench(py, cfg, concurrency, n, out_path):
    cmd = [py, str(SCRIPT_DIR / "bench_client.py"),
           "--base-url", f"http://localhost:{cfg['port']}",
           "--model", cfg["served_name"],
           "--concurrency", str(concurrency),
           "--num-requests", str(n),
           "--input-len", str(cfg["input_len"]),
           "--output-len", str(cfg["output_len"]),
           "--seed", str(cfg["seed"]),
           "--out", expand(out_path)]
    subprocess.run(cmd, check=True)
    # 硬验收：ignore_eos + max_tokens 下每条请求必须恰好输出 output_len 个
    # token。少一个都说明计数装置或服务端生成有问题，立即失败，不带病跑完。
    data = json.load(open(expand(out_path)))
    bad = [(r["request_id"], r["output_tokens"]) for r in data["requests"]
           if r["output_tokens"] != cfg["output_len"]]
    if bad:
        raise RuntimeError(f"{out_path} 输出 token 数不等于 {cfg['output_len']}：{bad[:5]}")


def main():
    cfg_path = expand(sys.argv[1]) if len(sys.argv) > 1 else SCRIPT_DIR / "config.yaml"
    cfg = yaml.safe_load(open(cfg_path))
    py = f"{expand(cfg['venv'])}/bin/python"

    results = SCRIPT_DIR.parent / "results" / time.strftime("%Y%m%d_%H%M")
    (results / "client").mkdir(parents=True)
    (results / "prometheus").mkdir()

    # ---- meta：版本锁 ----
    meta = {
        "start_ts": time.time(),
        "gpu": sh("nvidia-smi --query-gpu=name --format=csv,noheader"),
        "driver": sh("nvidia-smi --query-gpu=driver_version --format=csv,noheader"),
        "gpu_mem_total_gb": round(int(sh("nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits")) / 1024),
        "model": expand(cfg["model"]),
        "server_args": cfg.get("server_args", []),
        "config": {k: v for k, v in cfg.items() if k != "model_sha256"},
    }
    meta["cuda"] = subprocess.check_output(
        [py, "-c", "import torch; print(torch.version.cuda)"], text=True).strip()
    meta["vllm"] = subprocess.check_output(
        [py, "-c", "import vllm; print(vllm.__version__)"], text=True).strip()
    meta["model_sha256"] = cfg.get("model_sha256") or model_hashes(meta["model"])
    with open(results / "meta.json", "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)

    # ---- 采集进程 ----
    prom = subprocess.Popen(
        ["prometheus", f"--config.file={SCRIPT_DIR / 'prometheus.yml'}",
         f"--storage.tsdb.path={results / 'prom_db'}",
         "--web.listen-address=:9090"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    prom_start = time.time()
    dmon_fh = open(results / "dmon.csv", "w")
    dmon = subprocess.Popen(["nvidia-smi", "dmon", "-s", "pucvmet", "-d", "1", "-o", "DT"],
                            stdout=dmon_fh)

    server = Server(cfg, results / "vllm_main.log")
    try:
        # ---- 第一步：显存基线 ----
        meta["idle_gpu_mem_used_mb"] = gpu_mem_used_mb()  # 必须在服务启动前测
        server.start()
        meta["kv_cache_info"] = server.kv_cache_info()
        alloc = subprocess.check_output(
            [py, "-c", "import torch; torch.zeros(1, device='cuda'); "
                       "print(torch.cuda.memory_allocated())"], text=True).strip()
        meta["torch_empty_alloc_bytes"] = int(alloc)
        meta["cuda_context_overhead_mb"] = gpu_mem_used_mb() - meta["idle_gpu_mem_used_mb"]

        # 显存账：权重与 KV 池取日志实测，其余 footprint 归激活与上下文碎片
        w_gb, kv_gb = parse_mem_from_log(results / "vllm_main.log")
        footprint_gb = meta["cuda_context_overhead_mb"] / 1024
        if w_gb and kv_gb and footprint_gb > w_gb + kv_gb:
            meta["memory_breakdown"] = {
                "模型权重": round(w_gb, 2), "KV 块池": round(kv_gb, 2),
                "激活与上下文碎片": round(footprint_gb - w_gb - kv_gb, 2)}

        # ---- 第二步：并发扫描，每档双跑 ----
        for level in cfg["levels"]:
            for run_idx in (1, 2):
                bench(py, cfg, level, cfg["requests_per_level"],
                      results / "client" / f"level_{level}_run{run_idx}.json")
                time.sleep(cfg["double_run_gap_s"])

        # ---- 现象 B：默认服务上长跑（排在 A 前，省一次服务重启）----
        pb = cfg.get("phenomenon_b", {})
        if pb.get("enabled"):
            bench(py, cfg, pb["concurrency"], pb["requests"],
                  results / "client" / "phenomenon_b.json")

        # ---- 现象 A：压小 KV 池重启服务，主动触发抢占 ----
        server.stop()
        pa = cfg.get("phenomenon_a", {})
        if pa.get("enabled"):
            server_a = Server(cfg, results / "vllm_phenomA.log")
            server_a.start(extra_args=pa.get("extra_server_args", []))
            meta["phenom_a_kv_cache_info"] = server_a.kv_cache_info()
            bench(py, cfg, pa["concurrency"], pa["requests"],
                  results / "client" / "phenomenon_a.json")
            time.sleep(5)  # 等尾部观测被抓取进 TSDB 再停服务
            server_a.stop()

        # ---- 现象 C：换 AWQ 权重重启服务，同档对比 + 量化 kernel 观测 ----
        pc = cfg.get("phenomenon_c", {})
        if pc.get("enabled"):
            server_c = Server({**cfg, "model": pc["model"]}, results / "vllm_phenomC.log")
            server_c.start(extra_args=pc.get("extra_server_args", []))
            try:
                meta["phenom_c_kv_cache_info"] = server_c.kv_cache_info()
                meta["phenom_c_model_sha256"] = (pc.get("model_sha256")
                                                 or model_hashes(expand(pc["model"])))
                log_text = (results / "vllm_phenomC.log").read_text(errors="ignore")
                hits = [ln.strip() for ln in log_text.splitlines()
                        if re.search(r"marlin|falling back", ln, re.I)]
                if not hits:  # 没有决定性行时退抓 awq 相关行，排除启动命令回显
                    hits = [ln.strip() for ln in log_text.splitlines()
                            if re.search(r"awq", ln, re.I) and "--" not in ln]
                meta["phenom_c_kernel_lines"] = hits[:20]
                bench(py, cfg, pc["concurrency"], pc["requests"],
                      results / "client" / "phenomenon_c.json")
                time.sleep(5)  # 等尾部观测被抓取进 TSDB 再停服务
            finally:
                server_c.stop()

        # ---- 导出指标 ----
        # 主服务的尾部观测同样要等抓取：A/C 段已在各自停服务前留 5s，
        # 这里再留 5s 覆盖主服务最后一段（B 段）的尾部。
        time.sleep(5)
        subprocess.run(
            [py, str(SCRIPT_DIR / "export_metrics.py"),
             "--start", str(prom_start), "--end", str(time.time()),
             "--out-dir", str(results / "prometheus")], check=True)
    finally:
        server.stop()
        dmon.terminate()
        dmon_fh.close()
        prom.terminate()
        prom.wait()

    meta["end_ts"] = time.time()
    with open(results / "meta.json", "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print(f"[run] 产物目录：{results}")


if __name__ == "__main__":
    main()
