# 压测客户端：bench_client.py

## 作用
`scripts/bench_client.py` 是发压脚本：固定并发地向 vLLM 的 `/v1/completions` 接口发请求，逐条记录 TTFT（首 token 延迟）、ITL（token 间隔延迟）序列、输出 token 数、总耗时，跑完把整体吞吐和逐请求数据写成一个 JSON 文件。`run_bench.py` 每档调它一次，也可以单独手动调用。

三个设计点，都是为了让数据可比：

- 输入用随机 token id 序列（固定种子），不走文本编码。发文本做不到输入恰好 512 token，token id 可以。
- 请求带 `ignore_eos=True` 和 `max_tokens=128`，强制每条输出 128 token。不带的话模型提前停，输出长短不齐，ITL 在档位之间没法比。
- 请求带 `return_token_ids=True`，按流式 chunk 的 `token_ids` 列表精确计数。`text` 字段是增量 detokenize 结果，随机 token 解码时经常为空串，按 text 数 chunk 会漏计（vLLM 0.33 没有 `tokens` 字段，读它等于永远走 text 回退）。
- 并发用 asyncio 信号量控制：同时最多 N 条请求在飞，N 就是档位值。40 条请求全部提交，信号量放行多少就并发多少。

## 使用
```bash
~/Project/vLLM-on-GPU-test/.venv/bin/python scripts/bench_client.py \
  --model qwen --concurrency 8 --num-requests 40 \
  --out results/test/level_8.json
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--base-url` | `http://localhost:8000` | vLLM 服务地址 |
| `--model` | 必填 | 请求里带的模型名，对应服务端的 `--served-model-name` |
| `--concurrency` | 必填 | 并发档位 |
| `--num-requests` | 40 | 本档请求总数 |
| `--input-len` | 512 | 每条请求的输入 token 数 |
| `--output-len` | 128 | 每条请求的输出 token 上限 |
| `--seed` | 42 | 随机种子，同种子同长度下输入序列完全一致 |
| `--out` | 必填 | 输出 JSON 路径 |

## 输出格式
一个 JSON 对象，头部是档位参数和汇总（`wall_seconds` 全程秒数、`throughput_tokens_per_s` 输出 token 吞吐），`requests` 数组逐条给 `request_id`、`start_ts`、`ttft_ms`、`itl_ms`（127 个间隔）、`output_tokens`、`total_ms`。

## 验收判据
用 0.5B 模型跑 1/2/4/8 四档、每档 8 条，检查三点：JSON 里每条记录的 `output_tokens` 都等于 128；`itl_ms` 长度都等于 127；吞吐随并发单调上涨到饱和。

## 已知限制
客户端计时包含本机协议栈开销，同机回环下是微秒级，对毫秒级结论无影响。排队时间在这里拿不到：请求在服务端调度器里排多久，客户端看不见，那部分数据走 Prometheus 通道（见 docs/metrics.md）。
