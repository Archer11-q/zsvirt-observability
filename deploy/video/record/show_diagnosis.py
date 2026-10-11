"""把最新一条诊断的要点打印出来（录制演示时按这一条讲）。

## 为什么单独写一个脚本，而不是在录屏里敲一长串 curl

录屏里的命令**越长越容易出错**，而一旦出错就要整段重录。原本的写法是：

    ID=$(curl -s 'localhost:8080/api/v1/diagnoses?limit=1' | ... python -c "import json,...") \
      && curl -s "localhost:8080/api/v1/diagnosis/$ID" | python -m json.tool | head -42

它有三层嵌套引号（shell → python → JSON 键），任何一处转义写错都会在**录制时**
才暴露。把这段逻辑收进一个文件后，录入的命令只有一行：

    python3 deploy/video/record/show_diagnosis.py

顺带一个好处：这个脚本本身就是"怎么看一条诊断"的可用示例，
评审者可以直接读它来了解 API 的返回结构。

用法：
    python3 show_diagnosis.py [--backend http://127.0.0.1:8080] [--index 0]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def get(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"请求失败：{url}\n  {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="http://127.0.0.1:8080")
    parser.add_argument("--index", type=int, default=0, help="取第几条（默认最新一条）")
    args = parser.parse_args()

    base = args.backend.rstrip("/")
    listing = get(f"{base}/api/v1/diagnoses?limit=10").get("data", {}).get("items", [])
    if not listing:
        print("库里还没有诊断结论。先跑一次注入：./deploy/inject_fault.sh container_oom")
        return 1

    target = listing[min(args.index, len(listing) - 1)]
    detail = get(f"{base}/api/v1/diagnosis/{target['id']}").get("data", {})

    line = "─" * 74
    print(line)
    print(f"诊断 {detail.get('id')}")
    print(line)
    print(f"  根因      {detail.get('rootCause')}")
    print(f"  置信      {detail.get('confidence')}   （由 confidenceBreakdown 逐项相加复算）")
    print(f"  规则集    {detail.get('ruleSetVersion')}")
    print()

    breakdown = detail.get("confidenceBreakdown") or []
    if breakdown:
        print("  置信构成")
        for item in breakdown[:8]:
            print(f"    {item.get('ruleId', ''):24} {item.get('contribution')}")
        print()

    advice = detail.get("recommendation") or []
    if advice:
        print("  处置建议")
        for rec in advice:
            print(f"    {rec.get('code', ''):32} {rec.get('text', '')}")
        print()

    scope = detail.get("impactScope") or {}
    print("  影响范围（三集互不重叠 —— 合并了就退化成全量拓扑）")
    for key, label in (
        ("affectedResources", "有证据支持"),
        ("onChain", "过渡层"),
        ("potentiallyAffected", "结构相关但无证据"),
    ):
        values = scope.get(key) or []
        print(f"    {label:22} {len(values):2} 项")
        for value in values[:6]:
            print(f"      · {value}")
    print()

    print("  证据（每一条都能追回原始事件）")
    for evidence in (detail.get("evidence") or [])[:6]:
        source = evidence.get("source")
        print(
            f"    [{evidence.get('type', ''):6}] {evidence.get('name', ''):30}"
            f" 来源={source} 值={evidence.get('value')}"
        )
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
