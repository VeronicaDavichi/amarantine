"""A5 MCP stdio 协议冒烟测试。

用真实子进程跑，覆盖：握手、工具清单、未知方法、namespace 防护、缺 namespace 拒启动。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

MODULE = "memory.entrypoints.mcp_stdio"


def run_mcp(lines: list[str], namespace: str | None = "agent-a") -> tuple[str, str, int]:
    env = os.environ.copy()
    if namespace is None:
        env.pop("MEMORY_NAMESPACE", None)
    else:
        env["MEMORY_NAMESPACE"] = namespace

    proc = subprocess.run(
        [sys.executable, "-m", MODULE],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return proc.stdout, proc.stderr, proc.returncode


def parse(stdout: str) -> list[dict]:
    return [json.loads(ln) for ln in stdout.splitlines() if ln.strip()]


INIT = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
    }
)


def test_initialize_returns_server_info_and_namespace() -> None:
    out, _, code = run_mcp([INIT])
    assert code == 0
    msg = parse(out)[0]
    assert msg["result"]["serverInfo"]["name"] == "amarantine-memory"
    assert msg["result"]["_meta"]["namespace"] == "agent-a"
    assert "memory_recall" in msg["result"]["instructions"]


def test_tools_list_has_four_tools() -> None:
    out, _, _ = run_mcp([INIT, json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})])
    tools = parse(out)[1]["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "memory_recall",
        "memory_remember",
        "memory_forget",
        "memory_feedback",
    }
    for t in tools:
        assert t["inputSchema"]["type"] == "object"
        assert t["inputSchema"]["additionalProperties"] is False


def test_stdout_contains_only_protocol_messages() -> None:
    """日志必须走 stderr，否则 host 会解析失败。"""
    out, err, _ = run_mcp([INIT])
    for line in out.splitlines():
        assert line.strip().startswith("{")
    assert "started" in err


def test_unknown_method_returns_32601() -> None:
    out, _, _ = run_mcp([json.dumps({"jsonrpc": "2.0", "id": 9, "method": "no/such"})])
    assert parse(out)[0]["error"]["code"] == -32601


def test_namespace_argument_rejected() -> None:
    call = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "memory_recall", "arguments": {"query": "x", "namespace": "b"}},
        }
    )
    out, _, _ = run_mcp([call])
    result = parse(out)[0]["result"]
    assert result["isError"] is True
    assert "namespace" in result["content"][0]["text"]


def test_missing_namespace_exits_nonzero() -> None:
    out, err, code = run_mcp([INIT], namespace=None)
    assert code == 1
    assert "MEMORY_NAMESPACE" in err
    assert out.strip() == ""


def test_invalid_namespace_blocks_start() -> None:
    _, err, code = run_mcp([INIT], namespace="Bad Name")
    assert code != 0
    assert "MEMORY_NAMESPACE" in err


@pytest.mark.parametrize("tool", ["memory_recall", "memory_remember", "memory_forget", "memory_feedback"])
def test_unimplemented_tools_report_error_not_crash(tool: str) -> None:
    call = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {}},
        }
    )
    out, _, code = run_mcp([call])
    assert code == 0, "工具未实现不应让进程崩溃"
    assert parse(out)[0]["result"]["isError"] is True
