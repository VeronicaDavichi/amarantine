"""meta-test：证明 spec 032 的测试能抓住两类错误。

① 去掉「同义词优先」-> 同义词测试必须失败
② 震荡时改回「取第一票」（旧行为）-> 震荡测试必须失败

第 ② 类尤其重要：如果注入后震荡测试还过，说明它没在测「矛盾被显式化」，
而只是碰巧命中了 UNKNOWN。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

BE = pathlib.Path("/mnt/e/PycharmProjects/amarantine/back_end")
EM = BE / "src/memory/services/entity_match.py"


def _purge_pycache() -> None:
    for d in BE.rglob("__pycache__"):
        if ".venv" in str(d):
            continue
        for f in d.glob("*.pyc"):
            f.unlink(missing_ok=True)


def run(kexpr: str, extra: str = "") -> int:
    _purge_pycache()
    args = [
        sys.executable, "-m", "pytest",
        "../back_end/tests/test_rule_conflict.py",
        "-q", "--tb=no", "-k", kexpr,
    ]
    r = subprocess.run(args, cwd=str(BE), capture_output=True, text=True)
    lines = (r.stdout or "").strip().splitlines()
    print("    " + (lines[-1] if lines else f"exit={r.returncode}"))
    return r.returncode


def check(name: str, old: str, new: str, kexpr: str) -> bool:
    original = EM.read_text()
    assert old in original, f"{name}: 锚点没找到"

    print(f"=== {name} ===")
    print("  ① 现状：")
    ok = run(kexpr)

    print("  ② 注入错误：")
    EM.write_text(original.replace(old, new, 1))
    try:
        assert new in EM.read_text(), "注入没写进去"
        broken = run(kexpr)
    finally:
        EM.write_text(original)
        _purge_pycache()

    good = ok == 0 and broken != 0
    print(f"  {'✓ 能抓住' if good else '✗ 抓不住，测试是空转的'}")
    print()
    return good


def main() -> None:
    results = []

    # ① 去掉同义词优先（退回「按顺序裁决」）
    results.append(
        check(
            "去掉同义词优先",
            '    for rule, reason in same_hits:\n'
            '        if rule == "synonym":\n'
            '            return MatchResult(RULING_SAME, "synonym", reason)',
            "    # 注入：不优先同义词，退回按顺序裁决",
            "synonym_beats_name_structure or synonym_works_both_directions",
        )
    )

    # ② 震荡时改回「取第一票」（旧行为：顺序决定结论）
    results.append(
        check(
            "震荡时取第一票（旧行为）",
            '    return MatchResult(\n'
            '        RULING_UNKNOWN,\n'
            '        "conflict",\n'
            '        f"{a} / {b} 规则矛盾 | "\n'
            '        f"同: {[r for r, _ in same_hits]} "\n'
            '        f"不同: {[r for r, _ in diff_hits]}",\n'
            '    )',
            '    # 注入：震荡时不交 LLM，按旧逻辑取第一票\n'
            '    _rule, _reason = same_hits[0]\n'
            '    return MatchResult(RULING_SAME, _rule, _reason)',
            "conflict_yields_unknown_with_conflict_rule",
        )
    )

    print("=== 总结 ===")
    print(f"  {sum(results)}/{len(results)} 条测试被证明有效")
    if all(results):
        print("  ✓ 能区分「同义词优先」和「按顺序裁决」，也能区分「震荡交 LLM」和「震荡时硬选」")
    else:
        print("  ✗ 有测试是空转的")


if __name__ == "__main__":
    main()
