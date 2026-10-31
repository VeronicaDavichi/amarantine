"""A5 MCP stdio 协议冒烟测试。

用真实子进程跑，覆盖：握手、工具清单、未知方法、namespace 防护、缺 namespace 拒启动、
以及「日志必须落文件、不进 std」。
"""

from __future__ import annotations

import inspect
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
    """缺 namespace：退出码 1，stdout/stderr 全空，原因只写日志文件。"""
    log_file = tmp_path / "m.log"
    out, err, code = run_mcp([INIT], namespace=None, log_file=log_file)
    assert code == 1
    assert out.strip() == ""
    assert err.strip() == "", "stderr 不应有任何输出"
    assert "MEMORY_NAMESPACE" in log_file.read_text(encoding="utf-8")


def test_invalid_namespace_blocks_start(tmp_path: Path) -> None:
    log_file = tmp_path / "m.log"
    _, err, code = run_mcp([INIT], namespace="Bad Name", log_file=log_file)
    assert code != 0
    assert err.strip() == "", "stderr 不应有任何输出"
    assert "FATAL" in log_file.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "tool", ["memory_recall", "memory_remember", "memory_forget", "memory_feedback"]
)
def test_bad_arguments_report_error_not_crash(tool: str, tmp_path: Path) -> None:
    """空参数调任一工具 → isError，进程不崩（参数校验而非未实现）。"""
    call = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {}},
        }
    )
    out, _, code = run_mcp([call], log_file=tmp_path / "m.log")
    assert code == 0, "参数非法不应让进程崩溃"
    assert parse(out)[0]["result"]["isError"] is True


def test_all_four_tools_are_implemented() -> None:
    """四个工具都不能再挂 NotImplementedError —— 这是阶段 E 的出口条件。"""
    from memory.entrypoints.mcp_stdio import HANDLERS

    assert set(HANDLERS) == {
        "memory_recall",
        "memory_remember",
        "memory_forget",
        "memory_feedback",
    }
    for name, handler in HANDLERS.items():
        source = inspect.getsource(handler)
        assert "NotImplementedError" not in source, f"{name} 仍是空壳"


def test_recall_survives_trace_store_failure(tmp_path: Path, monkeypatch) -> None:
    """凭据存储挂了不能连累召回 —— trace 是辅助数据。

    真踩过：目录不可写时整个 recall 变 isError，上层拿到 KeyError: 'materials'。
    这里用假 store 绕开外部依赖，只断言「故障被隔离」。
    """
    from memory.entrypoints import mcp_stdio

    class _FakeResult:
        materials = [{"event_id": "evt_" + "a" * 16}]
        trace_id = None

        def to_payload(self):
            return {"materials": self.materials, "trace_id": self.trace_id}

    def boom(_settings):
        raise RuntimeError("目录不可写")

    monkeypatch.setattr(mcp_stdio, "trace_store_from_settings", boom)
    monkeypatch.setattr(mcp_stdio, "recall", lambda *a, **kw: _FakeResult())

    settings = mcp_stdio.load_settings().model_copy(
        update={"MEMORY_NAMESPACE": "agent-a"}
    )
    out = mcp_stdio._tool_recall(settings, {"query": "项目"})
    assert "materials" in out, "召回结果不该被 trace 故障吞掉"
    assert out["trace_id"] is None


def test_feedback_validates_before_touching_storage() -> None:
    """参数校验要挡在存储之前 —— 坏参数不该先建库再报错。"""
    from memory.entrypoints import mcp_stdio

    settings = mcp_stdio.load_settings().model_copy(
        update={"MEMORY_NAMESPACE": "agent-a"}
    )
    with pytest.raises(ValueError, match="trace_id"):
        mcp_stdio._tool_feedback(settings, {"trace_id": "", "used_material_ids": ["x"]})
    with pytest.raises(ValueError, match="used_material_ids"):
        mcp_stdio._tool_feedback(settings, {"trace_id": "tr_x", "used_material_ids": []})
