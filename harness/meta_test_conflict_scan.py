"""meta-test：证明 spec 033 的震荡扫描测试能抓住两类错误。

① 筛掉前缀关系（改成永远 True）-> 「可扫描 1 对」的测试必须失败
② 震荡判定改成「看 ruling 而不是 rule」-> 造震荡的测试必须失败
   （因为 UNKNOWN 也可能来自「无规则命中」，不能只看 ruling）
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

BE = pathlib.Path("/mnt/e/PycharmProjects/amarantine/back_end")
HARNESS = pathlib.Path("/mnt/e/PycharmProjects/amarantine/harness")
CDH = HARNESS / "check_data_health.py"


def _purge_pycache() -> None:
    for d in list(BE.rglob("__pycache__")) + list(HARNESS.rglob("__pycache__")):
        if ".venv" in str(d):
            continue
        for f in d.glob("*.pyc"):
            f.unlink(missing_ok=True)


def run(kexpr: str) -> int:
    _purge_pycache()
    r = subprocess.run(
        [sys.executable, "-m", "pytest",
         "../back_end/tests/test_conflict_scan.py",
         "-q", "--tb=no", "-k", kexpr],
        cwd=str(BE), capture_output=True, text=True,
    )
    lines = (r.stdout or "").strip().splitlines()
    print("    " + (lines[-1] if lines else f"exit={r.returncode}"))
    return r.returncode


def check(name: str, old: str, new: str, kexpr: str) -> bool:
    original = CDH.read_text()
    assert old in original, f"{name}: 锚点没找到"

    print(f"=== {name} ===")
    print("  ① 现状：")
    ok = run(kexpr)

    print("  ② 注入错误：")
    CDH.write_text(original.replace(old, new, 1))
    try:
        assert new in CDH.read_text(), "注入没写进去"
        broken = run(kexpr)
    finally:
        CDH.write_text(original)
        _purge_pycache()

    good = ok == 0 and broken != 0
    print(f"  {'✓ 能抓住' if good else '✗ 抓不住，测试是空转的'}")
    print()
    return good


def main() -> None:
    results = []

    # ① 前缀筛选失效（永远 True）-> 「可扫描 1 对」的断言会失败
    #    （因为不相关的对也被算进来，可扫描数变了）
    results.append(
        check(
            "前缀筛选失效（永远返回 True）",
            "    return a.startswith(b) or b.startswith(a)",
            "    return True  # 注入：不筛了",
            "finds_zero_on_related_but_not_conflicting or reports_total_combinations",
        )
    )

    # ② 震荡判定改成看 ruling（UNKNOWN 也算）-> 造震荡那条会失败
    #    因为「无规则命中」也是 UNKNOWN，不能混为一谈
    results.append(
        check(
            "震荡判定只看 ruling（UNKNOWN 也算震荡）",
            '        if r.rule == "conflict":',
            '        if r.ruling == "unknown":  # 注入：把「没命中」也当震荡',
            "finds_conflict_with_broken_config",
        )
    )

    print("=== 总结 ===")
    print(f"  {sum(results)}/{len(results)} 条测试被证明有效")
    if all(results):
        print("  ✓ 能区分「筛对了」和「没筛」，也能区分「真震荡」和「没命中」")
    else:
        print("  ✗ 有测试是空转的")


if __name__ == "__main__":
    main()
