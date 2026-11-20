"""spec 050 延伸：self 参与关系层 + MCP memory_whoami。"""

from __future__ import annotations

#: repo 根（tests → back_end → repo）。**不要写绝对路径** ——
#: 本机是 /mnt/e/...，CI 是 /home/runner/work/...，写死会让 CI 直接失败。
REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "harness"

import pytest

from memory.core import tools as T
from memory.entrypoints import mcp_stdio


def test_whoami_tool_registered() -> None:
    assert "memory_whoami" in T.TOOL_NAMES
    spec = T.TOOLS["memory_whoami"]
    assert spec["inputSchema"]["additionalProperties"] is False
    # 只读工具：不该要求任何必填参数
    assert not spec["inputSchema"].get("required")


def test_whoami_handler_registered() -> None:
    assert "memory_whoami" in mcp_stdio.HANDLERS


def test_whoami_handler_formats(monkeypatch) -> None:
    """处理器的输出要有「你是谁 + 群名片 + 关系」，并带 structuredContent。"""

    def fake_build(settings, namespace=""):
        return {
            "identity": {"configured": True, "qq": "10001", "uid": "u_me",
                         "canonical": "张三", "names": ["张三", "小张"],
                         "reason": ""},
            "namespace": namespace,
            "relations": [{"predicate": "OWNS", "target": "服务器",
                           "label": "probable", "confidence": 0.8}],
            "relations_note": "",
        }

    monkeypatch.setattr("memory.services.whoami.build_whoami", fake_build)

    class _S:
        MEMORY_NAMESPACE = "ns1"

    out = mcp_stdio.HANDLERS["memory_whoami"](_S(), {})
    text = out["content"][0]["text"]
    assert "张三" in text
    assert "QQ 10001" in text
    assert "OWNS → 服务器" in text
    assert out["structuredContent"]["identity"]["canonical"] == "张三"
    assert out["isError"] is False


def test_whoami_handler_unconfigured(monkeypatch) -> None:
    def fake_build(settings, namespace=""):
        return {"identity": {"configured": False, "qq": "", "uid": "",
                             "canonical": "", "names": [],
                             "reason": "没有配置 SELF_QQ"},
                "namespace": namespace, "relations": [], "relations_note": "无"}

    monkeypatch.setattr("memory.services.whoami.build_whoami", fake_build)

    class _S:
        MEMORY_NAMESPACE = "ns1"

    text = mcp_stdio.HANDLERS["memory_whoami"](_S(), {})["content"][0]["text"]
    assert "SELF_QQ" in text, "没配时要明确告诉模型，不能编"


@pytest.mark.parametrize("pron", ["我", "自己", "本人"])
def test_self_pronouns_in_alias_map(monkeypatch, pron: str) -> None:
    """第一人称代词要能归到本人（否则摘要里的「我」永远认不出）。"""
    import importlib.util
    import pathlib

    path = HARNESS / "build_relations.py"
    spec = importlib.util.spec_from_file_location("br", path)
    br = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(br)

    monkeypatch.setattr(
        "memory.services.self_identity.resolve_self",
        lambda s, namespace=None: {"uid": "u_me", "canonical": "张三",
                                   "names": ["小张"], "nicknames": ["张三"]},
    )
    got = br._self_alias_map(object())
    assert got.get(pron) == "acct_u_me"
    assert got.get("小张") == "acct_u_me"
