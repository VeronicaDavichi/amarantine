#!/usr/bin/env python3
"""MCP stdio 端到端冒烟测试。

模拟一个 host 真实调用：拉起子进程 → JSON-RPC 握手 → 写入 → 召回 → 退出。
需要 Weaviate / embedding / Neo4j 都在跑（`docker compose ps` 全 healthy）。

用法（仓库根目录）：
    ./harness/dev-linux.sh python ../harness/e2e_mcp.py

退出码 0 表示全绿；任何一条断言失败退出码 1。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, timedelta


def main() -> int:
    env = os.environ.copy()
    env["MEMORY_NAMESPACE"] = "work"
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.Popen(
        [sys.executable, "-m", "memory.entrypoints.mcp_stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        encoding="utf-8",
    )
    assert proc.stdin and proc.stdout

    failures: list[str] = []

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def recv() -> dict:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("stdout 提前 EOF，子进程可能已崩")
        return json.loads(line)

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    try:
        # 1) 握手
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        init = recv()["result"]
        check("initialize", init["serverInfo"]["name"] == "amarantine-memory",
              init["serverInfo"]["name"])
        check("namespace 注入", init["_meta"]["namespace"] == "work")

        # 2) 工具清单
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = [t["name"] for t in recv()["result"]["tools"]]
        expected = {"memory_recall", "memory_remember", "memory_forget", "memory_feedback"}
        check("tools/list 四个工具", expected.issubset(set(names)), str(names))

        # 3) 写入（用日期做唯一后缀，避免撞幂等）
        marker = f"e2e-{os.getpid()}-{date.today().isoformat()}"
        send({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {
                "name": "memory_remember",
                "arguments": {"summary": f"冒烟测试标记 {marker}", "date_hint": "今天"},
            },
        })
        payload = json.loads(recv()["result"]["content"][0]["text"])
        check("remember 返回 event_id", payload.get("created") is True, payload.get("event_id"))

        # 4) 召回
        send({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "memory_recall", "arguments": {"query": "冒烟测试标记"}},
        })
        body = json.loads(recv()["result"]["content"][0]["text"])
        check("recall 有材料", len(body["materials"]) > 0, f"{len(body['materials'])} 条")
        check("recall 无降级", body["degraded"] is False)
        check("trace 带 top_k_decision", "top_k_decision" in body["trace"])

        # 5) namespace 不许当工具参数
        send({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "memory_recall",
                       "arguments": {"query": "x", "namespace": "evil"}},
        })
        check("namespace 参数被拒", recv()["result"].get("isError") is True)

    finally:
        proc.stdin.close()
        proc.wait(timeout=20)

    # 退出码
    check("子进程正常退出", proc.returncode == 0, str(proc.returncode))

    if failures:
        print(f"\nFAILED: {len(failures)} 项 —— {failures}")
        stderr = proc.stderr.read() if proc.stderr else ""
        if stderr:
            print("--- stderr ---")
            print(stderr[-2000:])
        return 1
    print("\nOK: MCP stdio 端到端全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
