"""meta-test：证明 spec 026 的核心测试真的能抓住错误。

验两类：
  ① 规则层没接线   -> 「规则命中不调 LLM」必须失败
  ② 规则顺序错乱   -> 「规则 1 必须在规则 3 之前」必须失败

第 ② 类尤其重要：规则顺序写错时，功能「看起来有」，只在特定输入上出错，
是最难靠人眼发现的一类 bug。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

BE = pathlib.Path("/mnt/e/PycharmProjects/amarantine/back_end")
RESOLVER = BE / "src/memory/services/alias_resolver.py"
MATCH = BE / "src/memory/services/entity_match.py"


def run(kexpr: str) -> int:
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "../back_end/tests/test_entity_match.py",
            "../back_end/tests/test_alias_prefix.py",
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


def check(name: str, path: pathlib.Path, old: str, new: str, kexpr: str) -> bool:
    original = path.read_text()
    assert old in original, f"{name}: 锚点没找到"

    print(f"=== {name} ===")
    print("  ① 现状：")
    ok = run(kexpr)

    print("  ② 注入错误：")
    path.write_text(original.replace(old, new, 1))
    try:
        broken = run(kexpr)
    finally:
        path.write_text(original)

    good = ok == 0 and broken != 0
    print(f"  {'✓ 能抓住' if good else '✗ 抓不住，测试是空转的'}")
    print()
    return good


def main() -> None:
    results = []

    # ① 规则层未接线：直接从 resolve_one 里删掉 best_candidate 调用
    results.append(
        check(
            "规则层未接线（删掉 best_candidate 调用）",
            RESOLVER,
            "    hit = best_candidate(claim.alias, allowed, settings=settings)",
            "    hit = None  # 故意不接线",
            "test_rule_layer_skips_llm_on_suffix_hit",
        )
    )

    # ② 顺序错乱：把单字前缀规则从规则 1 挪到规则 3 之后
    #    做法是把「排除称呼的前缀判定」改成永远返回 False（等于禁用规则 1）
    results.append(
        check(
            "规则顺序错乱（禁用规则 1）",
            MATCH,
            '''    if _prefix_conflict_excluding_honorific(
        a, b, honorifics
    ) or _prefix_conflict_excluding_honorific(b, a, honorifics):''',
            "    if False:  # 故意禁用规则 1，模拟顺序错误",
            "test_prefix_rule_beats_suffix_whitelist or test_single_char_prefix_is_different",
        )
    )

    # ③ 后缀白名单被写成「谁包含谁就合并」
    results.append(
        check(
            "后缀规则过宽（不看白名单）",
            MATCH,
            "    return tail if tail in suffixes else None",
            "    return tail  # 故意不看白名单",
            "test_suffix_not_in_whitelist_is_unknown",
        )
    )

    print("=== 总结 ===")
    print(f"  {sum(results)}/{len(results)} 条测试被证明有效")
    if all(results):
        print("  ✓ 能区分「规则对了」和「规则错了」")
    else:
        print("  ✗ 有测试是空转的")


if __name__ == "__main__":
    main()
