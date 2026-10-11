#!/usr/bin/env bash
# 样例 AI 工作负载的负载生成器（赛题交付要求 d / e 的配套件）
#
# ## 为什么必须有它
#
# 一个「跑着但没人调用」的推理服务几乎不产生信号：没有请求就没有
# `inference.timeout` / `inference.error`，也没有可对比的延迟趋势。
# 而赛题三类场景里「应用/Agent 任务异常」正是靠这些信号表达的。
# 因此**负载是故障场景的一半**，不是可选的压测工具。
#
# ## 三种模式，对应三种演示意图
#
#   ./loadgen.sh steady      持续稳态请求 —— 建立"正常基线"（演示要先给基线）
#   ./loadgen.sh burst       高并发突发   —— 压出资源瓶颈与延迟上升
#   ./loadgen.sh broken      打向一个不通的端口 —— 制造可达性故障
#
# 设计上只用 curl，**不引入任何依赖**（与探针同一原则：最小环境可跑）。

set -uo pipefail

TARGET="${TARGET:-http://127.0.0.1:8000}"
MODE="${1:-steady}"

# 演示用的提示词故意短：让单次推理在 CPU 上也只需几秒，
# 以免演示时长被模型推理本身吃掉。
PROMPT="用一句话说明什么是跨层可观测。"

say() { printf '\033[1m%s\033[0m\n' "$*"; }

request() {
  # `--max-time` 是刻意的：超时**必须**被记录下来。
  # 一个"永远等下去"的压测工具会掩盖超时故障，而超时正是要观测的信号之一。
  curl -sS -o /dev/null -w '%{http_code} %{time_total}s\n' \
    --max-time "${TIMEOUT:-30}" \
    -X POST "${TARGET}/v1/completions" \
    -H 'content-type: application/json' \
    -d "{\"model\":\"${MODEL:-Qwen/Qwen2.5-0.5B-Instruct}\",\"prompt\":\"${PROMPT}\",\"max_tokens\":32}" \
    2>&1 || echo "000 timeout/conn-error"
}

case "${MODE}" in
  steady)
    say "稳态请求：每 2 秒一次，共 ${COUNT:-30} 次（Ctrl-C 停止）"
    say "用途：建立正常基线 —— 演示时先让评委看到'健康长什么样'"
    for i in $(seq 1 "${COUNT:-30}"); do
      printf '%3d  ' "$i"; request
      sleep 2
    done
    ;;

  burst)
    say "突发并发：${CONCURRENCY:-8} 并发 × ${ROUNDS:-5} 轮"
    say "用途：压出资源瓶颈与延迟上升（对应赛题'GPU 或资源瓶颈'一类）"
    for round in $(seq 1 "${ROUNDS:-5}"); do
      printf 'round %d: ' "${round}"
      for _ in $(seq 1 "${CONCURRENCY:-8}"); do
        request &
      done
      wait
      sleep 1
    done
    ;;

  broken)
    # 指向一个**确定不通**的端口，用于制造可达性故障。
    # 默认 9 端口（discard），它在多数系统上无人监听。
    say "可达性故障：打向 ${BROKEN_TARGET:-http://127.0.0.1:9}"
    say "用途：制造 container.network.unreachable / inference.timeout"
    say "预期：连接被拒绝 → 平台上出现可达性告警与跨层根因"
    for i in $(seq 1 "${COUNT:-10}"); do
      printf '%3d  ' "$i"
      curl -sS -o /dev/null -w '%{http_code} %{time_total}s\n' \
        --max-time 3 "${BROKEN_TARGET:-http://127.0.0.1:9}/v1/completions" \
        2>&1 || echo "connection-refused"
      sleep 1
    done
    ;;

  *)
    cat <<'USAGE'
用法：./loadgen.sh [steady|burst|broken]

  steady   持续稳态请求（建立正常基线）      env: COUNT=30
  burst    高并发突发（压出资源瓶颈）        env: CONCURRENCY=8 ROUNDS=5
  broken   打向不通的端口（可达性故障）      env: BROKEN_TARGET=http://127.0.0.1:9

通用 env：
  TARGET   推理服务地址（默认 http://127.0.0.1:8000）
  MODEL    模型名（须与容器内 MODEL_NAME 一致）
  TIMEOUT  单请求超时秒数（默认 30，**超时会被记录而不是被吞掉**）
USAGE
    exit 2
    ;;
esac

say "完成。"
echo "提示：现在去后端看效果 ——"
echo "  curl -s localhost:8080/api/v1/events   | head"
echo "  curl -s localhost:8080/api/v1/alerts   | head"
echo "  curl -s localhost:8080/api/v1/metrics  | head"
