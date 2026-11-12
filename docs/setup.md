# 脚本 1：setup.sh（安装检查与冒烟测试）

## 作用
`scripts/setup.sh` 在机器上验证四件事：venv 里的 vllm 能导入、vLLM 服务能起、服务能答一条请求、`/metrics` 端点吐得出 vllm 指标。租卡机开机后先跑它，本地 4060 彩排也用它。脚本用彩排小模型（Qwen2.5-0.5B-Instruct），这一步不产生任何验收数据。

## 使用
```bash
bash scripts/setup.sh
```
三个环境变量可以覆盖默认值，不传就用括号里的默认：

- `VENV`：装了 vllm 的虚拟环境路径（默认 `~/Project/vLLM-on-GPU-test/.venv`）
- `MODEL`：起服务用的模型目录（默认 `~/models/Qwen2.5-0.5B-Instruct`）
- `PORT`：服务端口（默认 8000）

租卡机上典型用法是换掉前两个：

```bash
VENV=~/vllm-env MODEL=~/models/Qwen2.5-7B-Instruct bash scripts/setup.sh
```

脚本结束时把服务进程杀掉（trap 处理），不留后台进程。

## 验收判据
按输出顺序核对四条：

1. 第一行打印 vllm 版本号，把这个号抄进版本锁。
2. 出现 `service ready`。0.5B 模型一般十几秒就绪；超过 5 分钟没就绪就是环境有问题。
3. 冒烟请求返回一段 JSON，里面有 `choices` 字段。
4. 指标列表里出现排队时间和抢占计数两项（`vllm:request_queue_time_seconds` 与 `vllm:num_preemptions_total`，具体名字以实际输出为准，不同 vLLM 版本有改名）。第 4 条过了，Prometheus 采集才有东西可抓。

## 已知限制
脚本起服务只带 `--gpu-memory-utilization 0.9` 一个参数，正式实验的全部服务参数在 `scripts/config.yaml` 里，两边不一致时以 config 为准。
