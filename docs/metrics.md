# 服务端指标采集：prometheus.yml 与 export_metrics.py

## 作用
vLLM 自带 Prometheus 格式的 `/metrics` 端点，排队时间、KV 块占用、抢占次数这些调度器内部状态只有它有。这两个文件负责把它采下来、带回来：

- `scripts/prometheus.yml`：抓取配置，每 1 秒抓一次 `localhost:8000/metrics`。
- `scripts/export_metrics.py`：实验结束后调 Prometheus 的 `query_range` API，把全部 `vllm:*` 时间序列导出成 JSON，一个指标一个文件。

导出用通配 `{__name__=~"vllm:.+"}` 而不是写死指标名，原因是 vLLM 各版本给指标改过名（0.33 实测 KV 占用叫 `vllm:kv_cache_usage_perc`，早期版本叫 `vllm:gpu_cache_usage_perc`），写死单名会漏数据。画图脚本按"精确指标文件名优先、前缀 glob 兜底"匹配：同前缀还有 `*_created`、`external_*`、`*_by_reason` 这些干扰文件，只靠模糊匹配会拿错序列。

## 使用
正常流程不用手动起，`run_bench.py` 会自动启动 Prometheus 并在结束时导出。单独调试时：

```bash
# 起 Prometheus（本机已装 3.11.3，命令直接可用）
prometheus --config.file=scripts/prometheus.yml \
  --storage.tsdb.path=/tmp/prom_db --web.listen-address=:9090

# 服务跑起来后导出，时间戳用 date +%s 取
python scripts/export_metrics.py \
  --start $(date -d '10 minutes ago' +%s) --end $(date +%s) \
  --out-dir results/test/prometheus
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--prom-url` | `http://localhost:9090` | Prometheus 地址 |
| `--start` / `--end` | 必填 | 导出窗口的 Unix 时间戳 |
| `--step` | `1s` | 采样步长，和抓取间隔一致 |
| `--out-dir` | 必填 | 输出目录 |

## 输出格式
每个文件是一个数组，数组元素是 Prometheus 返回的序列对象：`metric`（标签，含 `__name__`）加 `values`（`[时间戳, 值]` 列表）。直方图类指标（排队时间、TTFT、ITL）会导出三组文件：`_bucket`（累积计数）、`_sum`、`_count`。

## 验收判据
1. `promtool check config scripts/prometheus.yml` 通过。
2. 服务有负载时导出目录里能看到 `vllm_request_queue_time_seconds_bucket`（或对应版本改名后的同类文件）和抢占计数文件，`values` 非空。

## 读直方图的注意事项
`_bucket` 是累积计数、从进程启动开始算，不是窗口值。要拿"某一档的排队 P50/P99"，得用该档结束时刻减开始时刻的 bucket 差值重建分布再算分位数，这个计算放在画图脚本里做。
