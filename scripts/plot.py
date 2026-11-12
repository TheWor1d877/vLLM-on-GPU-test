#!/usr/bin/env python3
"""画图脚本：读 run_bench.py 的产物目录，输出内嵌数据与 ECharts 的单文件 HTML 报告。

用法：
  python3 scripts/plot.py --demo            # 用演示数据出初版（数据为合成，仅验证呈现）
  python3 scripts/plot.py results/<日期>/   # 正式数据
  输出默认写到产物目录的 report.html，--out 可改。

排队时间优先从 prometheus/ 的直方图重建；没有导出文件时退回 client JSON
头部的 queue_p50_ms/queue_p99_ms 字段（演示数据走这条回退）。
"""
import argparse
import json
import math
import os
import re
import statistics
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ECHARTS_PATH = SCRIPT_DIR / "assets" / "echarts.min.js"

# 高饱和蓝紫橙青，红绿不相邻
COLORS = ["#3b6fd4", "#7a5cd6", "#e8963c", "#3aa6a6", "#c95f81", "#8a8f9c"]


def pct(vals, p):
    """P50/P99 这类分位数，输入是逐请求实测列表。"""
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p / 100
    f, c = math.floor(k), math.ceil(k)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


def ecdf(vals, max_pts=None):
    """累积分布曲线点列 [[值, 累计占比]]；max_pts 等距降采样（保首尾）。"""
    s = sorted(v for v in vals if v is not None)
    if not s:
        return []
    n = len(s)
    idxs = (range(n) if not max_pts or n <= max_pts
            else [round(i * (n - 1) / (max_pts - 1)) for i in range(max_pts)])
    return [[round(s[i], 2), round((i + 1) / n, 4)] for i in idxs]


def load_level(path):
    d = json.load(open(path))
    reqs = d["requests"]
    ttfts = [r["ttft_ms"] for r in reqs if r.get("ttft_ms") is not None]
    itls = [x for r in reqs for x in r.get("itl_ms", [])]
    totals = [r["total_ms"] for r in reqs if r.get("total_ms") is not None]
    t0 = min((r["start_ts"] for r in reqs), default=None)
    t1 = max((r["start_ts"] + (r.get("total_ms") or 0) / 1000 for r in reqs if r.get("start_ts")),
             default=None)
    return {
        "level": d["concurrency"],
        "throughput": d["throughput_tokens_per_s"],
        "wall": d["wall_seconds"],
        "ttft_p50": pct(ttfts, 50), "ttft_p90": pct(ttfts, 90), "ttft_p99": pct(ttfts, 99),
        "itl_p50": pct(itls, 50), "itl_p99": pct(itls, 99),
        "total_p50": pct(totals, 50),
        "queue_p50": d.get("queue_p50_ms"), "queue_p99": d.get("queue_p99_ms"),
        "input_len": d.get("input_len"), "output_len": d.get("output_len"),
        "seed": d.get("seed"), "num_requests": d.get("num_requests"),
        "t0": t0, "t1": t1,
        "ttft_ecdf": ecdf(ttfts), "itl_ecdf": ecdf(itls, 300),
        "reqs": [{"i": r["request_id"], "ttft": r.get("ttft_ms"), "tot": r.get("total_ms"),
                  "out": r.get("output_tokens"),
                  "st": (round(r["start_ts"] - t0, 2) if (t0 and r.get("start_ts")) else None),
                  "itl_mean": (round(statistics.mean(r["itl_ms"]), 2) if r.get("itl_ms") else None)}
                 for r in reqs],
    }


def pick_prom_file(prom_dir, names, globpat=None):
    """优先按精确文件名取导出序列。真实导出里 *_created、external_*、
    *_by_reason 等文件会干扰模糊匹配，精确名取不到时才退回 glob。"""
    for n in names:
        f = Path(prom_dir) / f"{n}.json"
        if f.exists():
            return f
    if globpat:
        fs = sorted(Path(prom_dir).glob(f"{globpat}.json"))
        return fs[0] if fs else None
    return None


def load_prom_hist(prom_dir, names, pattern=None):
    """把 *_bucket 序列按 le 归并成 {le: [(ts, 累计计数)]}；文件不存在返回 None。"""
    f = pick_prom_file(prom_dir, names, f"*{pattern}*bucket" if pattern else None)
    if not f:
        return None
    series = json.load(open(f))
    by_le = {}
    for s in series:
        by_le.setdefault(s["metric"].get("le", "+Inf"), []).extend(
            (float(ts), float(v)) for ts, v in s["values"])
    for v in by_le.values():
        v.sort()
    return by_le


def window_delta(samples, t0, t1, tail_s=3.0):
    """samples 为按 ts 升序的 (ts, 累计值)。窗口内的增量：起点取 ≤t0 的最后一个
    样本，终点取 (t0, t1+tail_s] 内的最大值。tail_s 是服务端记入直方图再被
    抓取采到的滞后余量。终点不能取窗口之后的样本：现象窗口常是某台服务的
    最后一段，服务停后计数器随重启清零，窗口后的第一个样本属于下一台服务。
    取窗口内最大值而非末值，窗口内跨重启时也能拿到清零前的峰值。"""
    before = [v for ts, v in samples if ts <= t0]
    within = [v for ts, v in samples if t0 < ts <= t1 + tail_s]
    lo = before[-1] if before else 0.0
    hi = max(within) if within else lo
    return max(hi - lo, 0.0)


def hist_window_quantiles(by_le, windows, ps=(50, 99)):
    """每个时间窗取桶计数增量，按 histogram_quantile 同款算法重建分位数，返回 ms。"""
    out = []
    for t0, t1 in windows:
        vals = {le: window_delta(samples, t0, t1) for le, samples in by_le.items()}
        out.append(quantile_from_counts(vals, list(ps)))
    return out


def load_prom_counter_rate(prom_dir, names, pattern=None, windows=None):
    """counter 序列（如 generation_tokens_total）：每档窗口内增量 / 窗口秒数。"""
    f = pick_prom_file(prom_dir, names, f"*{pattern}" if pattern else None)
    if not f:
        return None
    series = json.load(open(f))
    if not series:
        return None
    vals = sorted((float(ts), float(v)) for ts, v in series[0]["values"])
    out = []
    for t0, t1 in windows:
        d = window_delta(vals, t0, t1)
        out.append(round(d / max(t1 - t0, 1e-9), 1))
    return out


def quantile_from_counts(vals, ps):
    """vals: {le: 窗口内计数}，按直方图近似算分位数，单位同 le（秒）。"""
    by_f = {float(k): v for k, v in vals.items() if k != "+Inf"}
    les = sorted(by_f)
    bins, prev = [], 0.0
    for le in les:
        c = by_f[le] - prev
        prev = by_f[le]
        bins.append((le, max(c, 0.0)))
    # 只用有限桶：+Inf 的增量与最大有限桶重复计数，会把高分位推落到最大桶值
    total = sum(c for _, c in bins)
    if total <= 0:
        return [None] * len(ps)
    res = []
    for p in ps:
        target, acc, lo = p / 100 * total, 0.0, 0.0
        for le, c in bins:
            if acc + c >= target:
                res.append(lo + (le - lo) * (target - acc) / max(c, 1e-9))
                break
            acc += c
            lo = le
        else:
            res.append(les[-1] if les else None)
    res = [(r if r is not None else 0) * 1000 for r in res]
    return res


def load_prom_gauge(prom_dir, names, pattern=None):
    f = pick_prom_file(prom_dir, names, f"*{pattern}" if pattern else None)
    if not f:
        return None
    series = json.load(open(f))
    if not series:
        return None
    return [(float(ts), float(v)) for ts, v in series[0]["values"]]


def load_prom_hit_ratio(prom_dir):
    """前缀缓存命中率：两个 counter 按时间戳对齐，逐点取增量比。
    返回 [(ts, ratio)]；任一 counter 缺失返回 None，调用方再退回 gauge。"""
    hf = pick_prom_file(prom_dir, ["vllm_prefix_cache_hits_total"])
    qf = pick_prom_file(prom_dir, ["vllm_prefix_cache_queries_total"])
    if not (hf and qf):
        return None
    def vals(f):
        s = json.load(open(f))
        return dict((float(ts), float(v)) for ts, v in s[0]["values"]) if s else {}
    h, q = vals(hf), vals(qf)
    ts_list = sorted(set(h) & set(q))
    out = []
    for a, b in zip(ts_list, ts_list[1:]):
        dq = q[b] - q[a]
        if dq > 0:
            out.append([b, round(max(0.0, h[b] - h[a]) / dq, 4)])
    return out or None


def load_dmon(path):
    """nvidia-smi dmon -s pucvmet -o DT：# 开头是注释，数据列依次为
    Date Time pwr gtemp mtemp sm mem enc dec jpg ofa mclk pclk ... fb ..."""
    rows, cols = [], None
    for line in open(path):
        if line.startswith("#"):
            if "pwr" in line:
                cols = line.lstrip("#").split()
            continue
        parts = line.split()
        if cols and len(parts) == len(cols):
            rows.append(dict(zip(cols, parts)))
    if not rows:
        return None
    import time as _t
    out = {"ts": [], "pwr": [], "sm": [], "pclk": [], "fb": [], "gtemp": [], "mem": []}
    for r in rows:
        try:
            d = r["Date"]
            dfmt = "%Y%m%d" if len(d) == 8 and d.isdigit() else ("%Y/%m/%d" if "/" in d else "%Y-%m-%d")
            tfmt = "%H:%M:%S.%f" if "." in r["Time"] else "%H:%M:%S"
            ts = _t.mktime(_t.strptime(d + " " + r["Time"], dfmt + " " + tfmt))
        except (KeyError, ValueError):
            continue
        out["ts"].append(ts)
        for k in ("pwr", "sm", "pclk", "fb", "gtemp", "mem"):
            out[k].append(float(r[k]) if r[k] != "-" else None)
    return out


def build_data(results_dir):
    client_dir = Path(results_dir) / "client"
    levels = {}
    for f in sorted(client_dir.glob("level_*_run*.json")):
        m = re.match(r"level_(\d+)_run(\d)", f.name)
        lv = load_level(f)
        levels.setdefault(int(m.group(1)), {})[int(m.group(2))] = lv
    phenomena = {}
    for key in ("a", "b", "c"):
        f = client_dir / f"phenomenon_{key}.json"
        if f.exists():
            phenomena[key] = load_level(f)

    # 排队时间：优先 prometheus 直方图重建，退回 client 头部字段
    prom_dir = Path(results_dir) / "prometheus"
    ns = sorted(levels)
    windows = [(levels[n][1]["t0"], levels[n][1]["t1"]) for n in ns]
    pq = th = eh = st = None
    if prom_dir.exists():
        qh = load_prom_hist(prom_dir, ["vllm_request_queue_time_seconds_bucket"],
                            "queue_time_seconds")
        pq = hist_window_quantiles(qh, windows) if qh else None
        hh = load_prom_hist(prom_dir, ["vllm_time_to_first_token_seconds_bucket"],
                            "time_to_first_token")
        th = hist_window_quantiles(hh, windows) if hh else None
        ehh = load_prom_hist(prom_dir, ["vllm_e2e_request_latency_seconds_bucket"],
                             "e2e_request_latency")
        eh = hist_window_quantiles(ehh, windows) if ehh else None
        st = load_prom_counter_rate(prom_dir, ["vllm_generation_tokens_total"],
                                    "generation_tokens_total", windows)

    def rnd(xs):
        return [round(x, 1) if x is not None else None for x in xs]

    for idx, n in enumerate(ns):
        r1 = levels[n][1]
        for run in levels[n].values():
            if pq and pq[idx][0] is not None:
                run["queue_p50"], run["queue_p99"] = pq[idx][0], pq[idx][1]
        if th:
            r1["srv_ttft"] = rnd(th[idx])
        if eh:
            r1["srv_e2e"] = rnd(eh[idx])
        if st:
            r1["srv_tps"] = st[idx]

    # 现象段同样重建服务端指标：A/C 各自的服务窗口独立，计数器随重启清零，
    # window_delta 取窗口内最大值，不会读到下一台服务的零值
    if prom_dir.exists():
        for ph in phenomena.values():
            if not ph.get("t0"):
                continue
            w = [(ph["t0"], ph["t1"])]
            if qh:
                q = hist_window_quantiles(qh, w)[0]
                if q[0] is not None:
                    ph["queue_p50"], ph["queue_p99"] = q
            if hh:
                ph["srv_ttft"] = rnd(hist_window_quantiles(hh, w)[0])
            rate = load_prom_counter_rate(prom_dir, ["vllm_generation_tokens_total"],
                                          "generation_tokens_total", w)
            if rate:
                ph["srv_tps"] = rate[0]

    meta = json.load(open(Path(results_dir) / "meta.json")) if (Path(results_dir) / "meta.json").exists() else {}

    # 耗时构成：排队用服务端值，Prefill≈TTFT-排队，Decode≈总耗时-TTFT。
    # 三段恒等于总耗时，不设残差段（残差数学上恒为 0）。
    breakdown, breakdown_ms = [], []
    for n in ns:
        r = levels[n][1]
        q = r["queue_p50"] or 0
        pre = max((r["ttft_p50"] or 0) - q, 0)
        dec = max((r["total_p50"] or 0) - (r["ttft_p50"] or 0), 0)
        tot = q + pre + dec or 1
        breakdown.append([round(x / tot * 100, 1) for x in (q, pre, dec)])
        breakdown_ms.append([round(x, 1) for x in (q, pre, dec)])

    # 经济并发点：达到最大吞吐 90% 的最小档位
    tps = {n: levels[n][1]["throughput"] for n in ns}
    mx = max(tps.values())
    econ = next(n for n in ns if tps[n] >= 0.9 * mx)

    timeline = {"ts": [], "kv": [], "preempt": [], "dmon": None, "windows": [],
                "running": None, "waiting": None, "hit": None}
    kv = load_prom_gauge(prom_dir, ["vllm_kv_cache_usage_perc", "vllm_gpu_cache_usage_perc",
                                    "vllm_gpu_cache_usage_gauge"]) if prom_dir.exists() else None
    pre = load_prom_gauge(prom_dir, ["vllm_num_preemptions_total"],
                          "num_preemptions") if prom_dir.exists() else None
    if kv:
        timeline["ts"] = [t for t, _ in kv]
        timeline["kv"] = [round(v * 100, 1) for _, v in kv]
    if pre:
        # 服务重启计数器清零：按段累加偏移，时间轴画全程累计而非单台服务计数
        off, prev, cum = 0.0, None, []
        for t, v in pre:
            if prev is not None and v < prev:
                off += prev
            cum.append([t, v + off])
            prev = v
        timeline["preempt"] = cum
    if prom_dir.exists():
        for key, names in (("running", ["vllm_num_requests_running",
                                        "vllm_num_requests_running_gauge"]),
                           ("waiting", ["vllm_num_requests_waiting",
                                        "vllm_num_requests_waiting_gauge"])):
            g = load_prom_gauge(prom_dir, names)
            if g:
                timeline[key] = [[t, v] for t, v in g]
        hit = load_prom_hit_ratio(prom_dir)
        if hit is None:
            g = load_prom_gauge(prom_dir, ["vllm_prefix_cache_hit_rate_gauge"],
                                "prefix_cache_hit")
            hit = [[t, v] for t, v in g] if g else None
        if hit:
            timeline["hit"] = hit
    dmon_path = Path(results_dir) / "dmon.csv"
    if dmon_path.exists():
        timeline["dmon"] = load_dmon(dmon_path)
    for name, seg in (("扫描", (windows[0][0], windows[-1][1])) if ns else (None, None),
                      ("现象B", (phenomena["b"]["t0"], phenomena["b"]["t1"]) if "b" in phenomena else (None, None)),
                      ("现象A", (phenomena["a"]["t0"], phenomena["a"]["t1"]) if "a" in phenomena else (None, None)),
                      ("现象C", (phenomena["c"]["t0"], phenomena["c"]["t1"]) if "c" in phenomena else (None, None))):
        if seg[0]:
            timeline["windows"].append({"name": name, "start": seg[0], "end": seg[1]})

    # 现象 A 窗口内的抢占增量
    if "a" in phenomena and pre:
        phenomena["a"]["preempt_delta"] = round(window_delta(
            pre, phenomena["a"]["t0"], phenomena["a"]["t1"]), 0)

    # 数据质量：逐请求 output_tokens 对 spec 计截断，ttft 为空计失败
    spec_out = levels[ns[0]][1].get("output_len") if ns else None
    quality = {"total": 0, "truncated": 0, "failed": 0}
    for datasets in list(levels.values()) + [{1: p} for p in phenomena.values()]:
        for run in datasets.values():
            for r in run["reqs"]:
                quality["total"] += 1
                if r["ttft"] is None:
                    quality["failed"] += 1
                if spec_out and r["out"] is not None and r["out"] != spec_out:
                    quality["truncated"] += 1

    first = levels[ns[0]][1] if ns else {}
    run2_keys = ("throughput", "ttft_p50", "ttft_p99", "itl_p50", "itl_p99", "total_p50")
    return {
        "meta": meta,
        "memory": meta.get("memory_breakdown"),
        "spec": {k: first.get(k) for k in ("input_len", "output_len", "seed", "num_requests")},
        "quality": quality,
        "levels": [{"level": n, **{k: v for k, v in levels[n][1].items() if k != "reqs"},
                    "run2": {k: levels[n][2].get(k) for k in run2_keys} if 2 in levels[n] else None}
                   for n in ns],
        "drill": {str(n): levels[n][1]["reqs"] for n in ns},
        "breakdown": breakdown,
        "breakdown_ms": breakdown_ms,
        "econ": econ,
        "timeline": timeline,
        "phenomenon_a": phenomena.get("a"),
        "phenomenon_b": phenomena.get("b"),
        "phenomenon_c": phenomena.get("c"),
    }


def make_demo(out_dir):
    """合成一份符合产物契约的数据，只用于验证呈现，数值无实测含义。"""
    import random
    rng = random.Random(7)
    out = Path(out_dir)
    (out / "client").mkdir(parents=True, exist_ok=True)
    (out / "prometheus").mkdir(exist_ok=True)
    base = time_module()
    levels = [1, 2, 4, 8, 16, 32]
    tp = {1: 46, 2: 88, 4: 160, 8: 229, 16: 251, 32: 258}
    ttft = {1: 120, 2: 175, 4: 300, 8: 520, 16: 1150, 32: 3600}
    qp = {1: 15, 2: 25, 4: 45, 8: 90, 16: 300, 32: 2700}
    itl = {1: 22, 2: 24, 4: 28, 8: 35, 16: 52, 32: 78}
    cursor = base + 60
    wins = {}
    for n in levels:
        s0 = cursor
        for run in (1, 2):
            wall = 40 * (512 + 128) * 0.004 / max(n, 1) + 8
            recs = []
            for i in range(40):
                st = cursor + i * wall / 40
                recs.append({"request_id": i, "start_ts": st,
                             "ttft_ms": round(ttft[n] * rng.uniform(0.85, 1.5), 1),
                             "itl_ms": [round(itl[n] * rng.uniform(0.7, 1.6), 1) for _ in range(127)],
                             "output_tokens": 128,
                             "total_ms": round(ttft[n] + 127 * itl[n] * rng.uniform(0.9, 1.2), 1)})
            (out / "client" / f"level_{n}_run{run}.json").write_text(json.dumps({
                "concurrency": n, "num_requests": 40, "input_len": 512, "output_len": 128,
                "seed": 42, "wall_seconds": round(wall, 2),
                "throughput_tokens_per_s": round(tp[n] * rng.uniform(0.96, 1.04), 1),
                "queue_p50_ms": round(qp[n] * rng.uniform(0.9, 1.1), 1),
                "queue_p99_ms": round(qp[n] * 2.3, 1), "requests": recs}))
        wins[n] = (s0, s0 + 2 * wall)
        cursor += 2 * wall + 20
    b_start = cursor
    b_recs = [{"request_id": i, "start_ts": cursor + i * 0.9,
               "ttft_ms": round(1150 * (1 + (i > 120) * 0.18) * rng.uniform(0.85, 1.4), 1),
               "itl_ms": [round(52 * (1 + (i > 120) * 0.22) * rng.uniform(0.7, 1.5), 1) for _ in range(127)],
               "output_tokens": 128, "total_ms": round(1150 + 127 * 52 * (1 + (i > 120) * 0.22), 1)}
              for i in range(200)]
    (out / "client" / "phenomenon_b.json").write_text(json.dumps({
        "concurrency": 16, "num_requests": 200, "input_len": 512, "output_len": 128, "seed": 42,
        "wall_seconds": 180, "throughput_tokens_per_s": 246,
        "queue_p50_ms": 310, "queue_p99_ms": 720, "requests": b_recs}))
    cursor += 200
    a_start = cursor
    a_recs = [{"request_id": i, "start_ts": cursor + i * 1.4,
               "ttft_ms": round(5200 * rng.uniform(0.8, 1.6), 1),
               "itl_ms": [round(95 * rng.uniform(0.7, 1.8), 1) for _ in range(127)],
               "output_tokens": 128, "total_ms": round(5200 + 127 * 95, 1)} for i in range(40)]
    (out / "client" / "phenomenon_a.json").write_text(json.dumps({
        "concurrency": 32, "num_requests": 40, "input_len": 512, "output_len": 128, "seed": 42,
        "wall_seconds": 90, "throughput_tokens_per_s": 118,
        "queue_p50_ms": 3900, "queue_p99_ms": 6100, "requests": a_recs}))
    cursor += 110
    c_start = cursor
    c_recs = [{"request_id": i, "start_ts": cursor + i * 0.9,
               "ttft_ms": round(470 * rng.uniform(0.85, 1.4), 1),
               "itl_ms": [round(29 * rng.uniform(0.7, 1.5), 1) for _ in range(127)],
               "output_tokens": 128, "total_ms": round(470 + 127 * 29 * rng.uniform(0.9, 1.15), 1)}
              for i in range(40)]
    (out / "client" / "phenomenon_c.json").write_text(json.dumps({
        "concurrency": 8, "num_requests": 40, "input_len": 512, "output_len": 128, "seed": 42,
        "wall_seconds": 42, "throughput_tokens_per_s": 296,
        "queue_p50_ms": 70, "queue_p99_ms": 160, "requests": c_recs}))

    t_end = cursor + 60
    step = (t_end - base) / 400
    tss = [base + i * step for i in range(400)]

    # 服务端指标：按同一时间窗合成直方图/计数器/gauge，验证交叉验证链路
    def done_cnt(ts):
        c = 0.0
        for n, (s, e) in wins.items():
            c += 80 * min(max((ts - s) / max(e - s, 1e-9), 0.0), 1.0)
        c += 200 * min(max((ts - b_start) / 180.0, 0.0), 1.0)
        c += 40 * min(max((ts - a_start) / 90.0, 0.0), 1.0)
        c += 40 * min(max((ts - c_start) / 42.0, 0.0), 1.0)
        return c

    def med_at(ts, table):
        for n in sorted(wins):
            if ts <= wins[n][1]:
                return table[n]
        if ts <= b_start + 180:
            return table["b"]
        if ts <= a_start + 90:
            return table["a"]
        return table["c"]

    les = [0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2, 3, 5, 8, 15, 30]

    def write_hist(name, table):
        series = []
        for le in les + ["+Inf"]:
            vals = []
            for ts in tss:
                med = med_at(ts, table)
                frac = 1.0 if le == "+Inf" else min(max((le - 0.8 * med) / (0.8 * med), 0.0), 1.0)
                vals.append([ts, str(round(done_cnt(ts) * frac, 3))])
            m = {"__name__": name}
            if le != "+Inf":
                m["le"] = str(le)
            series.append({"metric": m, "values": vals})
        (out / "prometheus" / f"{name.replace(':', '_')}_bucket.json").write_text(json.dumps(series))

    write_hist("vllm:request_queue_time_seconds",
               {n: qp[n] / 1000 for n in levels} | {"b": 0.31, "a": 3.9, "c": 0.07})
    write_hist("vllm:time_to_first_token_seconds",
               {n: ttft[n] / 1000 for n in levels} | {"b": 1.15, "a": 5.2, "c": 0.47})
    write_hist("vllm:e2e_request_latency_seconds",
               {n: (ttft[n] + 127 * itl[n]) / 1000 for n in levels} | {"b": 7.8, "a": 17.2, "c": 4.2})

    gen = [{"metric": {"__name__": "vllm:generation_tokens_total"},
            "values": [[ts, str(round(done_cnt(ts) * 128, 1))] for ts in tss]}]
    (out / "prometheus" / "vllm_generation_tokens_total.json").write_text(json.dumps(gen))

    def gauge(name, fn):
        vals = [[ts, str(fn(ts))] for ts in tss]
        (out / "prometheus" / f"{name.replace(':', '_')}.json").write_text(
            json.dumps([{"metric": {"__name__": name}, "values": vals}]))

    def conc_at(ts):
        for n in sorted(wins):
            if wins[n][0] <= ts <= wins[n][1]:
                return n
        if b_start <= ts <= b_start + 180:
            return 16
        if a_start <= ts <= a_start + 90:
            return 32
        if ts >= c_start:
            return 8
        return 0

    wait_map = {1: 0, 2: 0, 4: 0, 8: 1, 16: 3, 32: 9}
    gauge("vllm:num_requests_running_gauge", conc_at)
    gauge("vllm:num_requests_waiting_gauge",
          lambda ts: wait_map.get(conc_at(ts), 0) + (4 if b_start <= ts < b_start + 180 else 0)
                     + (12 if a_start <= ts < a_start + 90 else 0))
    gauge("vllm:prefix_cache_hit_rate_gauge", lambda ts: round(rng.uniform(0.0, 0.02), 3))

    # dmon：扫描段平稳，B 段后半 pclk 掉 12%，A 段利用率满
    dmon = out / "dmon.csv"
    lines = ["# Date Time gpu pwr gtemp mtemp sm mem enc dec jpg ofa mclk pclk pviol tviol fb bar1 ccpm sbecc dbecc pci rxpci txpci"]
    for i in range(400):
        ts = base + i * step
        phase = ("B" if b_start <= ts < b_start + 180 else
                 "A" if a_start <= ts < a_start + 90 else
                 "C" if ts >= c_start else "S")
        pclk = 1890 if phase != "B" or i % 400 < 330 else 1660
        sm = 99 if phase == "A" else rng.randint(88, 97)
        pwr = 320 if phase == "A" else (285 if phase == "B" else rng.randint(240, 300))
        gtemp = int(52 + pwr / 12 + rng.uniform(-1, 1))
        fb = 21650 if phase == "A" else (21500 if phase == "B" else (18900 if phase == "C" else rng.randint(21200, 21400)))
        lines.append(f"2026/11/11 {time_module_str(ts)} 0 {pwr} {gtemp} - {sm} {sm} 0 0 0 0 8001 {pclk} 0 0 {fb} 2 0 - - 0 1 0")
    dmon.write_text("\n".join(lines) + "\n")

    # KV 占用与抢占（prometheus 格式）
    kv_series = [{"metric": {"__name__": "vllm:gpu_cache_usage_gauge"},
                  "values": [[base + i * step, str(round(min(0.98, 0.15 + 0.02 * (i / 400) * 6), 2))]
                             for i in range(400)]}]
    (out / "prometheus" / "vllm_gpu_cache_usage_gauge.json").write_text(json.dumps(kv_series))
    pre_series = [{"metric": {"__name__": "vllm:num_preemptions_total"},
                   "values": [[base + i * step, str(max(0, (i - 350) // 2))] for i in range(400)]}]
    (out / "prometheus" / "vllm_num_preemptions_total.json").write_text(json.dumps(pre_series))
    (out / "meta.json").write_text(json.dumps({
        "gpu": "RTX 4090（演示数据）", "driver": "550.90.07", "cuda": "12.4",
        "vllm": "0.x.x", "model": "Qwen2.5-7B-Instruct", "start_ts": base, "end_ts": t_end,
        "gpu_mem_total_gb": 24,
        "idle_gpu_mem_used_mb": 320, "cuda_context_overhead_mb": 21800,
        "kv_cache_info": "GPU KV cache size: 21,504 tokens",
        "phenom_a_kv_cache_info": "GPU KV cache size: 10,240 tokens",
        "phenom_c_kv_cache_info": "GPU KV cache size: 62,300 tokens",
        "phenom_c_kernel_lines": ["(EngineCore pid=1234) Using Marlin kernel for AWQ quantization."],
        "memory_breakdown": {"模型权重": 14.2, "KV 块池": 5.4,
                             "激活与上下文碎片": 1.7}}))
    print(f"[demo] 演示数据写入 {out}（数值为合成，仅用于验证呈现设计）")


def time_module():
    import time
    return time.time()


def time_module_str(ts):
    import time
    return time.strftime("%H:%M:%S.0", time.localtime(ts))


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>vLLM 并发性能实测报告</title>
<script>__ECHARTS__</script>
<style>
:root{--bg:#f6f7f9;--ink:#1f2328;--mut:#5a616e;--line:#e3e6eb;--grid:#eceff3;
      --c1:#3b6fd4;--c2:#7a5cd6;--c3:#e8963c;--c4:#3aa6a6;--c5:#c95f81}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",sans-serif}
nav{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:18px;padding:11px 36px;
    background:rgba(255,255,255,.92);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);flex-wrap:wrap}
nav .brand{font-size:13px;font-weight:700;color:var(--ink)}
nav a{color:var(--mut);text-decoration:none;font-size:12.5px}
nav a:hover{color:var(--c1)}
#progress{position:absolute;left:0;bottom:-1px;height:2px;width:0;background:var(--c1)}
header{padding:30px 36px 4px}
header h1{margin:0;font-size:23px;font-weight:700}
header .sub{margin-top:8px;font-size:12.5px;color:var(--mut)}
.badge{display:inline-block;background:var(--c3);color:#fff;font-weight:700;
  padding:2px 10px;border-radius:4px;margin-left:10px;font-size:12px;vertical-align:middle}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;padding:20px 36px 0}
.kpi{position:relative;background:#fff;border:1px solid var(--line);border-radius:8px;
  padding:14px 16px 12px;box-shadow:0 1px 3px rgba(30,40,90,.05)}
.kpi::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;border-radius:8px 0 0 8px;background:var(--c1)}
.kpi:nth-child(2)::before{background:var(--c2)}
.kpi:nth-child(3)::before{background:var(--c3)}
.kpi:nth-child(4)::before{background:var(--c4)}
.kpi:nth-child(5)::before{background:var(--c5)}
.kpi .v{font-size:25px;font-weight:700;color:var(--ink);font-variant-numeric:tabular-nums}
.kpi .l{font-size:12px;color:var(--mut);margin-top:5px;line-height:1.45}
#quality{margin:12px 36px 0;font-size:12.5px;color:var(--mut)}
#quality b{color:var(--c4)}
#quality b.bad{color:var(--c5)}
.card{background:#fff;border:1px solid var(--line);border-radius:8px;margin:20px 36px;
  padding:16px 20px;box-shadow:0 1px 3px rgba(30,40,90,.05)}
.card h2{font-size:15.5px;margin:0 0 4px;font-weight:650}
.card p.note{font-size:12.5px;color:var(--mut);margin:0 0 10px;line-height:1.6}
.chart{width:100%;height:380px}
.chips{display:flex;gap:10px;flex-wrap:wrap;margin:6px 0 10px}
.chip{background:#f2f5fb;border:1px solid var(--line);border-radius:6px;padding:5px 12px;
  font-size:12.5px;color:var(--mut)}
.chip b{color:var(--ink);font-variant-numeric:tabular-nums}
.chip.warn b{color:var(--c5)}
.tgl{display:inline-flex;gap:0;margin-left:12px;vertical-align:middle}
.tgl button{border:1px solid var(--line);background:#fff;color:var(--mut);font-size:11.5px;
  padding:2px 10px;cursor:pointer}
.tgl button:first-child{border-radius:4px 0 0 4px}
.tgl button:last-child{border-radius:0 4px 4px 0;border-left:none}
.tgl button.on{background:var(--c1);border-color:var(--c1);color:#fff}
.csvbtn{border:1px solid var(--line);background:#fff;color:var(--c1);font-size:11.5px;
  padding:2px 10px;border-radius:4px;cursor:pointer;margin-left:10px}
.csvbtn:hover{border-color:var(--c1)}
table.dt{border-collapse:collapse;font-size:12.5px;width:100%;font-variant-numeric:tabular-nums}
table.dt th{color:var(--mut);font-weight:600;text-align:right;padding:6px 10px;border-bottom:1.5px solid var(--line)}
table.dt th:first-child,table.dt td:first-child{text-align:left}
table.dt td{padding:5px 10px;text-align:right;border-bottom:1px solid var(--grid)}
table.dt tr:hover td{background:#f6f8fc}
details{margin-top:10px;font-size:12.5px;color:var(--mut)}
details summary{cursor:pointer;color:var(--c1)}
details pre{background:#f6f7f9;border:1px solid var(--line);border-radius:6px;padding:10px;
  overflow:auto;max-height:320px;font-size:11.5px;color:var(--ink)}
#memwrap{display:flex;gap:18px}
#mem{flex:1;height:320px}
#memlegend{flex:0 0 260px;font-size:12.5px;color:var(--mut);align-self:center}
#memlegend div{display:flex;align-items:center;padding:7px 2px;border-bottom:1px dashed var(--line)}
#memlegend b{margin-left:auto;color:var(--ink);font-variant-numeric:tabular-nums}
.cmini{flex:1;height:220px;min-width:220px}
#cmiwrap{display:flex;gap:14px;flex-wrap:wrap}
footer{padding:16px 36px 24px;font-size:12px;color:var(--mut)}
@media (max-width:1100px){.kpis{grid-template-columns:repeat(2,1fr)}#memwrap{flex-direction:column}}
</style></head><body>
<nav><span class="brand">vLLM BENCH</span>
<a href="#sec-main">吞吐与延迟</a><a href="#sec-breakdown">耗时构成</a>
<a href="#sec-a">现象A</a><a href="#sec-b">现象B</a><a href="#sec-c">现象C·AWQ</a>
<a href="#sec-timeline">时间轴</a><a href="#sec-server">服务端指标</a><a href="#sec-cross">交叉验证</a>
<a href="#sec-mem">显存占比</a><a href="#sec-dist">延迟分布</a><a href="#sec-drill">逐请求明细</a><a href="#sec-tables">数据表</a>
<div id="progress"></div></nav>
<header><h1>单卡 <span id="gpucard">GPU</span> · vLLM 并发性能实测<span id="demobadge"></span></h1>
<div class="sub" id="metaline"></div></header>
<div class="kpis" id="kpis"></div>
<div id="quality"></div>

<div class="card" id="sec-main"><h2>吞吐与首 token 延迟
<span class="tgl" id="ptgl"><button data-p="50">P50</button><button data-p="90">P90</button><button data-p="99" class="on">P99</button></span>
<span class="tgl" id="maintgl"><button data-v="value" class="on">线性</button><button data-v="log">对数</button></span></h2>
<p class="note">柱=输出吞吐（左轴，run1/run2），线=TTFT 分位数（右轴，可切 P50/P90/P99 与对数刻度）。紫色虚线=经济并发点，标记=吞吐峰值。<b>点击任一档位</b>选中：其余档位淡出，耗时构成图与时间轴同步标注该档，延迟分布与明细面板切到该档；再点一次取消。</p>
<div id="main" class="chart"></div></div>

<div class="card" id="sec-breakdown"><h2>单请求耗时构成
<span class="tgl" id="bdtgl"><button data-m="pct" class="on">占比 %</button><button data-m="ms">绝对 ms</button></span></h2>
<p class="note">每档一根堆叠柱：排队=服务端调度等待；Prefill≈TTFT-排队；Decode≈总耗时-TTFT。三段恒等于总耗时。点击任意柱段与主图共用同一档位选择。</p>
<div id="breakdown" class="chart" style="height:340px"></div></div>

<div class="card" id="sec-a"><h2>现象 A：限制 KV 池容量触发抢占<button class="csvbtn" id="acsv">下载 CSV</button></h2>
<div class="chips" id="achips"></div>
<p class="note">上：逐请求平均 ITL；下：逐请求 TTFT。KV 池只够约一半并发请求，其余请求靠抢占换出与重算推进，TTFT 尾部升高。</p>
<div id="achart" class="chart" style="height:420px"></div>
<details><summary>逐请求原始表（<span id="an"></span> 条）</summary><div id="atable" style="max-height:320px;overflow:auto"></div></details></div>

<div class="card" id="sec-b"><h2>现象 B：长时运行漂移（<span id="btitle"></span>）<button class="csvbtn" id="bcsv">下载 CSV</button></h2>
<div class="chips" id="bchips"></div>
<p class="note">上：逐请求平均 ITL；下：逐请求 TTFT，x 为请求序号。虚线=前半/后半均值，两条虚线的高度差即漂移幅度。逐请求原始表折叠在底部。</p>
<div id="bchart" class="chart" style="height:420px"></div>
<details><summary>逐请求原始表（<span id="bn"></span> 条）</summary><div id="btable" style="max-height:320px;overflow:auto"></div></details></div>

<div class="card" id="sec-c"><h2>现象 C：FP16 与 AWQ 同并发对比<button class="csvbtn" id="ccsv">下载 CSV</button></h2>
<div class="chips" id="cchips"></div>
<p class="note">同一并发档、同一输入输出规格。AWQ 4-bit 权重大小减半以上，decode 阶段访存量相应减半；是否启用 Marlin 内核见下方日志原文。</p>
<div id="cmiwrap"><div id="cm1" class="cmini"></div><div id="cm2" class="cmini"></div><div id="cm3" class="cmini"></div></div>
<div id="ctable" style="margin-top:12px"></div>
<details open><summary>量化 kernel 日志行（vllm_phenomC.log）</summary><pre id="ckernel"></pre></details></div>

<div class="card" id="sec-timeline"><h2>运行全程指标：KV 占用 / 抢占 / SM 频率 / 功耗 / 利用率</h2>
<p class="note">上：KV 块占用与累计抢占次数；下：SM 频率、功耗与 SM/显存带宽利用率。<b>按住左键拖动可平移时间窗，滚轮缩放，底部滑块也可拖动</b>；色块标注扫描段与现象 B/A/C 段，紫线为当前选中档位的时间窗。</p>
<div id="timeline" class="chart" style="height:460px"></div></div>

<div class="card" id="sec-server"><h2>服务端指标：并发、显存与温度</h2>
<p class="note">上：服务端统计的运行中/等待中请求数（左轴）与前缀缓存命中率（右轴）；下：nvidia-smi 的显存实际占用与 GPU 温度。与运行时间轴同窗缩放。前缀命中率长期接近 0 时，Prefill 测量不受缓存影响。</p>
<div id="srv" class="chart" style="height:420px"></div></div>

<div class="card" id="sec-cross"><h2>吞吐交叉验证：客户端统计与服务端计数器</h2>
<p class="note">蓝柱=客户端统计的输出吞吐；青柱=对 vllm:generation_tokens_total 按档位时间窗差分。两柱偏差持续大于 5% 时，token 计数口径存在误差。点击档位同样淡出。</p>
<div id="cross" class="chart" style="height:300px"></div></div>

<div class="card" id="sec-mem"><h2 id="memtitle">显存占比</h2>
<p class="note" id="memnote">权重=模型参数常驻占用；KV 块池=gpu_memory_utilization 预算扣除权重与激活后的余量；激活与上下文碎片为服务启动期固定开销。数字由 run_bench 从服务日志与 nvidia-smi 基线自动采集。</p>
<div id="memwrap"><div id="mem"></div><div id="memlegend"></div></div>
<details><summary>完整 meta.json（版本锁 / 基线 / 配置）</summary><pre id="metapre"></pre></details></div>

<div class="card" id="sec-dist"><h2>延迟分布（ECDF）
<span class="tgl" id="ecfgltgl"><button data-v="log" class="on">对数</button><button data-v="value">线性</button></span></h2>
<p class="note">每档一条累积分布曲线：x=延迟，y=不超过该延迟的请求（或 token）占比。P50 读 y=0.5 对应 x，P99 读 y=0.99。曲线越靠左越快，越陡越集中。点击主图档位可高亮对应曲线。</p>
<div id="ecdf_ttft" class="chart" style="height:340px"></div>
<div id="ecdf_itl" class="chart" style="height:300px"></div></div>

<div class="card" id="sec-drill"><h2><span id="drilltitle">逐请求明细</span>
<button class="csvbtn" id="drillcsv">下载 CSV</button></h2>
<p class="note">每行一条请求，按请求编号排列。<span style="color:#e8a33d">■</span> 橙色段=TTFT（到达至首 token，含排队与 Prefill），<span style="color:#3b6fd6">■</span> 蓝色段=Decode（首 token 至完成）。横轴为该档开始后的秒数，同一时刻多条色条重叠即并发在跑；橙色段越长说明首 token 越慢。点击主图档位切换，下方为该档原始表。</p>
<div id="drill2" class="chart" style="height:600px"></div>
<div id="reqtable" style="margin-top:10px;max-height:320px;overflow:auto"></div></div>

<div class="card" id="sec-tables"><h2>数据表<button class="csvbtn" id="lvscsv">档位总表 CSV</button><button class="csvbtn" id="runscsv">双跑对比 CSV</button></h2>
<p class="note">上：档位总表（run1 全部指标）。下：run1/run2 双跑对比，用于检查重复性。服务端列来自 Prometheus 重建，缺失显示 -。</p>
<div id="lvtable" style="overflow:auto"></div>
<h2 style="margin-top:18px">双跑对比</h2>
<div id="runtable" style="overflow:auto"></div></div>

<footer>report.html 由 scripts/plot.py 生成 · 数据内嵌 · ECharts 5.5.1</footer>
<script>
const D = __DATA__;
const fmt = n => n==null ? "-" : (Math.abs(n)>=1000? n.toFixed(0): n.toFixed(1));
const demo = (D.meta.gpu||"").includes("演示");
if (demo) document.getElementById("demobadge").innerHTML='<span class="badge">演示数据</span>';
document.getElementById("gpucard").textContent =
  (D.meta.gpu||"").replace(/[（(][^）)]*[)）]/g,"").replace(/^NVIDIA\\s+/,"").trim() || "GPU";
if (D.meta.gpu_mem_total_gb)
  document.getElementById("memtitle").textContent = "显存占比（共 "+D.meta.gpu_mem_total_gb+" GB）";
document.getElementById("metaline").textContent =
  [D.meta.gpu, "驱动 "+D.meta.driver, "CUDA "+D.meta.cuda, "vLLM "+D.meta.vllm, D.meta.model,
   D.spec&&D.spec.input_len?`输入 ${D.spec.input_len} / 输出 ${D.spec.output_len} tok · ${D.spec.num_requests} 条/档 · seed ${D.spec.seed}`:""]
  .filter(Boolean).join(" · ");
document.getElementById("metapre").textContent = JSON.stringify(D.meta,null,1);

// 顶部滚动进度条
const bar=document.getElementById("progress");
addEventListener("scroll",()=>{const h=document.documentElement;
  bar.style.width=(h.scrollTop/(h.scrollHeight-h.clientHeight)*100)+"%";},{passive:true});

const lv = D.levels.map(r=>r.level), tp = D.levels.map(r=>r.throughput),
      econ = D.econ, mx = Math.max(...tp);
const i8 = lv.indexOf(8);
const gain = (tp[lv.length-1]/tp[i8===-1?lv.length-2:i8]-1)*100;
const qShare = D.levels.map(r=>r.queue_p50&&r.ttft_p50? r.queue_p50/r.ttft_p50*100 : null);
const preMax = D.timeline.preempt&&D.timeline.preempt.length? D.timeline.preempt[D.timeline.preempt.length-1][1] : 0;

document.getElementById("kpis").innerHTML = [
 {v:econ, d:0, u:"", l:"经济并发点 · 达到峰值吞吐 90% 的最小并发"},
 {v:mx, d:1, u:" tok/s", l:"吞吐上限 · 并发 "+lv[tp.indexOf(mx)]},
 {v:gain, d:1, u:"%", sign:true, l:"最高档相对 8 并发的吞吐增益"},
 {v:(qShare[qShare.length-1]||0), d:1, u:"%", l:"最高档 TTFT P50 中排队占比"},
 {v:preMax, d:0, u:"", l:"全程累计抢占次数"}
].map(k=>`<div class="kpi"><div class="v">${(k.sign&&k.v>0?"+":"")+k.v.toFixed(k.d)+k.u}</div><div class="l">${k.l}</div></div>`).join("");

const q=D.quality;
document.getElementById("quality").innerHTML =
  `数据质量：共 ${q.total} 条请求 · 截断 <b class="${q.truncated?'bad':''}">${q.truncated}</b> · 失败 <b class="${q.failed?'bad':''}">${q.failed}</b>`+
  (q.truncated===0&&q.failed===0?" · 全部请求输出完整，无失败":"");

// 学术简约主题公共件
const AXC={axisLine:{lineStyle:{color:"#c8cdd6"}},axisTick:{show:false},
  axisLabel:{color:"#5a616e"},splitLine:{lineStyle:{color:"#eceff3"}}};
const AXR={type:"value",splitLine:{show:false},axisLine:AXC.axisLine,
  axisTick:{show:false},axisLabel:AXC.axisLabel};
const NAM={nameTextStyle:{color:"#5a616e"}};
const TIP={backgroundColor:"#fff",borderColor:"#d6dae1",textStyle:{color:"#1f2328",fontSize:12},
  extraCssText:"box-shadow:0 4px 14px rgba(0,0,0,.08);border-radius:6px"};
const LEG={textStyle:{color:"#5a616e"}};
const TOOL={right:10,top:2,feature:{saveAsImage:{pixelRatio:2,backgroundColor:"#fff",title:"导出 PNG"}}};
const COLORS=["#3b6fd4","#7a5cd6","#e8963c","#3aa6a6","#c95f81","#8a8f9c"];

// CSV 下载（BOM 让 Excel 认中文）
function dlCSV(name,rows){
 const csv=rows.map(r=>r.join(",")).join("\\n");
 const a=document.createElement("a");
 a.href=URL.createObjectURL(new Blob(["\\ufeff"+csv],{type:"text/csv"}));
 a.download=name;a.click();URL.revokeObjectURL(a.href);
}
function tableHTML(head,rows){
 return `<table class="dt"><thead><tr>${head.map(h=>`<th>${h}</th>`).join("")}</tr></thead>`+
  `<tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${c==null?"-":c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}
const REQHEAD=["请求#","到达 s","TTFT ms","平均ITL ms","总耗时 ms","输出tok"];
const reqRow=r=>[r.i,r.st,r.ttft,r.itl_mean,r.tot,r.out];

// ── 主图 ──
let curP=99, mainLog=false;
const main=echarts.init(document.getElementById("main"));
const pkey=p=>"ttft_p"+p;
function mainLine(){return D.levels.map(r=>r[pkey(curP)]);}
function renderMain(){
main.setOption({
 tooltip:{...TIP,trigger:"axis",axisPointer:{type:"line",lineStyle:{color:"#c8cdd6"}},
  formatter:ps=>{
   const r=D.levels[ps[0].dataIndex];
   const row=(a,b)=>`<tr><td style="color:#5a616e;padding:2px 16px 2px 0">${a}</td>`+
     `<td style="text-align:right;font-variant-numeric:tabular-nums">${b}</td></tr>`;
   return `<div style="font-weight:700;margin-bottom:6px">并发 ${r.level}`+
     (r.level===econ?` <span style="color:#7a5cd6">· 经济点</span>`:"")+`</div>`+
     `<table style="border-collapse:collapse;font-size:12px">`+
     row("吞吐 run1",fmt(r.throughput)+" tok/s")+
     row("吞吐 run2",r.run2?fmt(r.run2.throughput)+" tok/s":"-")+
     row("TTFT P50 / P90 / P99",fmt(r.ttft_p50)+" / "+fmt(r.ttft_p90)+" / "+fmt(r.ttft_p99)+" ms")+
     row("排队 P50 / P99",fmt(r.queue_p50)+" / "+fmt(r.queue_p99)+" ms")+
     row("ITL P50 / P99",fmt(r.itl_p50)+" / "+fmt(r.itl_p99)+" ms")+
     row("总耗时 P50",fmt(r.total_p50)+" ms")+
     row("服务端 TTFT P50/P99",r.srv_ttft? fmt(r.srv_ttft[0])+" / "+fmt(r.srv_ttft[1])+" ms":"-")+
     row("服务端 e2e P50/P99",r.srv_e2e? fmt(r.srv_e2e[0])+" / "+fmt(r.srv_e2e[1])+" ms":"-")+
     row("服务端吞吐差分",r.srv_tps!=null? fmt(r.srv_tps)+" tok/s":"-")+
     row("墙钟",fmt(r.wall)+" s")+`</table>`;
  }},
 toolbox:TOOL,
 legend:{...LEG,data:["吞吐 run1","吞吐 run2","TTFT 分位","服务端 TTFT P99"]},
 grid:{left:70,right:80,top:44,bottom:40},
 xAxis:{type:"category",data:lv,name:"并发",...AXC,...NAM},
 yAxis:[{type:"value",name:"吞吐 tokens/s",...AXC,...NAM},
        {...AXR,type:mainLog?"log":"value",name:"TTFT P"+curP+" ms",position:"right",...NAM}],
 series:[
  {name:"吞吐 run1",type:"bar",data:tp,barWidth:"32%",
   itemStyle:{color:"#3b6fd4"},
   markLine:{symbol:["none","none"],data:econ!=null?[{xAxis:String(econ)}]:[],
     lineStyle:{color:"#7a5cd6",type:"dashed",width:1.5},
     label:{formatter:"经济点 "+econ,color:"#7a5cd6",position:"insideEndTop"}},
   markPoint:{symbol:"circle",symbolSize:1,data:[{type:"max"}],
     label:{show:true,position:"top",distance:6,formatter:p=>fmt(p.value)+" tok/s",
       color:"#1f2328",fontWeight:600},itemStyle:{color:"#3b6fd4"}}},
  {name:"吞吐 run2",type:"bar",data:D.levels.map(r=>r.run2&&r.run2.throughput),barWidth:"20%",
   itemStyle:{color:"#9b8ce4"}},
  {name:"TTFT 分位",type:"line",yAxisIndex:1,data:mainLine(),symbol:"emptyCircle",symbolSize:7,
   lineStyle:{width:2,color:"#e8963c"},itemStyle:{color:"#e8963c"}},
  {name:"服务端 TTFT P99",type:"line",yAxisIndex:1,data:D.levels.map(r=>r.srv_ttft&&r.srv_ttft[1]),
   symbol:"none",lineStyle:{width:1.6,type:"dashed",color:"#c95f81"},itemStyle:{color:"#c95f81"}}
 ]});}
renderMain();
document.getElementById("ptgl").addEventListener("click",e=>{
 const b=e.target.closest("button"); if(!b)return;
 curP=+b.dataset.p;
 [...e.currentTarget.children].forEach(x=>x.classList.toggle("on",x===b));
 renderMain(); applySel();
});
document.getElementById("maintgl").addEventListener("click",e=>{
 const b=e.target.closest("button"); if(!b)return;
 mainLog=b.dataset.v==="log";
 [...e.currentTarget.children].forEach(x=>x.classList.toggle("on",x===b));
 renderMain(); applySel();
});

// ── 耗时构成堆叠柱（三段，占比/绝对切换）──
let bdMode="pct";
const bd=echarts.init(document.getElementById("breakdown"));
function renderBd(){
 const pct=bdMode==="pct";
 bd.setOption({
  color:["#e8963c","#3b6fd4","#7a5cd6"],
  tooltip:{...TIP,trigger:"axis",axisPointer:{type:"shadow"},
   valueFormatter:v=>v+(pct?"%":" ms")},
  toolbox:TOOL,
  legend:LEG,
  grid:{left:60,right:30,top:44,bottom:40},
  xAxis:{type:"category",data:lv,name:"并发",...AXC,...NAM},
  yAxis:{type:"value",max:pct?100:null,name:pct?"占比 %":"ms",...AXC,...NAM},
  series:["排队","Prefill","Decode"].map((n,i)=>({
    name:n,type:"bar",stack:"t",barWidth:"55%",
    data:(pct?D.breakdown:D.breakdown_ms).map(r=>r[i]),
    label:{show:pct&&i===0,position:"insideBottom",color:"#fff",fontSize:11,
      formatter:p=>p.value>12?p.value+"%":""}}))
 });}
renderBd();
document.getElementById("bdtgl").addEventListener("click",e=>{
 const b=e.target.closest("button"); if(!b)return;
 bdMode=b.dataset.m;
 [...e.currentTarget.children].forEach(x=>x.classList.toggle("on",x===b));
 renderBd(); applySel();
});

// ── 现象 A/B 共用：逐请求双格散点 + 前后半均值线 ──
function halves(reqs,key){
 const h=Math.floor(reqs.length/2);
 const m=a=>{const v=a.map(r=>r[key]).filter(x=>x!=null);
   return v.length? v.reduce((x,y)=>x+y,0)/v.length : null;};
 return [m(reqs.slice(0,h)),m(reqs.slice(h))];
}
function phenChart(el,ph){
 const c=echarts.init(document.getElementById(el));
 const reqs=ph.reqs;
 const hi=halves(reqs,"itl_mean"), ht=halves(reqs,"ttft");
 const halfLine=(means,color)=>({symbol:["none","none"],silent:true,
   data:[{yAxis:means[0],lineStyle:{color,type:"dashed",width:1.2},
     label:{formatter:"前半 "+fmt(means[0]),color,fontSize:10,position:"insideStartTop"}},
    {yAxis:means[1],lineStyle:{color,type:"dashed",width:1.2},
     label:{formatter:"后半 "+fmt(means[1]),color,fontSize:10,position:"insideEndTop"}}]});
 c.setOption({
  color:["#7a5cd6","#e8963c"],
  tooltip:{...TIP,trigger:"item",formatter:p=>
    `请求 #${reqs[p.dataIndex].i}<br>${p.seriesName} ${fmt(p.data[1])} ms`},
  toolbox:TOOL,
  legend:{...LEG,data:["平均 ITL","TTFT"]},
  axisPointer:{link:[{xAxisIndex:"all"}]},
  grid:[{left:70,right:30,top:40,height:150},{left:70,right:30,top:250,height:130}],
  xAxis:[{type:"value",name:"请求序号",gridIndex:0,...AXC,...NAM},
         {type:"value",name:"请求序号",gridIndex:1,...AXC,...NAM}],
  yAxis:[{type:"value",name:"平均 ITL ms",gridIndex:0,scale:true,...AXC,...NAM},
         {type:"value",name:"TTFT ms",gridIndex:1,scale:true,...AXC,...NAM}],
  series:[
   {name:"平均 ITL",type:"scatter",symbolSize:6,xAxisIndex:0,yAxisIndex:0,
    data:reqs.map((r,i)=>[i,r.itl_mean]),itemStyle:{color:"#7a5cd6",opacity:.75},
    markLine:halfLine(hi,"#7a5cd6")},
   {name:"TTFT",type:"scatter",symbolSize:6,xAxisIndex:1,yAxisIndex:1,
    data:reqs.map((r,i)=>[i,r.ttft]),itemStyle:{color:"#e8963c",opacity:.75},
    markLine:halfLine(ht,"#e8963c")}
  ]});
 return c;
}
const charts=[];
let bChart=null,aChart=null;
if(D.phenomenon_b){
 const ph=D.phenomenon_b;
 document.getElementById("btitle").textContent=`并发 ${ph.level} · ${ph.reqs.length} 条`;
 document.getElementById("bn").textContent=ph.reqs.length;
 const hi=halves(ph.reqs,"itl_mean"), ht=halves(ph.reqs,"ttft");
 const drift=hi[0]?(hi[1]/hi[0]-1)*100:null;
 document.getElementById("bchips").innerHTML=[
  `<span class="chip">吞吐 <b>${fmt(ph.throughput)}</b> tok/s</span>`,
  `<span class="chip">前半 ITL <b>${fmt(hi[0])}</b> → 后半 <b>${fmt(hi[1])}</b> ms</span>`,
  `<span class="chip ${drift>5?'warn':''}">ITL 漂移 <b>${drift==null?"-":(drift>0?"+":"")+drift.toFixed(1)+"%"}</b></span>`,
  `<span class="chip">前半 TTFT <b>${fmt(ht[0])}</b> → 后半 <b>${fmt(ht[1])}</b> ms</span>`
 ].join("");
 bChart=phenChart("bchart",ph); charts.push(bChart);
 document.getElementById("btable").innerHTML=tableHTML(REQHEAD,ph.reqs.map(reqRow));
 document.getElementById("bcsv").onclick=()=>dlCSV("phenomenon_b.csv",[REQHEAD,...ph.reqs.map(reqRow)]);
}else document.getElementById("sec-b").style.display="none";
if(D.phenomenon_a){
 const ph=D.phenomenon_a;
 document.getElementById("an").textContent=ph.reqs.length;
 document.getElementById("achips").innerHTML=[
  `<span class="chip warn">窗口内抢占 <b>${ph.preempt_delta!=null?ph.preempt_delta:"-"}</b> 次</span>`,
  `<span class="chip">TTFT P50 / P99 <b>${fmt(ph.ttft_p50)} / ${fmt(ph.ttft_p99)}</b> ms</span>`,
  `<span class="chip">排队 P99 <b>${fmt(ph.queue_p99)}</b> ms</span>`,
  `<span class="chip">吞吐 <b>${fmt(ph.throughput)}</b> tok/s</span>`,
  D.meta.phenom_a_kv_cache_info?`<span class="chip">${D.meta.phenom_a_kv_cache_info}</span>`:""
 ].join("");
 aChart=phenChart("achart",ph); charts.push(aChart);
 document.getElementById("atable").innerHTML=tableHTML(REQHEAD,ph.reqs.map(reqRow));
 document.getElementById("acsv").onclick=()=>dlCSV("phenomenon_a.csv",[REQHEAD,...ph.reqs.map(reqRow)]);
}else document.getElementById("sec-a").style.display="none";

// ── 现象 C：FP16 vs AWQ ──
if(D.phenomenon_c){
 const c=D.phenomenon_c, f=D.levels.find(r=>r.level===c.level);
 document.getElementById("cchips").innerHTML=[
  `<span class="chip">并发 <b>${c.level}</b> · ${c.reqs.length} 条</span>`,
  f?`<span class="chip">吞吐 <b>${fmt(f.throughput)} → ${fmt(c.throughput)}</b> tok/s（${((c.throughput/f.throughput-1)*100).toFixed(1)}%）</span>`:"",
  D.meta.phenom_c_kv_cache_info?`<span class="chip">AWQ ${D.meta.phenom_c_kv_cache_info}</span>`:""
 ].join("");
 const rows=[
  ["吞吐 tok/s",f&&fmt(f.throughput),fmt(c.throughput)],
  ["TTFT P50 ms",f&&fmt(f.ttft_p50),fmt(c.ttft_p50)],
  ["TTFT P99 ms",f&&fmt(f.ttft_p99),fmt(c.ttft_p99)],
  ["ITL P50 ms",f&&fmt(f.itl_p50),fmt(c.itl_p50)],
  ["ITL P99 ms",f&&fmt(f.itl_p99),fmt(c.itl_p99)],
  ["总耗时 P50 ms",f&&fmt(f.total_p50),fmt(c.total_p50)],
  ["墙钟 s",f&&fmt(f.wall),fmt(c.wall)]];
 if(f)rows.forEach(r=>r.push(((parseFloat(r[2])/parseFloat(r[1])-1)*100).toFixed(1)+"%"));
 document.getElementById("ctable").innerHTML=tableHTML(
  f?["指标","FP16","AWQ","Δ"]:["指标","AWQ"],rows);
 document.getElementById("ckernel").textContent=
  (D.meta.phenom_c_kernel_lines||[]).join("\\n")||"日志中未出现 marlin / awq / falling back 关键词行";
 [["cm1","吞吐 tok/s","throughput","#3b6fd4"],
  ["cm2","TTFT P50 ms","ttft_p50","#e8963c"],
  ["cm3","ITL P50 ms","itl_p50","#7a5cd6"]].forEach(([el,name,key,color])=>{
  const ch=echarts.init(document.getElementById(el));
  ch.setOption({
   tooltip:{...TIP,trigger:"axis",axisPointer:{type:"shadow"}},
   toolbox:TOOL,
   title:{text:name,left:"center",top:0,textStyle:{fontSize:12,color:"#5a616e"}},
   grid:{left:50,right:16,top:34,bottom:26},
   xAxis:{type:"category",data:f?["FP16","AWQ"]:["AWQ"],...AXC},
   yAxis:{type:"value",scale:true,...AXC},
   series:[{type:"bar",barWidth:"46%",
     data:(f?[f[key],c[key]]:[c[key]]).map((v,i)=>({value:v,
       itemStyle:{color:i===0?"#9b8ce4":color}})),
     label:{show:true,position:"top",fontSize:11,formatter:p=>fmt(p.value)}}]});
  charts.push(ch);
 });
 document.getElementById("ccsv").onclick=()=>dlCSV("phenomenon_c.csv",[REQHEAD,...c.reqs.map(reqRow)]);
}else document.getElementById("sec-c").style.display="none";

// ── 运行时间轴 ──
const tl=echarts.init(document.getElementById("timeline"));
const ts=D.timeline.ts, kvv=D.timeline.kv;
const dmon=D.timeline.dmon||{ts:[],pclk:[],pwr:[],sm:[],mem:[]};
const dser=(arr)=>arr?dmon.ts.map((t,i)=>arr[i]!=null?[t*1000,arr[i]]:null).filter(Boolean):[];
tl.setOption({
 color:["#3b6fd4","#c95f81","#7a5cd6","#e8963c","#3aa6a6","#8a8f9c"],
 tooltip:{...TIP,trigger:"axis",axisPointer:{type:"cross",label:{backgroundColor:"#4a5163"}}},
 toolbox:TOOL,
 legend:{...LEG,data:["KV 占用 %","累计抢占","SM 频率","功耗 W","SM 利用率 %","显存带宽 %"]},
 axisPointer:{link:[{xAxisIndex:"all"}]},
 grid:[{left:70,right:70,top:44,height:150},{left:70,right:110,top:252,height:120}],
 xAxis:[{type:"time",gridIndex:0,...AXC},{type:"time",gridIndex:1,...AXC}],
 yAxis:[{type:"value",name:"KV %",max:100,...AXC,...NAM},
        {...AXR,name:"抢占",position:"right",...NAM},
        {type:"value",name:"MHz",gridIndex:1,scale:true,...AXC,...NAM},
        {...AXR,name:"W",gridIndex:1,position:"right",scale:true,...NAM},
        {...AXR,name:"%",gridIndex:1,position:"right",offset:48,max:100,...NAM}],
 dataZoom:[{type:"slider",xAxisIndex:[0,1],bottom:8,start:0,end:100,height:22,
   borderColor:"#d6dae1",backgroundColor:"#f6f7f9",fillerColor:"rgba(59,111,212,.12)",
   handleStyle:{color:"#fff",borderColor:"#3b6fd4"},
   dataBackground:{lineStyle:{color:"#b9c6de"},areaStyle:{color:"#eef1f6"}},
   textStyle:{color:"#5a616e",fontSize:10}},
  {type:"inside",xAxisIndex:[0,1],zoomOnMouseWheel:true,moveOnMouseMove:true,moveOnMouseWheel:false}],
 series:[
  {name:"KV 占用 %",type:"line",data:ts.map((t,i)=>[t,kvv[i]]),showSymbol:false,
   lineStyle:{width:1.8,color:"#3b6fd4"},areaStyle:{opacity:.12},
   markArea:{silent:true,data:(D.timeline.windows||[]).map(w=>[
     {xAxis:w.start*1000,itemStyle:{color:"rgba(122,92,214,.08)"},
      label:{show:true,position:"insideTop",color:"#7a5cd6",fontSize:11,formatter:w.name}},
     {xAxis:w.end*1000}])},
   markLine:{symbol:["none","none"],data:[],silent:true}},
  {name:"累计抢占",type:"line",yAxisIndex:1,step:"end",showSymbol:false,
   data:(D.timeline.preempt||[]).map(p=>[p[0]*1000,p[1]]),lineStyle:{width:1.8,color:"#c95f81"}},
  {name:"SM 频率",type:"line",xAxisIndex:1,yAxisIndex:2,showSymbol:false,
   data:dser(dmon.pclk),lineStyle:{width:1.8,color:"#7a5cd6"}},
  {name:"功耗 W",type:"line",xAxisIndex:1,yAxisIndex:3,showSymbol:false,
   data:dser(dmon.pwr),lineStyle:{width:1.8,color:"#e8963c"}},
  {name:"SM 利用率 %",type:"line",xAxisIndex:1,yAxisIndex:4,showSymbol:false,
   data:dser(dmon.sm),lineStyle:{width:1.4,color:"#3aa6a6"}},
  {name:"显存带宽 %",type:"line",xAxisIndex:1,yAxisIndex:4,showSymbol:false,
   data:dser(dmon.mem),lineStyle:{width:1.4,type:"dashed",color:"#8a8f9c"}}
 ]});

// ── 服务端指标：并发/显存/温度 ──
const srvCard=document.getElementById("sec-server");
let srv=null;
if(D.timeline.running||D.timeline.waiting||dmon.fb){
 srv=echarts.init(document.getElementById("srv"));
 srv.setOption({
  color:["#3b6fd4","#c95f81","#3aa6a6","#7a5cd6","#e8963c"],
  tooltip:{...TIP,trigger:"axis",axisPointer:{type:"cross",label:{backgroundColor:"#4a5163"}}},
  toolbox:TOOL,
  legend:{...LEG,data:["运行中请求","等待中请求","前缀命中率 %","显存占用 GB","GPU 温度 °C"]},
  axisPointer:{link:[{xAxisIndex:"all"}]},
  grid:[{left:70,right:70,top:44,height:140},{left:70,right:70,top:242,height:120}],
  xAxis:[{type:"time",gridIndex:0,...AXC},{type:"time",gridIndex:1,...AXC}],
  yAxis:[{type:"value",name:"请求数",...AXC,...NAM},
         {...AXR,name:"命中率 %",gridIndex:0,position:"right",max:100,...NAM},
         {type:"value",name:"GB",gridIndex:1,scale:true,...AXC,...NAM},
         {...AXR,name:"°C",gridIndex:1,position:"right",scale:true,...NAM}],
  dataZoom:[{type:"slider",xAxisIndex:[0,1],bottom:8,start:0,end:100,height:22,
    borderColor:"#d6dae1",backgroundColor:"#f6f7f9",fillerColor:"rgba(59,111,212,.12)",
    handleStyle:{color:"#fff",borderColor:"#3b6fd4"},
    dataBackground:{lineStyle:{color:"#b9c6de"},areaStyle:{color:"#eef1f6"}},
    textStyle:{color:"#5a616e",fontSize:10}},
   {type:"inside",xAxisIndex:[0,1],zoomOnMouseWheel:true,moveOnMouseMove:true,moveOnMouseWheel:false}],
  series:[
   {name:"运行中请求",type:"line",step:"end",showSymbol:false,
    data:(D.timeline.running||[]).map(p=>[p[0]*1000,p[1]]),lineStyle:{width:1.8,color:"#3b6fd4"}},
   {name:"等待中请求",type:"line",step:"end",showSymbol:false,
    data:(D.timeline.waiting||[]).map(p=>[p[0]*1000,p[1]]),lineStyle:{width:1.8,color:"#c95f81"}},
   {name:"前缀命中率 %",type:"line",yAxisIndex:1,showSymbol:false,
    data:(D.timeline.hit||[]).map(p=>[p[0]*1000,+(p[1]*100).toFixed(2)]),lineStyle:{width:1.4,type:"dotted",color:"#3aa6a6"}},
   {name:"显存占用 GB",type:"line",xAxisIndex:1,yAxisIndex:2,showSymbol:false,
    data:dmon.fb?dmon.ts.map((t,i)=>dmon.fb[i]!=null?[t*1000,+(dmon.fb[i]/1024).toFixed(2)]:null).filter(Boolean):[],
    lineStyle:{width:1.8,color:"#7a5cd6"}},
   {name:"GPU 温度 °C",type:"line",xAxisIndex:1,yAxisIndex:3,showSymbol:false,
    data:dser(dmon.gtemp),lineStyle:{width:1.8,color:"#e8963c"}}
  ]});
 echarts.connect([tl,srv]);
}else srvCard.style.display="none";

// ── 吞吐交叉验证 ──
const crossCard=document.getElementById("sec-cross");
let cross=null;
if(D.levels.some(r=>r.srv_tps!=null)){
 cross=echarts.init(document.getElementById("cross"));
 cross.setOption({
  tooltip:{...TIP,trigger:"axis",axisPointer:{type:"shadow"},valueFormatter:v=>v==null?"-":v+" tok/s"},
  toolbox:TOOL,
  legend:LEG,
  grid:{left:70,right:40,top:44,bottom:40},
  xAxis:{type:"category",data:lv,name:"并发",...AXC,...NAM},
  yAxis:{type:"value",name:"tokens/s",...AXC,...NAM},
  series:[
   {name:"客户端吞吐",type:"bar",barWidth:"30%",data:tp,itemStyle:{color:"#3b6fd4"}},
   {name:"服务端计数差分",type:"bar",barWidth:"30%",data:D.levels.map(r=>r.srv_tps),itemStyle:{color:"#3aa6a6"}}
  ]});
}else crossCard.style.display="none";

// ── 显存占比环形图 ──
const memCard=document.getElementById("sec-mem");
let mem=null;
if(D.memory){
 const MC=["#3b6fd4","#7a5cd6","#e8963c","#3aa6a6","#c95f81"];
 const entries=Object.entries(D.memory);
 const total=entries.reduce((s,[,v])=>s+v,0);
 mem=echarts.init(document.getElementById("mem"));
 mem.setOption({
  tooltip:{...TIP,formatter:p=>`${p.name}<br><b>${p.value} GB</b>（${p.percent}%）`},
  toolbox:TOOL,
  title:{text:total.toFixed(1)+" GB",subtext:"已占用显存",left:"center",top:"42%",
    textStyle:{color:"#1f2328",fontSize:22,fontWeight:700},subtextStyle:{color:"#5a616e",fontSize:12}},
  series:[{type:"pie",radius:["52%","76%"],center:["50%","50%"],
    itemStyle:{borderColor:"#fff",borderWidth:2},
    label:{color:"#5a616e",formatter:"{b} {d}%"},labelLine:{length:12,length2:8},
    data:entries.map(([k,v],i)=>({name:k,value:v,itemStyle:{color:MC[i%MC.length]}}))}]});
 document.getElementById("memlegend").innerHTML=entries.map(([k,v],i)=>
  `<div><span style="color:${MC[i%MC.length]}">■</span>`+
  `<span style="margin-left:8px">${k}</span><b>${v.toFixed(1)} GB · ${(v/total*100).toFixed(0)}%</b></div>`).join("")+
  (D.meta.kv_cache_info?`<div style="border:none;margin-top:8px">${D.meta.kv_cache_info}</div>`:"")+
  (D.meta.idle_gpu_mem_used_mb!=null?`<div style="border:none">空闲基线 ${D.meta.idle_gpu_mem_used_mb} MB · CUDA 上下文开销 ${D.meta.cuda_context_overhead_mb} MB</div>`:"");
}else memCard.style.display="none";

// ── 延迟分布 ECDF ──
let ecdfLog=true;
const eT=echarts.init(document.getElementById("ecdf_ttft"));
const eI=echarts.init(document.getElementById("ecdf_itl"));
function renderEcdf(){
 const mk=(title,key)=>({
  tooltip:{...TIP,trigger:"axis",valueFormatter:v=>v},
  toolbox:TOOL,
  title:{text:title,left:8,top:0,textStyle:{fontSize:12.5,color:"#5a616e"}},
  legend:{...LEG,data:lv.map(n=>"并发 "+n),top:0,right:60},
  grid:{left:70,right:30,top:40,bottom:44},
  xAxis:{type:ecdfLog?"log":"value",name:"ms",...AXC,...NAM},
  yAxis:{type:"value",name:"累计占比",max:1,...AXC,...NAM},
  series:D.levels.map((r,i)=>({name:"并发 "+r.level,type:"line",showSymbol:false,
    data:r[key],lineStyle:{width:1.8,color:COLORS[i%COLORS.length]},
    itemStyle:{color:COLORS[i%COLORS.length]},emphasis:{focus:"series"}}))
 });
 eT.setOption(mk("TTFT（逐请求）","ttft_ecdf"));
 eI.setOption(mk("ITL（全部 token 间隔池化）","itl_ecdf"));
}
renderEcdf();
document.getElementById("ecfgltgl").addEventListener("click",e=>{
 const b=e.target.closest("button"); if(!b)return;
 ecdfLog=b.dataset.v==="log";
 [...e.currentTarget.children].forEach(x=>x.classList.toggle("on",x===b));
 renderEcdf(); applySel();
});

// ── 逐请求明细：请求时间线，橙=TTFT 段，蓝=Decode 段 ──
let curRows=[], curLevel=null;
const drill2=echarts.init(document.getElementById("drill2"));
function drawDrill(level){
 curLevel=level;
 curRows=(D.drill[String(level)]||[]).filter(r=>r.st!=null&&r.tot!=null);
 document.getElementById("drilltitle").textContent=`逐请求明细 · 并发 ${level}（${curRows.length} 条）`;
 drill2.setOption({
  tooltip:{...TIP,formatter:p=>{const r=curRows[p.dataIndex];
    return `请求 #${r.i}<br>到达 +${r.st} s · TTFT ${fmt(r.ttft)} ms<br>平均 ITL ${fmt(r.itl_mean)} ms · 总耗时 ${fmt(r.tot)} ms<br>输出 ${r.out} token`;}},
  grid:{left:70,right:40,top:20,bottom:40},
  xAxis:{type:"value",name:"时间 s",...AXC,...NAM},
  yAxis:{type:"category",data:curRows.map(r=>"#"+r.i),inverse:true,...AXC,
    axisLabel:{fontSize:10,color:"#5a616e"}},
  series:[{type:"custom",
   renderItem:(params,api)=>{
     const idx=api.value(0);
     const x0=api.coord([api.value(1),idx]);
     const x1=api.coord([api.value(1)+api.value(2)/1000,idx]);
     const x2=api.coord([api.value(1)+api.value(3)/1000,idx]);
     const h=Math.max(api.size([0,1])[1]*0.62,3);
     return {type:"group",children:[
       {type:"rect",shape:{x:x0[0],y:x0[1]-h/2,width:Math.max(x1[0]-x0[0],1.5),height:h},
        style:{fill:"#e8a33d"}},
       {type:"rect",shape:{x:x1[0],y:x1[1]-h/2,width:Math.max(x2[0]-x1[0],1),height:h},
        style:{fill:"#3b6fd6"}}]};
   },
   encode:{x:[1,4],y:0},
   data:curRows.map((r,i)=>[i,r.st,r.ttft||0,r.tot,r.st+r.tot/1000])}]
 },true);
 document.getElementById("reqtable").innerHTML=tableHTML(REQHEAD,curRows.map(reqRow));
}
document.getElementById("drillcsv").onclick=()=>{
 if(curLevel!=null)dlCSV(`level_${curLevel}_requests.csv`,[REQHEAD,...curRows.map(reqRow)]);
};
drawDrill(lv[lv.length-1]);

// ── 数据表 ──
const LVHEAD=["并发","吞吐run1","吞吐run2","TTFT P50","TTFT P90","TTFT P99","排队P50","排队P99","ITL P50","ITL P99","总耗时P50","服务端吞吐","墙钟s"];
const lvRow=r=>[r.level,fmt(r.throughput),r.run2?fmt(r.run2.throughput):null,
 fmt(r.ttft_p50),fmt(r.ttft_p90),fmt(r.ttft_p99),fmt(r.queue_p50),fmt(r.queue_p99),
 fmt(r.itl_p50),fmt(r.itl_p99),fmt(r.total_p50),r.srv_tps!=null?fmt(r.srv_tps):null,fmt(r.wall)];
document.getElementById("lvtable").innerHTML=tableHTML(LVHEAD,D.levels.map(lvRow));
const RUNHEAD=["并发","吞吐 r1","吞吐 r2","TTFT P50 r1","TTFT P50 r2","TTFT P99 r1","TTFT P99 r2","ITL P50 r1","ITL P50 r2"];
const runRow=r=>[r.level,fmt(r.throughput),r.run2?fmt(r.run2.throughput):null,
 fmt(r.ttft_p50),r.run2?fmt(r.run2.ttft_p50):null,fmt(r.ttft_p99),r.run2?fmt(r.run2.ttft_p99):null,
 fmt(r.itl_p50),r.run2?fmt(r.run2.itl_p50):null];
document.getElementById("runtable").innerHTML=tableHTML(RUNHEAD,D.levels.map(runRow));
document.getElementById("lvscsv").onclick=()=>dlCSV("levels.csv",[LVHEAD,...D.levels.map(lvRow)]);
document.getElementById("runscsv").onclick=()=>dlCSV("runs.csv",[RUNHEAD,...D.levels.map(runRow)]);

// ── 点击选中档位：跨图联动 ──
let sel=null;
function applySel(){
 main.setOption({series:[
   {data:tp.map((v,i)=>({value:v,itemStyle:{color:"#3b6fd4",opacity:(sel==null||sel===i)?1:.22}}))},
   {data:D.levels.map((r,i)=>({value:r.run2&&r.run2.throughput,itemStyle:{color:"#9b8ce4",opacity:(sel==null||sel===i)?1:.22}}))},
   {},{}]});
 const pct=bdMode==="pct";
 bd.setOption({series:[0,1,2].map(i=>({
   data:(pct?D.breakdown:D.breakdown_ms).map((r,j)=>({value:r[i],itemStyle:{opacity:(sel==null||sel===j)?1:.3}}))}))});
 if(cross) cross.setOption({series:[0,1].map(i=>({
   data:(i===0?tp:D.levels.map(r=>r.srv_tps))
     .map((v,j)=>({value:v,itemStyle:{opacity:(sel==null||sel===j)?1:.3}}))}))});
 const r=sel!=null?D.levels[sel]:null;
 tl.setOption({series:[{markLine:{data:r&&r.t0?[[{xAxis:r.t0*1000},{xAxis:r.t1*1000}]]:[],
   lineStyle:{color:"#7a5cd6",width:1.5,type:"solid"},label:{show:false}}}]});
 eT.setOption({series:D.levels.map((_,i)=>({lineStyle:{opacity:(sel==null||sel===i)?1:.12}}))});
 eI.setOption({series:D.levels.map((_,i)=>({lineStyle:{opacity:(sel==null||sel===i)?1:.12}}))});
 drawDrill(sel!=null?lv[sel]:lv[lv.length-1]);
}
function selectLevel(l){
 const di=lv.indexOf(l);
 sel=(di===sel)?null:di;
 applySel();
}
main.on("click",p=>{ if(p.componentSubType==="bar"||p.componentSubType==="line") selectLevel(+p.name); });
bd.on("click",p=>{ selectLevel(+p.name); });

charts.push(main,bd,tl,srv,cross,eT,eI,drill2,mem);
window.addEventListener("resize",()=>charts.filter(Boolean).forEach(c=>c.resize()));
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir", nargs="?", help="run_bench.py 产物目录")
    ap.add_argument("--demo", action="store_true", help="生成合成演示数据并出图")
    ap.add_argument("--out", help="输出 HTML 路径，默认产物目录下 report.html")
    args = ap.parse_args()

    if args.demo:
        rdir = args.results_dir or "results/demo"
        make_demo(rdir)
    elif not args.results_dir:
        ap.error("需要 results 目录，或用 --demo")
        return
    else:
        rdir = args.results_dir

    data = build_data(rdir)
    html = (HTML_TEMPLATE
            .replace("__ECHARTS__", ECHARTS_PATH.read_text())
            .replace("__DATA__", json.dumps(data, ensure_ascii=False)))
    out = args.out or str(Path(rdir) / "report.html")
    Path(out).write_text(html)
    print(f"[plot] 报告已生成：{out}（{os.path.getsize(out)//1024} KB）")


if __name__ == "__main__":
    main()