"""真库验证：`FORGET_SWEEP_DAYS` 接线后，MaintenanceWorker 真的会扫。

不 mock 任何东西 —— 走真实的 Weaviate + Neo4j + SQLite。
如果这条跑通，说明「配置 → 定时器 → 扫描 → 报告」整条链是通的。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memory.config import load_settings
from memory.services.maintenance import MaintenanceWorker

NS = "work"


def main() -> None:
    s = load_settings()
    print(f"FORGET_SWEEP_DAYS = {s.FORGET_SWEEP_DAYS}")
    print(f"ISLAND_MAX_NEIGHBORS = {s.ISLAND_MAX_NEIGHBORS}")
    print()

    w = MaintenanceWorker(
        NS,
        s,
        reconcile_every_s=1e9,      # 不测对账
        reconcile_on_start=False,
        sweep_on_start=True,        # 第一次 tick 就扫
    )
    print(f"扫描间隔 = {w._sweep_every_s / 86400:.0f} 天")
    print(f"下次扫描 = {w._next_sweep_at}")
    print()

    t0 = datetime.now(timezone.utc)
    report = w.tick(now=t0)
    print("=== 第一次 tick ===")
    if report.sweep is None:
        print("  ✗ 没有扫！接线没生效")
        return
    sw = report.sweep
    print(f"  共 {sw.total} 张")
    print(f"  保护 {sw.protected} / 复核 {sw.need_review} / 可删 {sw.safe_to_delete}")
    print(f"  图降级 = {sw.graph_degraded}")
    print(f"  可删样例 = {sw.sample_ids}")
    print(f"  did_anything = {sw.did_anything}")
    print()

    print("=== 立刻再 tick（没到周期）===")
    report2 = w.tick(now=t0 + timedelta(minutes=1))
    print(f"  sweep = {report2.sweep}")
    if report2.sweep is None:
        print("  ✓ 没重复扫（周期没到）")
    else:
        print("  ✗ 重复扫了！周期判断失效")

    print()
    print("=== 跳过一整个周期 ===")
    report3 = w.tick(now=t0 + timedelta(days=s.FORGET_SWEEP_DAYS + 1))
    if report3.sweep is not None:
        print(f"  ✓ 又扫了一次，共 {report3.sweep.total} 张")
    else:
        print("  ✗ 满周期了却没扫")


if __name__ == "__main__":
    main()
