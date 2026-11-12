# 脚本 2：run_bench.py（正式执行驱动）

## 作用
`scripts/run_bench.py` 把租卡后的全部测量串成一条命令：起 Prometheus 和 dmon、起 vLLM、测显存基线、六档并发扫描（每档双跑）、现象 B 长跑、压小 KV 池重启服务跑现象 A、换 AWQ 权重重启服务跑现象 C、导出指标、收尾打包。租卡机上执行过程零手敲，跑完产物直接落在 `results/<日期时间>/`。

执行顺序里两处安排有原因：

- 现象 B 排在现象 A/C 前面：B 用默认配置的服务，A 要带 `--num-gpu-blocks-override` 重启，C 要换 AWQ 模型重启。B 在最前只多重启两次，租卡时间就是钱。
- 版本锁在起服务之前写：万一中途崩了，`meta.json` 里已有卡型号、驱动、CUDA、vllm 版本这些底账。

## 使用
```bash
python scripts/run_bench.py scripts/config.yaml
```
配置文件路径可以不传，默认读同目录 `config.yaml`。所有可调项都在这份 yaml 里：

| 字段 | 说明 |
|---|---|
| `model` / `venv` | 模型目录与 venv 路径，租卡机上这两个必改 |
| `model_sha256` | 本地预算好的权重哈希（文件名到 sha256 的映射），填了就不在租卡机上现算 |
| `served_name` / `port` | 客户端请求用的模型名与端口，两边共用 |
| `levels` / `requests_per_level` | 并发档位列表与每档请求数，正式跑 `[1,2,4,8,16,32]` × 40 |
| `input_len` / `output_len` / `seed` | 请求构造参数，透传给 bench_client |
| `double_run_gap_s` | 每档两次跑之间的间隔 |
| `server_args` | vLLM 服务默认参数列表，原样拼进启动命令。已含 `--custom-histogram-buckets`：request_latency 桶族（e2e/queue/inference/prefill/decode 共用）1 秒内加密到 50ms 一档，细桶事后能合并、粗桶事后分不开，租卡只跑一次必须取细的 |
| `phenomenon_b` | `enabled` / `concurrency` / `requests`，16 并发 200 条 |
| `phenomenon_a` | `enabled` / `concurrency` / `requests` / `extra_server_args`，压池子的 blocks 值填在这里 |
| `phenomenon_c` | `enabled` / `model` / `model_sha256` / `concurrency` / `requests` / `extra_server_args`。换 AWQ 权重重启服务跑一档，concurrency 与主扫描某档对齐（默认 8）供同档对比；`--quantization awq` 显式指定量化。kernel 观测从 `vllm_phenomC.log` 抓 Marlin/慢路径关键词行写进 meta |

## 产物结构
```
results/<日期时间>/
  meta.json                 # 版本锁 + 显存基线（idle 占用、KV 池容量、空 torch 进程分配、context 开销）
                            # + memory_breakdown（自动回填，见下）+ 现象 C 的 kernel 日志行
  client/level_<N>_run<k>.json   # 逐请求数据，k 为 1/2 两次跑
  client/phenomenon_b.json  # 长跑数据
  client/phenomenon_a.json  # 压池子数据
  client/phenomenon_c.json  # AWQ 对比数据
  prometheus/*.json         # vllm:* 时间序列导出
  dmon.csv                  # 功耗/频率/利用率全程
  vllm_main.log             # 默认服务日志（KV 池容量在里面）
  vllm_phenomA.log          # 压池子服务日志
  vllm_phenomC.log          # AWQ 服务日志（量化 kernel 行在里面）
```

显存账自动回填：起服务后从 `vllm_main.log` 解析权重占用（`Model loading took` / `Loading model weights took`）与 KV 池预算（`Available KV cache memory`），总 footprint 取服务启动后 nvidia-smi 占用减 idle 基线，三者写成 `meta.json` 的 `memory_breakdown`（模型权重 / KV 块池 / 激活与上下文碎片，单位 GiB）。日志行解析不到就不写该字段，报告显存账卡自动隐藏，不回填假数。

## 验收判据
1. 每段跑完立即硬校验：每条请求的 `output_tokens` 恰好等于 `output_len`（ignore_eos + max_tokens 口径），不符当场报错终止，不带病跑完。
2. `client/` 下文件数 = 档位数 × 2 + 启用的现象数，每个文件非空。
3. `meta.json` 里 `driver`、`cuda`、`vllm`、`model_sha256`、`idle_gpu_mem_used_mb`、`kv_cache_info`、`memory_breakdown` 都有值；启用现象 C 时 `phenom_c_kernel_lines` 非空。
4. `prometheus/` 下有排队时间直方图和抢占计数文件。
5. `dmon.csv` 行数覆盖整个运行窗口（1 秒一行）。

## 已知限制
- 起服务时若外部未设置，脚本默认注入 `VLLM_USE_FLASHINFER_SAMPLER=0`：flashinfer 采样内核要 nvcc JIT，冒烟实测系统 nvcc 12.4 不认识新压缩选项、venv 自带 CUDA 头文件又不全，两条路都编不过；关掉后走 vLLM 原生采样。租卡机若装了完整 CUDA 12.8+/13 toolkit，可在环境里显式设 1 换回 flashinfer。
- 关闭 chunked prefill 的开关名跨 vLLM 版本不一致，`server_args` 里先没放；装好 vllm 后用 `vllm serve --help | grep -i chunked` 核对实际写法再补进配置。
- 现象 A 的 blocks 值按 7B 的 KV 池换算：先跑一次默认配置，从 `vllm_main.log` 的 `GPU KV cache size` 读出总容量，取约 1/13 填进 `extra_server_args`。冒烟同样开启现象 A（0.5B、16 并发 20 请求），启动校验与抢占路径都要在冒烟走到。vLLM 0.33 启动时校验池子至少装得下一条 `max_model_len` 序列，压池后默认 32768 装不下会拒绝启动，`extra_server_args` 必须同时带 `--max-model-len 8192`（请求仅 640 token，口径不受影响）。
- 现象 C 的 kernel 观测口径：`vllm_phenomC.log` 里抓含 `marlin` / `awq` / `falling back` 的行（不区分大小写）。出现 Marlin 字样即走快路径；出现 `falling back` 或只有慢路径关键词就记录为回退。冒烟用 `Qwen2.5-0.5B-Instruct-AWQ`（需提前下载到 models 目录），正式跑用 7B-AWQ。
- 服务就绪等待上限 15 分钟，7B 冷启动加载权重一般 2–4 分钟；超时说明权重路径或显存有问题，看 `vllm_main.log`。
