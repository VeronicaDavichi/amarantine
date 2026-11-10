#!/usr/bin/env python3
"""从 wrapper.node 静态定位 `nt_sqlite3_key_v2`，再用 frida 读口令（spec 040）。

## 为什么不用导出表

QQ 9.9.37 的 `wrapper.node` 只导出 66 个 C++ 符号，**没有** `nt_sqlite3_key_v2`
（实测 `enumerateExports` 无命中；SQLite 是静态链接的）。

但函数里有一条日志字符串：

    nt_sqlite3_key_v2: db=%p zDb=%s

所以定位方法是：**找字符串 → 找引用它的 LEA → 用 .pdata 反查该 LEA 所属函数的 RVA**。
这正是 [qq-win-db-key](https://github.com/QQBackup/qq-win-db-key) 的做法；
这里用 Python 重写，抓取交给 frida（不重启调试器也能挂）。

## 用法

    # 1) 只做静态分析（不需要管理员）
    py -3.12 harness/qq_key_frida.py --analyze

    # 2) 挂到已登录的 QQ（管理员），等它开库
    py -3.12 harness/qq_key_frida.py --pid 28072 --timeout 60

`wrapper.node` 里该函数的第 3 个参数（x64 的 R8）就是口令指针。
"""

from __future__ import annotations

import argparse
import pathlib
import struct
import sys
import time

#: 目标函数里的日志字符串
NEEDLE = b"nt_sqlite3_key_v2: db=%p zDb=%s"

DEFAULT_WRAPPER = (
    "D:/Program Files/Tencent/QQNT/versions/9.9.37-54395/resources/app/wrapper.node"
)


def _u16(b: bytes, off: int) -> int:
    return struct.unpack_from("<H", b, off)[0]


def _u32(b: bytes, off: int) -> int:
    return struct.unpack_from("<I", b, off)[0]


def _u64(b: bytes, off: int) -> int:
    return struct.unpack_from("<Q", b, off)[0]


def _i32(b: bytes, off: int) -> int:
    return struct.unpack_from("<i", b, off)[0]


def analyze(path: pathlib.Path) -> dict:
    """返回 {string_rva, lea_rva, function_rva}。"""
    data = path.read_bytes()
    if _u16(data, 0) != 0x5A4D:
        raise SystemExit("不是 PE 文件")
    pe = _u32(data, 0x3C)
    if _u32(data, pe) != 0x00004550:
        raise SystemExit("PE 签名不对")

    coff = pe + 4
    n_sections = _u16(data, coff + 2)
    size_opt = _u16(data, coff + 16)
    opt = coff + 20
    if _u16(data, opt) != 0x20B:
        raise SystemExit("只支持 PE32+")

    # 数据目录：异常表（.pdata）下标 3
    data_dirs = opt + 112
    exc_rva = _u32(data, data_dirs + 3 * 8)
    exc_size = _u32(data, data_dirs + 3 * 8 + 4)

    sections: dict[str, dict] = {}
    sec_off = opt + size_opt
    for i in range(n_sections):
        off = sec_off + i * 40
        name = data[off:off + 8].rstrip(b"\x00").decode("ascii", "ignore")
        sections[name] = {
            "va": _u32(data, off + 12),
            "vsize": _u32(data, off + 8),
            "raw_ptr": _u32(data, off + 20),
            "raw_size": _u32(data, off + 16),
        }

    rdata = sections.get(".rdata")
    if not rdata:
        raise SystemExit("没有 .rdata")
    start = rdata["raw_ptr"]
    hit = data.find(NEEDLE, start, start + rdata["raw_size"])
    if hit < 0:
        raise SystemExit("没找到目标字符串（QQ 版本可能变了）")
    string_rva = rdata["va"] + (hit - start)

    text = sections.get(".text")
    if not text:
        raise SystemExit("没有 .text")
    t_start, t_size, t_va = text["raw_ptr"], text["raw_size"], text["va"]

    lea_rva = None
    i = t_start + 1
    end = t_start + t_size - 7
    while i < end:
        if data[i] == 0x8D and (data[i - 1] & 0xF8) == 0x48 and (data[i + 1] & 0xC7) == 0x05:
            disp = _i32(data, i + 2)
            instr_rva = t_va + (i - 1 - t_start)
            if instr_rva + 7 + disp == string_rva:
                lea_rva = instr_rva
                break
        i += 1
    if lea_rva is None:
        raise SystemExit("没找到引用该字符串的 LEA 指令")

    # 在 .pdata 里二分找函数起点
    pdata = None
    for sec in sections.values():
        if sec["va"] <= exc_rva < sec["va"] + sec["vsize"]:
            pdata = sec
            break
    if not pdata:
        raise SystemExit("异常表所在 section 找不到")
    base = pdata["raw_ptr"] + (exc_rva - pdata["va"])
    n = exc_size // 12
    lo, hi = 0, n - 1
    func_rva = None
    while lo <= hi:
        mid = (lo + hi) // 2
        off = base + mid * 12
        begin, stop = _u32(data, off), _u32(data, off + 4)
        if lea_rva < begin:
            hi = mid - 1
        elif lea_rva >= stop:
            lo = mid + 1
        else:
            func_rva = begin
            break
    if func_rva is None:
        raise SystemExit("没找到 LEA 所属函数")

    return {"string_rva": string_rva, "lea_rva": lea_rva, "function_rva": func_rva,
            "leaves": list(sections)}


HOOK = r"""
const targetRva = %(rva)d;
let attached = false;

function tryHook() {
  const mods = Process.enumerateModules().filter(m => /wrapper/i.test(m.name));
  if (!mods.length) return false;
  const addr = mods[0].base.add(targetRva);
  send({ stage: "hooked", base: mods[0].base.toString(), addr: addr.toString() });
  Interceptor.attach(addr, {
    onEnter(args) {
      try {
        // x64: rcx=db, rdx=zDb, r8=pKey, r9d=nKey
        const keyPtr = args[2];
        const nKey = args[3].toInt32();
        const key = keyPtr.readUtf8String(Math.max(0, Math.min(nKey, 128)));
        send({ stage: "key", key: key, nKey: nKey });
      } catch (e) {
        send({ stage: "error", msg: String(e) });
      }
    },
  });
  attached = true;
  return true;
}

if (!tryHook()) {
  const iv = setInterval(() => { if (tryHook()) clearInterval(iv); }, 500);
}
"""


def run_frida(rva: int, pid: int, timeout: int, out: pathlib.Path) -> int:
    import frida

    device = frida.get_local_device()
    session = device.attach(pid)
    found: dict[str, str] = {}

    def on_message(msg, data):
        payload = msg.get("payload") or {}
        stage = payload.get("stage")
        if stage in ("hooked", "error"):
            print(f"[{stage}] {payload}")
        if stage == "key":
            print(f"[key] {payload.get('key')!r} (nKey={payload.get('nKey')})")
            found["key"] = payload["key"]

    script = session.create_script(HOOK % {"rva": rva})
    script.on("message", on_message)
    script.load()
    try:
        deadline = time.time() + timeout
        while time.time() < deadline and not found:
            time.sleep(0.5)
    finally:
        try:
            script.unload()
            session.detach()
        except Exception:  # noqa: BLE001
            pass

    if not found:
        print("超时：没等到开库调用。试试先点开一个聊天，或重启 QQ。")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(found["key"], encoding="utf-8")
    print(f"\n✓ 口令已保存：{out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="定位并 hook QQ SQLCipher 口令")
    ap.add_argument("--wrapper", default=DEFAULT_WRAPPER)
    ap.add_argument("--analyze", action="store_true")
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--out", default=str(pathlib.Path.home() / ".qq-mcp" / "qqkey"))
    args = ap.parse_args()

    info = analyze(pathlib.Path(args.wrapper))
    print(f"字符串 RVA: 0x{info['string_rva']:X}")
    print(f"LEA 指令 RVA: 0x{info['lea_rva']:X}")
    print(f"函数 RVA: 0x{info['function_rva']:X}")
    if args.analyze:
        return 0
    if not args.pid:
        print("需要 --pid（或用 --analyze 只看静态结果）")
        return 2
    return run_frida(info["function_rva"], args.pid, args.timeout,
                     pathlib.Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
