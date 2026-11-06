"""Windows 侧 TCP 转发：把 WSL 的请求送到 aTrust VPN 后面的网关。

## 为什么需要它

LLM 网关 `198.51.100.58:3000` 只挂在 Windows 的 aTrust VPN 虚拟网卡（`2.0.0.1`）上。
WSL2 默认是 NAT 网络，**不继承 Windows 的路由表**，所以 WSL 里 TCP 完全不通：

    TCP 不通     # bash /dev/tcp 探活失败
    curl 卡到超时

`netsh interface portproxy` 能做同样的事，但要管理员权限；`networkingMode=mirrored`
要 `wsl --shutdown` 重来一遍（容器全停）。这个脚本两样都不用：

    WSL  ──>  198.51.100.1:3001  ──>  198.51.100.58:3000
              (Windows WSL 网卡)        (VPN 后面)

## 用法

Windows 侧：

    python harness/vpn_relay.py                # 默认 0.0.0.0:3001 -> 198.51.100.58:3000
    python harness/vpn_relay.py --listen 3001 --target 198.51.100.58:3000

WSL 侧 `.env`：

    LLM_BASE_URL=http://198.51.100.1:3001

## 已知限制

- **不是持久方案**：进程退出/重启就没了。长期该用 `networkingMode=mirrored`
- 只做裸 TCP 转发，不解析 HTTP，也不看内容
- 如果 Windows 防火墙拦入站，需要在防火墙里放行该端口（脚本不碰防火墙）
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
from datetime import datetime


def _log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def _handle(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
) -> None:
    peer = client_writer.get_extra_info("peername")
    try:
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(target_host, target_port), timeout=10
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"连不上目标 {target_host}:{target_port} <- {peer}: {exc}")
        client_writer.close()
        return

    _log(f"{peer} -> {target_host}:{target_port}")
    await asyncio.gather(
        _pipe(client_reader, up_writer),
        _pipe(up_reader, client_writer),
    )


async def serve(listen_host: str, listen_port: int, target_host: str, target_port: int) -> None:
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, target_host, target_port),
        host=listen_host,
        port=listen_port,
        reuse_address=True,
    )
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets or [])
    _log(f"监听 {addrs}  ->  {target_host}:{target_port}")
    _log("Ctrl+C 停止")
    async with server:
        await server.serve_forever()


def main() -> int:
    p = argparse.ArgumentParser(description="Windows 侧 TCP 转发（给 WSL 用）")
    p.add_argument("--listen-host", default="0.0.0.0")
    p.add_argument("--listen", type=int, default=3001, help="监听端口")
    p.add_argument("--target", default="198.51.100.58:3000", help="目标 host:port")
    args = p.parse_args()

    host, _, port_s = args.target.rpartition(":")
    if not host or not port_s.isdigit():
        print(f"--target 格式应为 host:port，收到 {args.target!r}", file=sys.stderr)
        return 2

    try:
        asyncio.run(serve(args.listen_host, args.listen, host, int(port_s)))
    except KeyboardInterrupt:
        _log("已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
