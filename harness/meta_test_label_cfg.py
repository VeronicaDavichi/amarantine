"""meta-test：证明 spec 025 的两条核心测试真的能抓住「配置没接线」。

做法和 spec 024 一样：把实现改回没接线的状态，测试必须失败。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

BE = pathlib.Path("/mnt/e/PycharmProjects/amarantine/back_end")
LLM = BE / "src/memory/services/llm_labeler.py"
ADM = BE / "src/memory/entrypoints/admin_http.py"
TEST = "../back_end/tests/test_label_config_wiring.py"


def run(kexpr: str) -> int:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "--tb=no", "-k", kexpr],
        cwd=str(BE),
        capture_output=True,
        text=True,
    )
    lines = (r.stdout or "").strip().splitlines()
    print("    " + (lines[-1] if lines else f"exit={r.returncode}"))
    return r.returncode


def check(name: str, path: pathlib.Path, old: str, new: str, kexpr: str) -> None:
    original = path.read_text()
    assert old in original, f"{name}: 找不到锚点"

    print(f"=== {name} ===")
    print("  ① 现状：")
    ok = run(kexpr)

    print("  ② 改回没接线的状态：")
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

    # ① LABEL_LLM_TEMP：删掉传参
    results.append(
        check(
            "LABEL_LLM_TEMP（llm_labeler 不传温度）",
            LLM,
            "                temperature=settings.LABEL_LLM_TEMP,\n",
            "",
            "test_labeler_passes_configured_temp or test_labeler_temp_changes_with_config",
        )
    )

    # ② LABEL_REVIEW_BATCH：端点退回硬编码 50
    results.append(
        check(
            "LABEL_REVIEW_BATCH（端点退回硬编码）",
            ADM,
            "        effective = limit if limit is not None else cfg.LABEL_REVIEW_BATCH",
            "        effective = limit if limit is not None else 50",
            "test_label_review_batch_controls_page_size or test_label_review_batch_changes_with_config",
        )
    )

    print("=== 总结 ===")
    print(f"  {sum(results)}/{len(results)} 条测试被证明有效")
    if all(results):
        print("  ✓ 都能区分「接线了」和「没接线」")
    else:
        print("  ✗ 有测试是空转的，必须重写")


if __name__ == "__main__":
    main()
