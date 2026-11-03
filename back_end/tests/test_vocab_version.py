"""A23：词表版本化 —— 历史卡能回溯「按哪版词表打的标签」。"""

from __future__ import annotations

import pytest

from memory.config import Settings
from memory.services.vocab_version import (
    LEGACY_VERSION,
    VocabRegistry,
    effective_vocab,
)


@pytest.fixture()
def reg(tmp_path) -> VocabRegistry:
    return VocabRegistry(tmp_path / "vocab.db")


@pytest.fixture()
def settings() -> Settings:
    s = Settings()
    s.TYPES_VOCAB = []
    return s


# ---------------- D1：同词表两次 → 同版本 ----------------


def test_same_vocab_same_version(reg: VocabRegistry, settings: Settings) -> None:
    v1 = reg.current(settings)
    v2 = reg.current(settings)
    assert v1 == v2
    assert v1.startswith("v1-"), f"首个版本应是 v1-xxx：{v1}"


# ---------------- D2：词表变了 → 版本变、序号 +1 ----------------


def test_vocab_change_bumps_version(reg: VocabRegistry, settings: Settings) -> None:
    first = reg.current(settings)
    settings.TYPES_VOCAB = ["decision", "work"]
    second = reg.current(settings)

    assert first != second
    assert second.startswith("v2-"), f"第二个版本应是 v2-xxx：{second}"

    # 两版都能回溯，且内容不同
    assert reg.get(first) != reg.get(second)


def test_switching_back_reuses_registered_version(
    reg: VocabRegistry, settings: Settings
) -> None:
    """改回旧词表不该登记第三版 —— 同内容同版本。"""
    settings.TYPES_VOCAB = ["decision"]
    v1 = reg.current(settings)
    settings.TYPES_VOCAB = ["work"]
    v2 = reg.current(settings)
    settings.TYPES_VOCAB = ["decision"]
    again = reg.current(settings)

    assert again == v1, "回到旧词表却登记了新版本"
    assert v2 != v1
    assert reg.list_versions() == [v1, v2]


# ---------------- 生效词表的口径 ----------------


def test_effective_vocab_defaults_include_fallback(settings: Settings) -> None:
    vocab = effective_vocab(settings)
    assert "general" in vocab, "兜底类型必须在生效词表里"
    assert "decision" in vocab
    assert vocab == sorted(vocab), "应当是排序去重的"


def test_effective_vocab_respects_config(settings: Settings) -> None:
    settings.TYPES_VOCAB = ["only_this"]
    assert effective_vocab(settings) == ["only_this"]


# ---------------- D6：不存在的版本 → None（端点转 404） ----------------


def test_unknown_version_returns_none(reg: VocabRegistry, settings: Settings) -> None:
    reg.current(settings)
    assert reg.get("v99-deadbeef") is None


# ---------------- D5：历史卡读出补 v0-legacy ----------------


def test_legacy_card_reads_as_placeholder() -> None:
    """版本化之前写入的卡没有该字段 —— 补占位值，不报错、不伪造具体版本。"""
    from memory.services.weaviate_store import LEGACY_VOCAB_VERSION, WeaviateStore

    hit = WeaviateStore._to_hit({"event_id": "evt_1"}, score=0.0)
    assert hit["vocab_version"] == LEGACY_VOCAB_VERSION == LEGACY_VERSION
    assert hit["vocab_version"] == "v0-legacy"


def test_existing_vocab_version_is_preserved() -> None:
    from memory.services.weaviate_store import WeaviateStore

    hit = WeaviateStore._to_hit(
        {"event_id": "evt_1", "vocab_version": "v3-abc12345"}, score=0.0
    )
    assert hit["vocab_version"] == "v3-abc12345", "已有版本号被覆盖了"


# ---------------- D3：写入的卡带上当时的版本 ----------------


def test_remember_stores_vocab_version(tmp_path) -> None:
    from memory.services.remember import remember_event

    settings = Settings()
    settings.MEMORY_NAMESPACE = "work"
    settings.DATABASE_URL = f"sqlite:///{(tmp_path / 'v.db').as_posix()}"
    settings.EMBEDDING_MODEL = "m1"
    settings.TYPES_VOCAB = []

    captured: dict = {}

    class _CapturingStore:
        def get_by_event_id(self, namespace, event_id):
            return None

        def insert_event(self, card, vector, *, anomaly=None, vocab_version=None):
            captured["vocab_version"] = vocab_version
            return "uid-1"

    class _Stop(Exception):
        pass

    def stop_after_insert(*a, **k):
        raise _Stop("写库之后立即中止，只验版本号")

    from memory.services import remember as rem_mod

    original = rem_mod._write_graph_with_intent
    rem_mod._write_graph_with_intent = stop_after_insert
    try:
        with pytest.raises(_Stop):
            remember_event(
                namespace="work",
                summary="决定了，下个月上线",
                settings=settings,
                store=_CapturingStore(),
                embedder=lambda text, cfg: [0.0] * cfg.EMBEDDING_DIM,
            )
    finally:
        rem_mod._write_graph_with_intent = original

    assert captured.get("vocab_version"), "写入时没带 vocab_version"
    assert captured["vocab_version"].startswith("v1-")


# ---------------- D4/D6 端点级 ----------------


def _client(tmp_path):
    from fastapi.testclient import TestClient

    import memory.entrypoints.admin_http as mod

    settings = Settings()
    settings.MEMORY_NAMESPACE = "work"
    settings.ADMIN_TOKEN = "tok"
    settings.DATABASE_URL = f"sqlite:///{(tmp_path / 'http.db').as_posix()}"
    settings.TYPES_VOCAB = []
    return TestClient(mod.create_app(settings)), settings


def test_endpoint_returns_current_vocab(tmp_path) -> None:
    client, _ = _client(tmp_path)
    r = client.get("/label/vocab", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200
    body = r.json()
    assert body["version"].startswith("v1-")
    assert body["count"] == len(body["vocab"])
    assert "general" in body["vocab"]


def test_endpoint_traces_old_version(tmp_path) -> None:
    client, settings = _client(tmp_path)

    old = client.get("/label/vocab", headers={"Authorization": "Bearer tok"}).json()

    # 换个词表 → 新版本（create_app 捕获的是同一个 settings 对象引用）
    settings.TYPES_VOCAB = ["decision", "work"]
    new = client.get("/label/vocab", headers={"Authorization": "Bearer tok"}).json()
    assert new["version"] != old["version"]

    # 回溯旧版本
    r = client.get(
        f"/label/vocab?version={old['version']}",
        headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 200
    assert r.json()["vocab"] == old["vocab"]


def test_endpoint_unknown_version_404(tmp_path) -> None:
    client, _ = _client(tmp_path)
    r = client.get(
        "/label/vocab?version=v99-deadbeef",
        headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 404


def test_endpoint_requires_token(tmp_path) -> None:
    client, _ = _client(tmp_path)
    r = client.get("/label/vocab")
    assert r.status_code == 401
