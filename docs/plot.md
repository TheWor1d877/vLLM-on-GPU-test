# 画图脚本：plot.py

## 作用
读 `run_bench.py` 的产物目录，算出报告需要的全部数字，输出一个内嵌数据与 ECharts 的单文件 HTML（`report.html`）。文件约 1 MB，断网可开，直接发给面试官。

报告浅色学术风格，顶部粘性导航与滚动进度条，按"结论 → 证据 → 过程"排：

1. 顶部五张数字卡：经济并发点（达到峰值吞吐 90% 的最小并发）、吞吐上限、最高档相对 8 并发的吞吐增益、最高档 TTFT P50 中排队占比、全程累计抢占次数。卡下一条数据质量条：总请求数、截断数（output_tokens 不满）、失败数（无 TTFT）。
2. 主图：吞吐双柱（run1/run2）+ TTFT 分位折线（P50/P90/P99 按钮切换），右轴可切对数刻度；紫色虚线标经济并发点，峰值标数值。
3. 耗时构成：每档一根堆叠柱（排队/Prefill/Decode 三段），占比 100% 与绝对毫秒两种模式切换。
4. 现象 B 专卡：逐请求平均 ITL 与 TTFT 随请求序号的散点（上下两格），各自带前半/后半均值线，漂移百分比直接标在卡头；200 条原始表折叠可展开，可下 CSV。
5. 现象 A 专卡：同样的逐请求双格散点，卡头标窗口内抢占增量与小 KV 池容量（meta 的 phenom_a_kv_cache_info）。
6. 现象 C 专卡：FP16 与 AWQ 同档对比表 + 成对柱图（吞吐/TTFT P50/ITL P50），kernel 日志行原文展示（Marlin 还是慢路径）。无现象 C 数据时整卡隐藏。
7. 运行时间轴：上格 KV 占用与累计抢占；下格 SM 频率、功耗、SM 利用率与显存带宽利用率（后两条共用右偏移百分比轴）。扫描段与现象 B/A/C 窗口涂色。
8. 服务端指标：上格运行中/等待中请求数台阶线与前缀缓存命中率，下格显存实际占用（dmon fb）与 GPU 温度（gtemp），与运行时间轴同窗缩放。
9. 吞吐交叉验证：客户端统计吞吐 vs 对 generation_tokens_total 按档差分，两柱并排。
10. 显存占比：环形图分模型权重/KV 块池/激活与上下文碎片，取 meta.json 的 memory_breakdown（run_bench 自动回填），缺失时整卡隐藏；卡内附 KV 池容量原文与 idle/context 基线，完整 meta 折叠可查。
11. 延迟分布：TTFT ECDF（每档一条累积分布曲线，x 轴对数毫秒，可切线性）与 ITL ECDF（每档全部 token 间隔池化，降采样到 300 点内）。P50/P99 从曲线上直接读。
12. 逐请求明细：选中档的到达时刻×总耗时散点，y 轴可切平均 ITL；框选工具拉框即时统计选中条数与均值区间；下方逐请求原始表（序号/到达/TTFT/平均ITL/总耗时/输出token），可下 CSV。
13. 数据表：档位总表（全部指标一屏）与 run1/run2 双跑对比表，均可下 CSV。

交互能力：点击主图或堆叠柱任一档位即选中（未选档淡出、时间轴画出该档时间窗标线、ECDF 高亮该档曲线、明细面板切换，再点取消）；悬停弹该档全部指标表；时间轴按住左键拖动平移、滚轮缩放、滑块可拖；每张图右上角工具箱可导出 PNG（2 倍清晰度）。

## 数据口径
| 报告字段 | 来源与算法 |
|---|---|
| 吞吐、TTFT/ITL P50/P90/P99 | `client/level_<N>_run<k>.json` 逐请求数据算分位数 |
| 排队 P50/P99 | 从 `prometheus/vllm_request_queue_time_seconds_bucket.json` 按档位时间窗重建直方图分位数（histogram_quantile 同款算法，只用有限桶）。窗口取 client 时间戳：起点为首个请求到达，终点为最后请求完成；桶增量终点取窗口右端加 3 秒记录滞后之后的第一个抓取样本（服务端记入直方图再被采到，实测可晚约两个抓取周期），避免完成时刻落在两次抓取之间被漏计。server_args 已把 request_latency 桶族低段加密到 50ms 一档（vLLM 默认 0.3/0.5/0.8 起步，排队与 e2e 分位数会挤在同一桶）。bench_client 不输出排队字段，导出文件缺失时该字段为空 |
| Prefill / Decode | 每档 P50 拆分：TTFT P50 减排队、总耗时 P50 减 TTFT。三段恒等于总耗时，不设残差段 |
| KV 占用 / 抢占 | `prometheus/` 的 gauge/counter 序列。文件按精确指标名优先匹配（0.33 版 KV 占用叫 `vllm_kv_cache_usage_perc.json`），取不到再按前缀 glob，demo 文件名走这条回退 |
| SM 频率 / 功耗 / SM 利用率 / 显存带宽利用率 | `dmon.csv`（`-o DT` 带时间戳格式）的 pclk / pwr / sm / mem 列 |
| TTFT ECDF | client JSON 逐请求 TTFT 排序，逐点 (值, 累计序号/总数) |
| ITL ECDF | 每档全部请求的 itl_ms 池化排序，等距降采样到 ≤300 点 |
| 现象 B/A 逐请求散点 | 各自 client JSON 的逐请求 itl_mean（该请求全部 ITL 的均值）与 ttft_ms，x 为请求序号；前后半均值按序号对半切 |
| 现象 A 抢占增量 | num_preemptions_total 在 A 窗口两端差分 |
| 现象 C 对比 | client/phenomenon_c.json 与主扫描同并发档 run1 对比；kernel 行取 meta.phenom_c_kernel_lines |
| 服务端 TTFT/e2e P50/P99 | prometheus 的 time_to_first_token/e2e_request_latency 桶按档时间窗重建 |
| 服务端吞吐 | generation_tokens_total 计数器按档窗口差分/墙钟 |
| 运行中/等待中 | prometheus gauge 原值（`vllm_num_requests_running/waiting.json`） |
| 命中率 | `prefix_cache_hits_total` 与 `prefix_cache_queries_total` 两个 counter 按时间戳对齐取相邻增量之比；两者缺失时退回命中率 gauge（演示数据） |
| 显存占用/温度 | dmon.csv 的 fb（MiB 换算 GB）与 gtemp 列 |
| 数据质量条 | 逐请求 output_tokens 对 spec.output_len 计截断，ttft_ms 为空计失败 |
| 显存占比 | meta.json 的 memory_breakdown（run_bench 从服务日志与 nvidia-smi 基线自动回填） |
| 数字卡与经济点虚线 | 由上表汇总计算，无手工输入 |
| CSV 下载 | 前端用内嵌数据即时生成 Blob，不改动产物目录 |

## 使用
```bash
python3 scripts/plot.py results/<日期>/          # 正式数据
python3 scripts/plot.py --demo                   # 合成演示数据，验证呈现用
python3 scripts/plot.py results/<日期>/ --out /tmp/report.html
```
只依赖标准库，系统 Python 直接跑。依赖文件 `scripts/assets/echarts.min.js`（5.5.1，已就位）。

## 验收判据
1. 浏览器打开无控制台报错，全部图渲染；无现象 C 数据时 C 卡隐藏、无 prometheus 时服务端指标与交叉验证隐藏。
2. 数字卡的值与 README 表格手算一致（分位数允许 ±1 舍入差）。
3. 演示数据生成的报告右上角必须带橙色「演示数据」角标，正式数据不出现该角标。
4. 每张图工具箱能导出 PNG；档位总表、双跑表、逐请求表、现象 B 表的 CSV 按钮能下载且行数对得上。

## 已知限制
- 演示数据的数值曲线是按预期结论合成的形状，只用于评审呈现设计，任何字段都不能抄进报告正文。
- ITL ECDF 是降采样曲线，尾部极值以逐请求表为准。
- 显存占比依赖 run_bench 解析服务日志成功；解析不到时该卡隐藏，不显示合成值。
- 时间轴上现象 A 段的 KV 占用是 gauge 原始值，压池子重启服务后 gauge 会归零重涨，图上表现为断崖，解读时注意这是两次服务，不是数据错误。现象 C 段同理（第三次服务）。