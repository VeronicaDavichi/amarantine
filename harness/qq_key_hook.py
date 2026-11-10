#!/usr/bin/env python3
"""用 frida hook QQ 的 `nt_sqlite3_key_v2`，抓 SQLCipher 口令（spec 040 兜底方案）。

## 为什么需要它

`qq-mcp` 的 `capture_offline_key` 靠**扫内存里的短可打印串**猜口令。
实测 QQ 9.9.37 上不行：7 个进程、每个约 40 万候选，全部解不开
（候选窗口 12–32 字节，可能对不上这个版本的存法）。

更可靠的办法是直接 hook SQLCipher 的 key 设置函数，**它拿到的就是口令原文**：

    int nt_sqlite3_key_v2(sqlite3 *db, const char *zDb, const void *pKey, int nKey)

## 用法（要管理员权限）

    # 1) 挂到已登录的 QQ 主进程，等它下次开库
    py -3.12 harness/qq_key_hook.py --pid 28072 --timeout 60

    # 2) 抓不到就重启 QQ（frida 在启动阶段就注入，登录时必然触发）
    py -3.12 harness/qq_key_hook.py --spawn "D:/Program Files/Tencent/QQNT/QQ.exe" --timeout 120

抓到后写到 `~/.qq-mcp/qqkey`，`export_qq.mjs` 就能直接用。
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

import frida

HOOK = r"""
const mods = Process.enumerateModules().filter(m => /wrapper/i.test(m.name));
send({ stage: "modules", names: mods.map(m => m.name) });
if (!mods.length) { send({ stage: "error", msg: "no wrapper module" }); }
else {
  const target = mods[0].findExportByName("nt_sqlite3_key_v2");
  send({ stage: "export", found: !!target, addr: target ? target.toString() : null });
  if (target) {
    Interceptor.attach(target, {
      onEnter(args) {
        try {
          const nKey = args[3].toInt32();
          const key = args[2].readUtf8String(Math.max(0, Math.min(nKey, 128)));
          send({ stage: "key", nKey: nKey, key: key });
        } catch (e) {
          send({ stage: "error", msg: String(e) });
        }
      },
    });
    send({ stage: "ready" });
  }
}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="frida hook QQ SQLCipher 口令")
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--spawn", default="", help="QQ.exe 路径（重启方式）")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--out", default=str(pathlib.Path.home() / ".qq-mcp" / "qqkey"))
    args = ap.parse_args()

    if not args.pid and not args.spawn:
        print("需要 --pid 或 --spawn")
        return 2

    device = frida.get_local_device()
    found: dict[str, str] = {}

    def on_message(message, data):
        payload = message.get("payload") or {}
        stage = payload.get("stage")
        if stage in ("modules", "export", "ready", "error"):
            print(f"[{stage}] {payload}")
        if stage == "key" and payload.get("key"):
            found["key"] = payload["key"]
            print(f"[key] {payload['key']!r} (nKey={payload.get('nKey')})")

    script = None
    session = None
    pid = args.pid
    try:
        if args.spawn:
            pid = device.spawn([args.spawn])
            print(f"spawned pid={pid}")
            session = device.attach(pid)
            script = session.create_script(HOOK)
            script.on("message", on_message)
            script.load()
            device.resume(pid)
        else:
            session = device.attach(pid)
            script = session.create_script(HOOK)
            script.on("message", on_message)
            script.load()

        deadline = time.time() + args.timeout
        while time.time() < deadline and not found:
            time.sleep(0.5)
        if not found:
            print("超时：这次没抓到口令。试试 --spawn 重启 QQ，或先点开一个聊天再试。")
            return 1
        out = pathlib.Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(found["key"], encoding="utf-8")
        print(f"\n✓ 口令已保存：{out}")
        return 0
    finally:
        try:
            if script is not None:
                script.unload()
            if session is not None:
                session.detach()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
