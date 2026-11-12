#!/bin/bash
# 等 FP16 四个分片 .ok 齐 → 下 AWQ → sha256 核对 → 落 AWQ_DONE / AWQ_FAIL 哨兵
FP16=/home/howardhe/models/Qwen2.5-7B-Instruct
AWQ=/home/howardhe/models/Qwen2.5-7B-Instruct-AWQ
while [ "$(ls "$FP16"/*.ok 2>/dev/null | wc -l)" -lt 4 ]; do sleep 60; done
echo "FP16 complete, starting AWQ $(date)"
python3 /home/howardhe/Project/vLLM-on-GPU-test/scripts/dl_model_awq.py "$AWQ" || { echo AWQ_FAIL; exit 1; }
cd "$AWQ" || { echo AWQ_FAIL; exit 1; }
{
  echo "4ad6e70f93823a9228931615e04b5e9293854c236fb4bd5f85e27f3b600605ad  model-00001-of-00002.safetensors"
  echo "920a8cc9d3c81668f18014a69bb041ab8bab22a31640a713f90f5e7580e2f126  model-00002-of-00002.safetensors"
} | sha256sum -c - && echo AWQ_DONE || echo AWQ_FAIL