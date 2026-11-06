"""实测 recall 冷查询延迟，对比 COLD_QUERY_P95_MS（spec 028）。

用法：
    python ../harness/measure_recall_latency.py                # 默认 30 次
    python ../harness/measure_recall_latency.py --n 50
    python ../harness/measure_recall_latency.py --budget 800

## 测的是什么

「暖启动后的冷 query」—— 进程已起、连接池已建、模型已加载，
但**每个 query 都是新的**（不复用同一个 query，避免 Weaviate / embedding
侧的缓存把第二次查变成热查）。

**不是**「进程冷启动的第一查」。那一次的开销（连接握手、模型加载）
每次部署只发生一次，拿它当 P95 会误导。

## 为什么必须先 warmup

第一次调用要付连接池建立、DNS、embedding 模型预热的一次性成本。
不丢弃这一次，P95 会被它拉偏（尤其 N 小的时候）。
"""

from __future__ import annotations

import argparse
import statistics
import time

import httpx

from memory.config import load_settings
from memory.services.recall import recall

#: 兜底 query（从库里取不到 summary 时用）
FALLBACK_QUERIES = [
    "张三最近在做什么",
    "项目A的进展",
    "李四说了什么关于项目B",
    "周会上讨论了什么",
    "预算分配方案定了吗",
    "上次说的那个项目延期了没有",
    "谁负责新项目",
    "六月的计划",
    "明年的预算",
    "最近的会议记录",
]


def load_queries(settings, limit: int = 30) -> list[str]:
    """从 work namespace 的真实卡片里造 query，不够就用兜底补齐。"""
    base = settings.WEAVIATE_URL.rstrip("/")
    q = """
    {
      Get {
        MemoryEvent(
          limit: 60
          where: {path: ["namespace"], operator: Equal, valueText: "work"}
        ) { summary entity_names }
      }
    }
    """
    queries: list[str] = []
    try:
        r = httpx.post(f"{base}/v1/graphql", json={"query": q}, timeout=20)
        objs = r.json().get("data", {}).get("Get", {}).get("MemoryEvent", []) or []
        for o in objs:
            summary = (o.get("summary") or "").strip()
            if not summary or "冒烟测试" in summary:
                continue  # 冒烟测试标记不是有意义的内容
            queries.append(summary)
            for ent in o.get("entity_names") or []:
                if ent and len(ent) >= 2:
                    queries.append(f"{ent} 相关的事")
    except Exception as exc:  # noqa: BLE001
        print(f"  [警告] 取真实 query 失败，用兜底: {exc}")

    # 去重保序
    seen: dict[str, None] = {}
    for x in queries:
        seen.setdefault(x, None)
    queries = list(seen)

    for x in FALLBACK_QUERIES:
        if len(queries) >= limit:
            break
        if x not in seen:
            queries.append(x)
            seen[x] = None
    return queries[:limit]


def percentile(values: list[float], p: float) -> float:
    """线性插值分位数（和 numpy.percentile 默认行为一致）。"""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def main() -> int:
    ap = argparse.ArgumentParser(description="实测 recall 冷查询延迟")
    ap.add_argument("--n", type=int, default=30, help="测量次数（默认 30）")
    ap.add_argument(
        "--budget",
        type=int,
        default=None,
        help="覆盖 RECALL_BUDGET_MS（默认用配置值）",
    )
    args = ap.parse_args()

    settings = load_settings()
    limit_ms = settings.COLD_QUERY_P95_MS
    budget = args.budget if args.budget is not None else settings.RECALL_BUDGET_MS

    print("=" * 72)
    print("recall 冷查询延迟实测")
    print("=" * 72)
    print(f"  namespace          : work")
    print(f"  COLD_QUERY_P95_MS  : {limit_ms} ms  (目标)")
    print(f"  RECALL_BUDGET_MS   : {budget} ms  (运行时预算)")
    print(f"  embedding model    : {settings.EMBEDDING_MODEL}")
    print(f"  测量次数            : {args.n}")
    print()

    queries = load_queries(settings, limit=max(args.n, 10))
    print(f"  准备了 {len(queries)} 个不同的 query")
    print()

    # ---- warmup ----
    print("warmup（丢弃，用来付连接池/模型预热的一次性开销）...")
    t = time.monotonic()
    try:
        r = recall(
            queries[0],
            namespace="work",
            settings=settings,
            budget_ms=budget,
        )
        warm_ms = (time.monotonic() - t) * 1000
        print(f"  warmup 耗时 {warm_ms:.0f} ms，返回 {len(r.materials)} 条")
    except Exception as exc:  # noqa: BLE001
        print(f"  warmup 失败: {exc}")
        return 1
    print()

    # ---- 正式测量 ----
    print(f"测量 {args.n} 次（每次不同 query）...")
    samples: list[float] = []
    channel_samples: dict[str, list[float]] = {}
    n_materials: list[int] = []
    partial_count = 0
    degraded_count = 0
    errors: list[str] = []

    for i in range(args.n):
        q = queries[i % len(queries)]
        try:
            res = recall(q, namespace="work", settings=settings, budget_ms=budget)
            samples.append(res.elapsed_ms)
            for k, v in res.channel_ms.items():
                channel_samples.setdefault(k, []).append(v)
            n_materials.append(len(res.materials))
            if res.partial:
                partial_count += 1
            if res.degraded:
                degraded_count += 1
            print(
                f"  [{i+1:>2}/{args.n}] {res.elapsed_ms:>7.1f} ms  "
                f"材料 {len(res.materials):>2}  "
                f"{'partial ' if res.partial else ''}"
                f"{'degraded' if res.degraded else ''}"
                f"  {q[:24]}"
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
            print(f"  [{i+1:>2}/{args.n}] 失败 {exc}")

    if not samples:
        print("\n没有任何成功样本")
        return 1

    # ---- 统计 ----
    print()
    print("=" * 72)
    print("结果")
    print("=" * 72)
    p50 = percentile(samples, 50)
    p95 = percentile(samples, 95)
    p99 = percentile(samples, 99)
    print(f"  样本数   : {len(samples)}（失败 {len(errors)}）")
    print(f"  min      : {min(samples):>8.1f} ms")
    print(f"  均值     : {statistics.mean(samples):>8.1f} ms")
    print(f"  P50      : {p50:>8.1f} ms")
    print(f"  P95      : {p95:>8.1f} ms   <== 与目标 {limit_ms} ms 对比")
    print(f"  P99      : {p99:>8.1f} ms")
    print(f"  max      : {max(samples):>8.1f} ms")
    print()

    verdict = "达成" if p95 <= limit_ms else "未达成"
    diff = p95 - limit_ms
    print(f"  P95 {verdict}：{p95:.1f} ms vs 目标 {limit_ms} ms "
          f"（{'超出' if diff > 0 else '余量'} {abs(diff):.1f} ms）")
    if limit_ms > 0 and p95 > limit_ms:
        ratio = p95 / limit_ms
        print(f"  即目标的 {ratio:.2f} 倍")
    print()

    print("  分通道耗时（并行，不可相加）：")
    for k in sorted(channel_samples):
        vals = channel_samples[k]
        print(
            f"    {k:<8} P50={percentile(vals, 50):>7.1f}  "
            f"P95={percentile(vals, 95):>7.1f}  max={max(vals):>7.1f} ms"
        )
    print()

    print("  结果质量：")
    print(f"    平均材料数 : {statistics.mean(n_materials):.1f}")
    print(f"    空结果     : {sum(1 for x in n_materials if x == 0)} / {len(n_materials)}")
    print(f"    partial    : {partial_count} / {len(samples)}")
    print(f"    degraded   : {degraded_count} / {len(samples)}")
    if errors:
        print(f"    错误       : {len(errors)}")
        for e in errors[:3]:
            print(f"      {e}")

    print()
    print("=" * 72)
    print("说明")
    print("=" * 72)
    print("  · 测的是「暖启动后的冷 query」，不含进程冷启动的首查开销")
    print("  · partial 表示有通道撞了 RECALL_BUDGET_MS 被截断，")
    print("    它的耗时被预算压在 budget 附近，不代表通道真实速度")
    print("  · 本次只测量，不改 COLD_QUERY_P95_MS —— 单次测量不足以定新目标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
