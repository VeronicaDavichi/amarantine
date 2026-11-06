"""meta-test：证明 spec 031 的测试能抓住「规则 1 又抢跑了」。

注入：把规则 1 的「后缀例外」去掉（回到 bug 状态）
      -> 地名测试必须失败，而「张三/张三丰」测试必须仍然通过。

如果注入后地名测试还过，说明它没在测规则顺序。
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


def run(kexpr: str) -> int:
    _purge_pycache()
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "../back_end/tests/test_entity_match.py",
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


def main() -> None:
    original = EM.read_text()

    # 注入：后缀例外去掉，只保留称呼例外（= 回到 031 修之前的 bug 状态）
    old = "        if extra in honorifics or extra in suffixes:"
    new = "        if extra in honorifics:  # 注入：去掉后缀例外，回到 bug"

    assert old in original, "锚点没找到"

    print("=== 规则 1 去掉「后缀例外」（回到 bug 状态）===")
    print("  ① 现状（两个都应通过）：")
    ok_place = run("place_name_with_suffix_is_same")
    ok_name = run("surname_like_place_does_not_break_real_names")

    print("  ② 注入错误：")
    EM.write_text(original.replace(old, new, 1))
    try:
        assert new in EM.read_text(), "注入没写进去"
        broken_place = run("place_name_with_suffix_is_same")
        still_name = run("surname_like_place_does_not_break_real_names")
    finally:
        EM.write_text(original)
        _purge_pycache()

    print()
    print("=== 判定 ===")
    caught = ok_place == 0 and broken_place != 0
    not_harmed = ok_name == 0 and still_name == 0
    print(f"  {'✓' if caught else '✗'} 地名测试抓得住 bug"
          f"（现状 exit={ok_place}，注入后 exit={broken_place}）")
    print(f"  {'✓' if not_harmed else '✗'} 姓名测试不受影响"
          f"（注入后 exit={still_name}）")

    if caught and not_harmed:
        print("  ✓ 两条测试分别盯住了「修好了」和「没修坏」，互不干扰")
    else:
        print("  ✗ 测试没有正确区分这两件事")

    print()
    print("=== 额外确认：UNKNOWN 而不是 DIFFERENT ===")
    print("  规则层关闭时地名应为 UNKNOWN —— 见")
    print("  test_place_name_degrades_to_unknown_when_rules_off")


if __name__ == "__main__":
    main()
