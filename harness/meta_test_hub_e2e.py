"""meta-test：证明 spec 030 的真图测试能抓住 Cypher 写错。

两类注入：
  ① `(e)--()` 改成 `(e)-->(n)`  -> 反向边测试必须失败
  ② 去掉 `namespace` 过滤         -> 隔离测试必须失败

这两类正是 fake driver 测不出来的东西 —— 它们只错在 Cypher 里，
而 fake driver 直接喂 records，根本不经过 Cypher。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

BE = pathlib.Path("/mnt/e/PycharmProjects/amarantine/back_end")
GS = BE / "src/memory/services/graph_search.py"


def _purge_pycache() -> None:
    """删掉 __pycache__。

    **第一版没删，注入后测试照样通过** —— 改文件太快时 .py 的 mtime
    可能和旧 .pyc 记录的一致，Python 于是用了旧字节码，
    等于「注入了但没生效」。meta-test 自己必须先保证注入真的落地。
    """
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
            "../back_end/tests/test_hub_end_to_end.py",
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
    original = GS.read_text()
    assert old in original, f"{name}: 锚点没找到"

    print(f"=== {name} ===")
    print("  ① 现状：")
    ok = run(kexpr)

    print("  ② 注入错误：")
    GS.write_text(original.replace(old, new, 1))
    try:
        # 读回确认注入真的落地了（写盘了不等于读的是新内容）
        injected = GS.read_text()
        assert new in injected, f"{name}: 注入没写进去"
        # 不再断言 `old not in injected` —— 同一段文本可能在文件里有多处，
        # replace(...,1) 只换第一处。真正该保证的是「新代码在里面」。
        broken = run(kexpr)
    finally:
        GS.write_text(original)
        _purge_pycache()

    good = ok == 0 and broken != 0
    print(f"  {'✓ 能抓住' if good else '✗ 抓不住，测试是空转的'}")
    print()
    return good


def main() -> None:
    results = []

    # ① 度数只数出边。用**完整 Cypher 块**做锚点：
    #    单行 `MATCH (e:Event {namespace: $namespace, event_id: eid})`
    #    在文件里出现 2 次（L408 / L509），replace(...,1) 只换掉一处，
    #    另一处还在 → 「注入后旧代码还在」的断言会失败。
    results.append(
        check(
            "度数只数出边（漏掉入边）",
            "RETURN eid AS event_id, size([(e)--() | 1]) AS degree",
            "RETURN eid AS event_id, size([(e)-->(_) | 1]) AS degree",
            "both_directions",
        )
    )

    # ② 去掉 namespace 过滤。**必须用整块**：
    #    单行 MATCH 在文件里有 2 处（_NEIGHBOR_CYPHER 和 _DEGREE_CYPHER），
    #    只替换一处等于没改到度数用的那条。
    results.append(
        check(
            "去掉 namespace 过滤（跨域串数据）",
            "UNWIND $event_ids AS eid\n"
            "MATCH (e:Event {namespace: $namespace, event_id: eid})\n"
            "RETURN eid AS event_id, size([(e)--() | 1]) AS degree",
            "UNWIND $event_ids AS eid\n"
            "MATCH (e:Event {event_id: eid})\n"
            "RETURN eid AS event_id, size([(e)--() | 1]) AS degree",
            "namespace_scoped",
        )
    )

    print("=== 总结 ===")
    print(f"  {sum(results)}/{len(results)} 条真图测试被证明有效")
    if all(results):
        print("  ✓ 能区分「Cypher 对」和「Cypher 错」—— 这是 fake driver 做不到的")
    else:
        print("  ✗ 有测试是空转的")


if __name__ == "__main__":
    main()
