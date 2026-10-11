"""构建期把推理模型固化进镜像（赛题交付要求 d 的配套件）。

## 为什么用 ONNX Runtime 而不是 PyTorch / vLLM

本目录最早的版本写的是 `pip install vllm==0.8.5`。实测结论：

| 渠道 | 在**无 GPU** 机器上的可用性 |
|---|---|
| `vllm`（PyPI） | ❌ 只有 CUDA wheel，依赖整套 `nvidia-*`；无 GPU 时 `import vllm` 失败 |
| `torch`（PyPI） | ⚠️ 是 CUDA 构建（约 766 MB + 数 GB `nvidia-*` 依赖） |
| `torch`（`download.pytorch.org/whl/cpu`） | ❌ 索引可达，但 wheel 落在 `download-r2.pytorch.org` —— **实测容器内网络不可达** |
| **`onnxruntime` + 预训练 ONNX** | ✅ 约 15 MB，纯 CPU，PyPI 可达 |

赛题明文要求「缺特定 GPU 硬件时也能复现实验流程」，所以默认路径必须是**确定可跑**的。
选 ONNX Runtime 不是退而求其次：它是**同一个预训练模型**（DistilBERT 在 SST-2 上微调）
的推理后端之一，只是运行时不带 CUDA。有 GPU 的 VM 上把本文件换成 vLLM 即可，
观测接口不变（见 `docs/REQUIREMENTS_GAP.md`）。

## 为什么要校验 SHA256

模型是**外部输入**。不校验哈希的话，"同一镜像 tag 两次运行结果一致"
（赛题「稳定性与可复现性 15%」）就只是一句声明：上游换一个权重文件，
本地构建悄悄跟着变，而没有任何东西会告诉你。

校验失败**必须让构建失败**，而不是打条警告继续 —— 一个"权重变了但继续跑"的
推理服务，产出的每一条延迟与错误率都无法与上一次比较。

用法（由 Dockerfile 调用）：

    python bake_model.py <dest-dir> [--endpoint https://huggingface.co]
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys
import urllib.error
import urllib.request

#: 模型仓库（HuggingFace 上的 ONNX 导出）。
REPO = "Xenova/distilbert-base-uncased-finetuned-sst-2-english"

#: 需要固化的文件 → (仓库内路径, SHA256, 说明)。
#:
#: 哈希是**实测**得出的（2026-09-30 经 `hf-mirror.com` 取得），不是从文档抄的。
FILES: dict[str, tuple[str, str, str]] = {
    "model_quantized.onnx": (
        "onnx/model_quantized.onnx",
        "79b45c7fd8c2673aa53615aba07caeb2de798f63a95a2393a365982429c94f7f",
        "动态量化后的 DistilBERT（67.5 MB），CPU 推理延迟约为 fp32 的一半",
    ),
    "tokenizer.json": (
        "tokenizer.json",
        "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
        "WordPiece 分词器（711 KB）—— 用 `tokenizers` 直接加载，不需要 transformers",
    ),
}


def sha256_of(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, dest: pathlib.Path) -> None:
    """流式下载。

    两个细节都是踩出来的，别删：

    1. **必须带 User-Agent**。HuggingFace 的 CDN 会对 `Python-urllib/3.x`
       直接回 **403 Forbidden**，而 `curl` 同样的 URL 却能过 —— 因为 curl 自己
       带了 UA。这个坑的表现极具误导性：报错说"Forbidden"，看起来像权限或
       token 问题，实际上只是没人告诉服务器"我是谁"。
    2. **流式写盘**。一次性 `read()` 会把 67 MB 全放进内存，而演示容器的内存
       限额就是拿来压的 —— 在构建阶段就把自己 OOM 掉，排查方向会被带偏。
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "crosslayer-sample-workload/1.0",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        with dest.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)


def main() -> int:
    parser = argparse.ArgumentParser(description="下载并校验样例推理模型")
    parser.add_argument("dest", help="模型落地目录（镜像内路径）")
    parser.add_argument(
        "--endpoint",
        default="https://huggingface.co",
        help="模型仓库端点。受限网络下可用可信镜像，例如 https://hf-mirror.com",
    )
    args = parser.parse_args()

    dest = pathlib.Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    base = f"{args.endpoint.rstrip('/')}/{REPO}/resolve/main"

    for filename, (remote_path, expected, note) in FILES.items():
        target = dest / filename
        url = f"{base}/{remote_path}"
        print(f"[bake] 下载 {filename} ← {url}", flush=True)

        try:
            download(url, target)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(
                f"[bake] ✗ 下载失败：{exc}\n"
                f"       若本机无法访问 {args.endpoint}，用可信镜像重试：\n"
                f"       docker compose build --build-arg HF_ENDPOINT=https://hf-mirror.com",
                file=sys.stderr,
            )
            return 1

        actual = sha256_of(target)
        if actual != expected:
            # 故意**不**删除坏文件：留着才能比对，而且构建已经失败，不会带进镜像。
            print(
                f"[bake] ✗ {filename} 校验失败\n"
                f"       期望 {expected}\n"
                f"       实际 {actual}\n"
                f"       权重变了。**不要**直接改哈希绕过 —— 先弄清上游为什么变了。",
                file=sys.stderr,
            )
            return 1

        print(f"[bake] ✓ {filename} 校验通过（{target.stat().st_size / 1e6:.1f} MB）—— {note}")

    print(f"[bake] 模型已固化到 {dest}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
