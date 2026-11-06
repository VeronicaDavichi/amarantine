"""meta-test：证明 spec 028 的测试能抓住错误。

三类注入：
  ① 删掉 finally 里的告警      -> 告警测试必须失败
  ② elapsed_ms 写死 0          -> 耗时测试必须失败
  ③ 通道计时改回「遍历顺序」    -> 不同值测试必须失败

第 ③ 类是真发生过的 bug：埋点写在 `for name, future in futures.items()` 里，
三个通道拿到同一个值（P50=25.5 / P95=271.3 三个一模一样）。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

BE = pathlib.Path("/mnt/e/PycharmProjects/amarantine/back_end")
RECALL = BE / "src/memory/services/recall.py"


def run(kexpr: str) -> int:
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "../back_end/tests/test_recall_latency.py",
            "-q",
            "--tb=no",
            "-k",
            kexpr,
        ],
        cwd=str(BE),
        capture_output=True,
        text=True,
    )
    lines = (r.stdout or "").strip().splitlines()
    print("    " + (lines[-1] if lines else f"exit={r.returncode}"))
    return r.returncode


def check(name: str, old: str, new: str, kexpr: str) -> bool:
    original = RECALL.read_text()
    assert old in original, f"{name}: 锚点没找到"

    print(f"=== {name} ===")
    print("  ① 现状：")
    ok = run(kexpr)

    print("  ② 注入错误：")
    RECALL.write_text(original.replace(old, new, 1))
    try:
        broken = run(kexpr)
    finally:
        RECALL.write_text(original)

    good = ok == 0 and broken != 0
    print(f"  {'✓ 能抓住' if good else '✗ 抓不住，测试是空转的'}")
    print()
    return good


def main() -> None:
    results = []

    # ① 删掉 finally 里的告警（把阈值判断改成永假）
    results.append(
        check(
            "告警逻辑失效（if 改成 if False）",
            "        if _limit > 0 and _total_ms > _limit:",
            "        if False:  # 故意禁用告警",
            "test_warning_when_over_target or test_elapsed_available_when_channel_raises",
        )
    )

    # ② elapsed_ms 写死 0
    results.append(
        check(
            "耗时埋点失效（elapsed_ms 写死 0）",
            "        result.elapsed_ms = (time.monotonic() - t0) * 1000",
            "        result.elapsed_ms = 0.0  # 故意不测",
            "test_elapsed_ms_is_recorded or test_elapsed_ms_reflects_slow_channel",
        )
    )

    # ③ 通道计时退回「遍历顺序」：
    #    真实 bug 是三个通道在 for 循环里被记录成同一个时刻。
    #    注入时用固定值模拟 —— 第一次尝试是把它改成 `time.monotonic() - t0`，
    #    但 fake 里 embedding 是 0ms，`started - t0 ≈ 0`，行为几乎没变，
    #    测试照样通过。**注入本身也得能真的改变行为，否则 meta-test 也是空转。**
    results.append(
        check(
            "通道计时退回遍历顺序（三个通道同一个值）",
            "                finally:\n                    channel_ms[name] = (time.monotonic() - started) * 1000",
            "                finally:\n                    channel_ms[name] = 42.0  # 错：模拟遍历顺序拿到同一个值",
            "test_channel_timings_are_not_identical",
        )
    )

    print("=== 总结 ===")
    print(f"  {sum(results)}/{len(results)} 条测试被证明有效")
    if all(results):
        print("  ✓ 能区分「埋点对了」和「埋点错了」")
    else:
        print("  ✗ 有测试是空转的")


if __name__ == "__main__":
    main()
