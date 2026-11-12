#!/usr/bin/env python3
"""压测客户端：按指定并发向 vLLM 发定长 token 请求，逐请求落盘 JSON。

作用、参数与验收判据见 docs/bench-client.md
"""
import argparse
import asyncio
import json
import random
import time

import aiohttp


def make_prompts(n, length, seed, vocab_lo=1000, vocab_hi=150000):
    """生成 n 条定长随机 token id 序列。直接发 token id，输入长度才精确。"""
    rng = random.Random(seed)
    return [[rng.randint(vocab_lo, vocab_hi) for _ in range(length)] for _ in range(n)]


async def one_request(session, base_url, model, prompt_ids, out_len, sem, rec):
    payload = {
        "model": model,
        "prompt": prompt_ids,
        "max_tokens": out_len,
        "ignore_eos": True,  # 强制生成到 max_tokens，输出长度才齐，ITL 才能跨档比
        "temperature": 0.0,
        "stream": True,
        "return_token_ids": True,  # 流式 chunk 带 token_ids 列表，计数才精确
    }
    async with sem:
        t0 = time.perf_counter()
        rec["start_ts"] = time.time()
        ttft = None
        itl = []
        last = None
        out_tokens = 0
        async with session.post(f"{base_url}/v1/completions", json=payload) as resp:
            resp.raise_for_status()
            async for raw in resp.content:
                line = raw.decode(errors="ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                choice = chunk["choices"][0]
                # vLLM 0.33 流式 chunk 的 token id 列表在 token_ids 字段（需
                # return_token_ids）；text 是增量 detokenize 结果，可能为空，
                # 按 text 计数会把空 text 的 chunk 漏掉
                toks = choice.get("token_ids") or ([None] if choice.get("text") else [])
                now = time.perf_counter()
                for _ in toks:
                    if ttft is None:
                        ttft = now - t0
                    elif last is not None:
                        itl.append(now - last)
                    last = now
                    out_tokens += 1
        rec["ttft_ms"] = round(ttft * 1000, 2) if ttft is not None else None
        rec["itl_ms"] = [round(x * 1000, 2) for x in itl]
        rec["output_tokens"] = out_tokens
        rec["total_ms"] = round((time.perf_counter() - t0) * 1000, 2)


async def run(args):
    prompts = make_prompts(args.num_requests, args.input_len, args.seed)
    sem = asyncio.Semaphore(args.concurrency)
    connector = aiohttp.TCPConnector(limit=args.concurrency)
    async with aiohttp.ClientSession(connector=connector) as session:
        recs = [{"request_id": i} for i in range(args.num_requests)]
        t0 = time.perf_counter()
        tasks = [
            asyncio.create_task(
                one_request(session, args.base_url, args.model, p, args.output_len, sem, r))
            for p, r in zip(prompts, recs)
        ]
        await asyncio.gather(*tasks)
        wall = time.perf_counter() - t0

    total_out = sum(r["output_tokens"] for r in recs)
    result = {
        "concurrency": args.concurrency,
        "num_requests": args.num_requests,
        "input_len": args.input_len,
        "output_len": args.output_len,
        "seed": args.seed,
        "wall_seconds": round(wall, 2),
        "throughput_tokens_per_s": round(total_out / wall, 1),
        "requests": recs,
    }
    with open(args.out, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f"[bench] 并发={args.concurrency} 请求数={args.num_requests} "
          f"吞吐={result['throughput_tokens_per_s']} tokens/s -> {args.out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--model", required=True, help="served-model-name 或模型路径")
    ap.add_argument("--concurrency", type=int, required=True)
    ap.add_argument("--num-requests", type=int, default=40)
    ap.add_argument("--input-len", type=int, default=512)
    ap.add_argument("--output-len", type=int, default=128)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", required=True, help="输出 JSON 路径")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
