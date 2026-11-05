"""spec 025：`LABEL_REVIEW_BATCH` 与 `LABEL_LLM_TEMP` 必须真的跟着配置走。

沿用 spec 024 定下的规则：**改配置 → 行为必须变**。
「默认值看起来对」不算证据 —— `DB_BUSY_TIMEOUT_MS` 就是这么骗过了所有人。
"""

from __future__ import annotations

import json

import pytest

from memory.config import Settings
from memory.services.llm import chat


# ================================================================ LABEL_LLM_TEMP


class _Recorder:
    """假 httpx client：记下发出去的 payload，返回一个合法响应。"""

    def __init__(self, provider: str = "openai") -> None:
        self.payloads: list[dict] = []
        self._provider = provider

    def post(self, url, headers=None, json=None):  # noqa: A002 - 模拟 httpx 签名
        self.payloads.append(json or {})

        class _Resp:
            status_code = 200

            def __init__(self, provider: str) -> None:
                self._provider = provider

            def json(self_inner):
                if self_inner._provider == "anthropic":
                    return {"content": [{"type": "text", "text": "{}"}]}
                return {"choices": [{"message": {"content": "{}"}}]}

        return _Resp(self._provider)


def _settings(**over) -> Settings:
    s = Settings()
    for k, v in over.items():
        setattr(s, k, v)
    return s


@pytest.mark.parametrize("temp", [0.0, 0.2, 0.7])
def test_label_temp_appears_in_payload(temp: float) -> None:
    """**核心证据**：配了温度，请求 payload 里必须真的有它。

    改之前 `chat()` 的 payload 里根本没有 temperature 字段 ——
    打标一直在用服务端默认温度（通常 1.0），双跑一致性天然偏低。
    """
    rec = _Recorder()
    s = _settings(LLM_PROVIDER="openai", LLM_API_KEY="k")
    chat([{"role": "user", "content": "hi"}], s, temperature=temp, client=rec)
    assert rec.payloads, "没发请求"
    assert rec.payloads[0].get("temperature") == temp


def test_label_temp_omitted_when_none() -> None:
    """不传 temperature 时 payload 里**不能有这个字段**。

    这条保护其他调用方（alias_resolver / llm_split）的行为不变 ——
    它们没要求低温度，硬塞一个 0.2 会改变它们的输出分布。
    """
    rec = _Recorder()
    s = _settings(LLM_PROVIDER="openai", LLM_API_KEY="k")
    chat([{"role": "user", "content": "hi"}], s, client=rec)
    assert "temperature" not in rec.payloads[0]


def test_label_temp_in_anthropic_branch_too() -> None:
    """**两个 provider 分支都要带** —— 只加一条就是半个修复。

    Anthropic 的 payload 结构和 OpenAI 不同（system 是顶层字段），
    很容易只改了 else 分支就以为完事。
    """
    rec = _Recorder(provider="anthropic")
    s = _settings(LLM_PROVIDER="anthropic", LLM_API_KEY="k")
    chat([{"role": "user", "content": "hi"}], s, temperature=0.2, client=rec)
    assert rec.payloads[0].get("temperature") == 0.2


def test_labeler_passes_configured_temp(monkeypatch) -> None:
    """`make_llm_labeler` 造出来的回调必须把**配置值**传下去，不是常数。"""
    import memory.services.llm_labeler as lm

    captured: dict = {}

    def _fake_chat(messages, settings, **kw):
        captured.update(kw)
        return json.dumps({"types": ["work"], "confidence": 0.9})

    monkeypatch.setattr(lm, "chat", _fake_chat)

    s = _settings(LLM_PROVIDER="openai", LLM_API_KEY="k", LABEL_LLM_TEMP=0.15)
    labeler = lm.make_llm_labeler(s, vocab=["work", "money"])
    labeler("张三决定项目A延期", ["work"])

    assert captured.get("temperature") == 0.15, (
        "打标没传配置的温度，或者传了常数"
    )


def test_labeler_temp_changes_with_config(monkeypatch) -> None:
    """改配置 -> 传下去的温度跟着变（不是写死某个值）。"""
    import memory.services.llm_labeler as lm

    seen: list = []

    def _fake_chat(messages, settings, **kw):
        seen.append(kw.get("temperature"))
        return json.dumps({"types": ["work"], "confidence": 0.9})

    monkeypatch.setattr(lm, "chat", _fake_chat)

    for temp in (0.0, 0.35, 0.9):
        s = _settings(LLM_PROVIDER="openai", LLM_API_KEY="k", LABEL_LLM_TEMP=temp)
        labeler = lm.make_llm_labeler(s, vocab=["work"])
        labeler("句子", ["work"])

    assert seen == [0.0, 0.35, 0.9]


# ================================================================ LABEL_REVIEW_BATCH


def _make_app(monkeypatch, tmp_path, *, batch: int):
    from fastapi.testclient import TestClient

    import memory.entrypoints.admin_http as mod
    from memory.services.label_review import LabelReviewQueue

    s = Settings()
    s.MEMORY_NAMESPACE = "work"
    s.ADMIN_TOKEN = "tok"
    s.DATABASE_URL = f"sqlite:///{(tmp_path / 'lr.db').as_posix()}"
    s.LABEL_REVIEW_BATCH = batch

    # 塞 30 条待复核
    q = LabelReviewQueue(tmp_path / "lr.db")
    for i in range(30):
        q.push(
            namespace="work",
            event_id=f"evt_{i:03d}",
            summary=f"句子{i}",
            rules_types=["work"],
            llm_types=["money"],
            agreement=0.0,
        )

    monkeypatch.setattr(mod, "queue_from_settings", lambda cfg: q)
    app = mod.create_app(s)
    return TestClient(app), q


def test_label_review_batch_controls_page_size(monkeypatch, tmp_path) -> None:
    """**核心证据**：不传 limit 时，返回条数由配置决定。

    改之前端点是 `Query(default=50)` 硬编码，`LABEL_REVIEW_BATCH` 形同虚设。
    """
    client, _ = _make_app(monkeypatch, tmp_path, batch=7)
    r = client.get("/admin/label-review", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200
    assert len(r.json()["items"]) == 7, "配置没控制住返回条数"


@pytest.mark.parametrize("batch", [3, 11, 25])
def test_label_review_batch_changes_with_config(monkeypatch, tmp_path, batch: int) -> None:
    client, _ = _make_app(monkeypatch, tmp_path, batch=batch)
    r = client.get("/admin/label-review", headers={"Authorization": "Bearer tok"})
    assert len(r.json()["items"]) == batch


def test_explicit_limit_beats_config(monkeypatch, tmp_path) -> None:
    """显式 `?limit=` 必须优先于配置 —— 否则运维没法临时看更多。"""
    client, _ = _make_app(monkeypatch, tmp_path, batch=5)
    r = client.get(
        "/admin/label-review?limit=20", headers={"Authorization": "Bearer tok"}
    )
    assert len(r.json()["items"]) == 20


def test_total_is_not_truncated_by_limit(monkeypatch, tmp_path) -> None:
    """`total` 是总数，不受分页 limit 影响 —— 否则前端会以为只有 5 条。"""
    client, _ = _make_app(monkeypatch, tmp_path, batch=5)
    r = client.get("/admin/label-review", headers={"Authorization": "Bearer tok"})
    assert len(r.json()["items"]) == 5
    assert r.json()["total"] == 30
