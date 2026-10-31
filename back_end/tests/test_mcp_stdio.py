"""A5 MCP stdio 协议冒烟测试。

用真实子进程跑，覆盖：握手、工具清单、未知方法、namespace 防护、缺 namespace 拒启动、
以及「日志必须落文件、不进 std」。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

MODULE = "memory.entrypoints.mcp_stdio"

INIT = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
    }
)


def run_mcp(
    lines: list[str],
    namespace: str | None = "agent-a",
    log_file: Path | None = None,
) -> tuple[str, str, int]:
    env = os.environ.copy()
    if namespace is None:
        env.pop("MEMORY_NAMESPACE", None)
    else:
        env["MEMORY_NAMESPACE"] = namespace

    if log_file is None:
        log_file = Path(tempfile.gettempdir()) / "amarantine-test.log"
    env["LOG_FILE"] = str(log_file)

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


def test_initialize_returns_server_info_and_namespace(tmp_path: Path) -> None:
    out, _, code = run_mcp([INIT], log_file=tmp_path / "m.log")
    assert code == 0
    msg = parse(out)[0]
    assert msg["result"]["serverInfo"]["name"] == "amarantine-memory"
    assert msg["result"]["_meta"]["namespace"] == "agent-a"
    assert "memory_recall" in msg["result"]["instructions"]


def test_tools_list_has_four_tools(tmp_path: Path) -> None:
    out, _, _ = run_mcp(
        [INIT, json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})],
        log_file=tmp_path / "m.log",
    )
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


def test_logs_go_to_file_not_std(tmp_path: Path) -> None:
    """stdout 只有协议消息；运行日志落在 LOG_FILE；stderr 保持空。"""
    log_file = tmp_path / "mcp.log"
    out, err, code = run_mcp([INIT], log_file=log_file)

    assert code == 0
    assert all(line.strip().startswith("{") for line in out.splitlines() if line.strip())
    assert err.strip() == "", f"stderr 应为空，实际: {err!r}"

    text = log_file.read_text(encoding="utf-8")
    assert "started" in text
    assert "namespace=agent-a" in text


def test_unknown_method_returns_32601(tmp_path: Path) -> None:
    out, _, _ = run_mcp(
        [json.dumps({"jsonrpc": "2.0", "id": 9, "method": "no/such"})],
        log_file=tmp_path / "m.log",
    )
    assert parse(out)[0]["error"]["code"] == -32601


def test_namespace_argument_rejected(tmp_path: Path) -> None:
    call = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "memory_recall", "arguments": {"query": "x", "namespace": "b"}},
        }
    )
    out, _, _ = run_mcp([call], log_file=tmp_path / "m.log")
    result = parse(out)[0]["result"]
    assert result["isError"] is True
    assert "namespace" in result["content"][0]["text"]


def test_missing_namespace_exits_nonzero(tmp_path: Path) -> None:
    out, err, code = run_mcp([INIT], namespace=None, log_file=tmp_path / "m.log")
    assert code == 1
    assert "MEMORY_NAMESPACE" in err  # 致命错误保留一行 stderr 供 host 感知
    assert out.strip() == ""


def test_invalid_namespace_blocks_start(tmp_path: Path) -> None:
    _, err, code = run_mcp([INIT], namespace="Bad Name", log_file=tmp_path / "m.log")
    assert code != 0
    assert "MEMORY_NAMESPACE" in err


@pytest.mark.parametrize(
    "tool", ["memory_recall", "memory_remember", "memory_forget", "memory_feedback"]
)
def test_unimplemented_tools_report_error_not_crash(tool: str, tmp_path: Path) -> None:
    call = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {}},
        }
    )
    out, _, code = run_mcp([call], log_file=tmp_path / "m.log")
    assert code == 0, "工具未实现不应让进程崩溃"
    assert parse(out)[0]["result"]["isError"] is True
