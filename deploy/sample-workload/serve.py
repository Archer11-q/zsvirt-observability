"""样例 AI 工作负载：一个**真实可跑**的 Transformer 推理服务。

赛题交付要求 d：「在虚拟机中运行至少一种容器化 AI 工作负载，例如模型推理服务……
**提供完整配置**」。本项目选模型推理服务，因为它天然产生本平台要观测的三层信号：

| 层 | 信号 |
|---|---|
| 容器 | 内存 / CPU 限额、OOM、重启 |
| 进程 | 崩溃、I/O 等待 |
| 服务 | 推理错误、超时、延迟与并发 |

**模型是真的**：DistilBERT 在 SST-2 上微调后的情感分类模型（ONNX 格式，
构建期经 SHA256 校验固化进镜像，见 `bake_model.py`）。
推理是真的：每个请求都跑一次完整的 Transformer 前向。

## 为什么是 ONNX Runtime 而不是 vLLM（一次修正，值得读）

本目录最初的版本是 `pip install vllm==0.8.5`。实测下来那条路在**没有 GPU 的机器上
根本走不通**：PyPI 上的 vLLM 只有 CUDA wheel（依赖整套 `nvidia-*`），
而 `download.pytorch.org/whl/cpu` 的 wheel 落在 `download-r2.pytorch.org`，
容器内**网络不可达**。也就是说那份配置的注释写着"CPU 可跑的极小模型"，
实际上演示机上跑不起来 —— 正是本项目一直在防的那类缺陷：**声明与事实不一致**。

ONNX Runtime 不是"退而求其次"：它是**同一个预训练模型**的另一个推理后端，
只是运行时不带 CUDA，体积从数 GB 降到约 90 MB，且在纯 CPU 上确定可跑。
有 GPU 的 VM 上把 `serve.py` 换成 vLLM 即可，对外接口不变。

## `KV_CACHE_MB`：把内存需求显式化

真实推理引擎（vLLM 等）启动时会按 `gpu_memory_utilization` **预分配 KV cache** ——
内存需求本来就是显式的。这里把同一件事写成环境变量：

    KV_CACHE_MB=256   + mem_limit 2g    → 健康
    KV_CACHE_MB=1200  + mem_limit 512m  → **必然**被 OOM Killer 终止

为什么做成"必然"而不是"碰运气"：演示需要一个确定会发生的故障。
模型加载本身的内存占用会随运行时版本波动，**用一个会波动的量当故障开关，
演示现场就会翻车**。

这块内存是**真实申请并真实写入**的（`fill(1)`）—— 不写入的话页是惰性映射的，
不计入 RSS，cgroup 也就不会 OOM。这一点写错了故障永远不会发生，
而现象是"限额明明很小却跑得好好的"，很容易被误读成"限额没生效"。
"""

from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from tokenizers import Tokenizer

# ---------------------------------------------------------------- 日志
#
# `-u`（见 Dockerfile 的 CMD）与这里的显式 flush 都是**故意的**：
# 被 OOM Killer SIGKILL 的进程不会执行任何收尾代码，缓冲区里的日志会全部丢掉 ——
# 而"它死在哪一步"恰恰是排查时最需要的信息。

LOG_DIR = os.environ.get("INFERENCE_LOG_DIR", "/var/log/inference")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(f"{LOG_DIR}/server.log")],
    force=True,
)
LOG = logging.getLogger("inference")

MODEL_DIR = os.environ.get("MODEL_DIR", "/models/local")
KV_CACHE_MB = int(os.environ.get("KV_CACHE_MB", "256"))
PORT = int(os.environ.get("VLLM_PORT", "8000"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "128"))

#: SST-2 的标签顺序（模型 config.json 里 id2label 就是这个顺序）。
LABELS = ["NEGATIVE", "POSITIVE"]

STATE: dict[str, Any] = {}


def allocate_kv_cache(megabytes: int) -> np.ndarray:
    """真实申请并真实写入一块内存（模拟推理引擎预分配的 KV cache）。

    返回值必须被持有：一旦被回收，内存就还回去了，故障也就不会发生。
    """
    size = megabytes * 1024 * 1024
    LOG.info("预分配 KV cache：%d MiB（若限额不足，这里会被 OOM Killer 终止）", megabytes)
    sys.stdout.flush()
    started = time.perf_counter()
    buffer = np.zeros(size, dtype=np.uint8)
    # **必须真写一遍**：只 `zeros` 在某些分配器路径下可能是惰性映射，
    # 不写入就不计入 RSS，cgroup 也就看不到超限。
    buffer.fill(1)
    LOG.info("KV cache 就绪：%d MiB 常驻，耗时 %.1fs", megabytes, time.perf_counter() - started)
    sys.stdout.flush()
    return buffer


def softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - logits.max(axis=-1, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=-1, keepdims=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动顺序刻意是"先 KV cache、后模型"。

    反过来也能跑，但故障场景下日志会停在"模型加载成功"那一行，
    读起来像"启动没问题却莫名被杀"。先申请 KV cache，最后一行日志就直接指出死因。
    """
    LOG.info("启动：MODEL_DIR=%s KV_CACHE_MB=%d", MODEL_DIR, KV_CACHE_MB)

    STATE["kv_cache"] = allocate_kv_cache(KV_CACHE_MB)

    model_path = os.path.join(MODEL_DIR, "model_quantized.onnx")
    tokenizer_path = os.path.join(MODEL_DIR, "tokenizer.json")
    LOG.info("加载模型：%s", model_path)
    sys.stdout.flush()
    if not os.path.exists(model_path):
        raise RuntimeError(f"缺少模型文件 {model_path}；镜像构建时 bake_model.py 失败了？")

    # 只用 CPU：这台机器上没有 GPU，显式指定可以避免 onnxruntime 去探测 CUDA 而卡住。
    STATE["session"] = ort.InferenceSession(
        model_path, providers=["CPUExecutionProvider"]
    )
    STATE["inputs"] = {i.name for i in STATE["session"].get_inputs()}
    STATE["output"] = STATE["session"].get_outputs()[0].name

    tokenizer = Tokenizer.from_file(tokenizer_path)
    tokenizer.enable_truncation(max_length=MAX_TOKENS)
    STATE["tokenizer"] = tokenizer

    LOG.info(
        "模型就绪：输入 %s，输出 %s，开始监听 :%d",
        sorted(STATE["inputs"]),
        STATE["output"],
        PORT,
    )
    sys.stdout.flush()
    yield
    STATE.clear()


app = FastAPI(title="Crosslayer 样例推理服务", version="2.0.0", lifespan=lifespan)


class InferRequest(BaseModel):
    """推理请求。`loadgen.sh` 按这个形状发。"""

    text: str = "Crosslayer correlates signals across layers."


@app.get("/health")
def health() -> dict[str, Any]:
    """容器"活着"不等于"能推理"，所以这个端点只有在模型真的就绪后才返回 ok。"""
    ready = "session" in STATE
    return {
        "status": "ok" if ready else "loading",
        "model": "distilbert-base-uncased-finetuned-sst-2-english (onnx, quantized)",
        "kvCacheMb": KV_CACHE_MB,
        "ready": ready,
    }


@app.post("/v1/infer")
def infer(request: InferRequest) -> dict[str, Any]:
    if "session" not in STATE:
        raise HTTPException(status_code=503, detail="模型尚未就绪")

    started = time.perf_counter()
    encoded = STATE["tokenizer"].encode(request.text)

    # ONNX 的输入名由模型决定，不硬编码 —— 换模型时这里不该跟着改，
    # 否则"换模型"就变成了"改代码"，而改代码就可能改错。
    feed: dict[str, np.ndarray] = {}
    if "input_ids" in STATE["inputs"]:
        feed["input_ids"] = np.array([encoded.ids], dtype=np.int64)
    if "attention_mask" in STATE["inputs"]:
        feed["attention_mask"] = np.array([encoded.attention_mask], dtype=np.int64)
    if not feed:
        raise HTTPException(
            status_code=500,
            detail=f"模型输入名不认识：{sorted(STATE['inputs'])}",
        )

    logits = STATE["session"].run([STATE["output"]], feed)[0]
    probabilities = softmax(np.asarray(logits, dtype=np.float64))[0]
    best = int(probabilities.argmax())
    elapsed = time.perf_counter() - started

    LOG.info(
        "推理完成：tokens=%d label=%s 耗时 %.3fs", len(encoded.ids), LABELS[best], elapsed
    )
    return {
        "label": LABELS[best],
        "score": round(float(probabilities[best]), 6),
        "probabilities": {
            label: round(float(prob), 6) for label, prob in zip(LABELS, probabilities, strict=True)
        },
        "tokens": len(encoded.ids),
        "elapsed_seconds": round(elapsed, 4),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
