"""meta-test：证明 test_busy_timeout_follows_config 真的能抓住那个 bug。

做法：把 helper 临时改回「写死 timeout=5.0」，跑测试，必须失败。
如果不失败，说明那条测试是空转的 —— 比没有测试更危险。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

SRC = pathlib.Path(
    "/mnt/e/PycharmProjects/amarantine/back_end/src/memory/services/sqlite_conn.py"
)
TEST = "../back_end/tests/test_sqlite_conn.py"


def run_test() -> int:
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            TEST,
            "-q",
            "--tb=no",
            "-k",
            "test_busy_timeout_follows_config",
        ],
        cwd="/mnt/e/PycharmProjects/amarantine/back_end",
        capture_output=True,
        text=True,
    )
    tail = (r.stdout or "").strip().splitlines()
    print("    " + (tail[-1] if tail else f"exit={r.returncode}"))
    return r.returncode


def main() -> None:
    original = SRC.read_text()

    print("=== ① 现状（配置生效）===")
    rc_ok = run_test()
    print(f"  exit={rc_ok}  {'✓ 通过' if rc_ok == 0 else '✗ 应该通过却没通过'}")

    print()
    print("=== ② 临时改成硬编码 timeout=5.0（模拟修复前的状态）===")
    broken = original.replace(
        """    if timeout_s is not None:
        timeout = float(timeout_s)
    elif settings is not None:
        timeout = max(0.0, float(settings.DB_BUSY_TIMEOUT_MS) / 1000.0)
    else:
        timeout = DEFAULT_TIMEOUT_S""",
        """    timeout = 5.0  # 故意写死，模拟修复前的实现""",
    )
    assert broken != original, "替换失败"
    SRC.write_text(broken)
    try:
        rc_broken = run_test()
        print(f"  exit={rc_broken}  {'✓ 如预期失败' if rc_broken != 0 else '✗ 没抓住 bug！这条测试是空转的'}")
    finally:
        SRC.write_text(original)
        print("  （已恢复）")

    print()
    print("=== 结论 ===")
    if rc_ok == 0 and rc_broken != 0:
        print("  ✓ 这条测试能区分「配置生效」和「硬编码」—— 不是空转")
    else:
        print("  ✗ 测试无效，必须重写")


if __name__ == "__main__":
    main()
