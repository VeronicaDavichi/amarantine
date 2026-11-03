"""A25：换 embedding 模型必须拒绝写入。

最要紧的两条：
- 不一致时必须是**错误**（不是告警）
- 报错必须发生在调用 embedding 服务**之前**（否则白花一次编码）
"""

from __future__ import annotations

import pytest

from memory.services.embedding_guard import (
    EmbeddingIndexMeta,
    EmbeddingModelMismatchError,
)


@pytest.fixture()
def meta(tmp_path) -> EmbeddingIndexMeta:
    return EmbeddingIndexMeta(tmp_path / "meta.sqlite3")


# ---------------- D4：空索引首次写入登记模型 ----------------


def test_first_write_registers_model(meta: EmbeddingIndexMeta) -> None:
    assert meta.get_model("work") is None, "新库不该有登记"
    assert meta.ensure_model("work", "BAAI/bge-m3") == "BAAI/bge-m3"
    assert meta.get_model("work") == "BAAI/bge-m3"


# ---------------- D1：一致就通过 ----------------


def test_same_model_passes(meta: EmbeddingIndexMeta) -> None:
    meta.ensure_model("work", "m1")
    assert meta.ensure_model("work", "m1") == "m1"


# ---------------- D2：不一致必须报错 ----------------


def test_different_model_raises(meta: EmbeddingIndexMeta) -> None:
    meta.ensure_model("work", "m1")
    with pytest.raises(EmbeddingModelMismatchError) as exc:
        meta.ensure_model("work", "m2")
    assert exc.value.expected == "m1"
    assert exc.value.actual == "m2"
    assert "m1" in str(exc.value) and "m2" in str(exc.value), "错误信息要能看出两个模型名"


# ---------------- D5：换回来就能写，不是永久锁死 ----------------


def test_model_can_switch_back(meta: EmbeddingIndexMeta) -> None:
    meta.ensure_model("work", "m1")
    with pytest.raises(EmbeddingModelMismatchError):
        meta.ensure_model("work", "m2")
    assert meta.ensure_model("work", "m1") == "m1"


# ---------------- 域隔离：不同 namespace 互不影响 ----------------


def test_namespaces_are_independent(meta: EmbeddingIndexMeta) -> None:
    meta.ensure_model("work", "m1")
    meta.ensure_model("personal", "m2")
    assert meta.get_model("work") == "m1"
    assert meta.get_model("personal") == "m2"


# ---------------- 空模型名没有校验意义 ----------------


def test_blank_model_rejected(meta: EmbeddingIndexMeta) -> None:
    with pytest.raises(ValueError):
        meta.ensure_model("work", "   ")


# ---------------- D2 的核心：校验在 embedding 之前 ----------------


def test_remember_rejects_before_calling_embedding(tmp_path) -> None:
    """这是 A25 里最容易做错的一条。

    把校验放在 embed 之后同样能「拦住」，但会白调一次 embedding 服务。
    用 spy 记录调用次数，断言为 0 —— 只有这样才能证明校验位置是对的。
    """
    from memory.config import Settings
    from memory.services.remember import remember_event

    settings = Settings()
    settings.MEMORY_NAMESPACE = "work"
    settings.DATABASE_URL = f"sqlite:///{(tmp_path / 'mem.db').as_posix()}"

    meta = EmbeddingIndexMeta(tmp_path / "mem.db")
    meta.ensure_model("work", "old-model")

    settings.EMBEDDING_MODEL = "new-model"

    embed_calls: list[str] = []

    def spy_embed(text: str, cfg) -> list[float]:
        embed_calls.append(text)
        return [0.0] * cfg.EMBEDDING_DIM

    with pytest.raises(EmbeddingModelMismatchError):
        remember_event(
            namespace="work",
            summary="主人接受新 offer，通勤一小时",
            settings=settings,
            embedder=spy_embed,
            meta=meta,
        )

    assert embed_calls == [], f"校验没拦住，白调了 {len(embed_calls)} 次 embedding"


def test_remember_registers_model_on_first_write(tmp_path) -> None:
    """首次写入应当登记模型（此时还没有旧索引可比）。"""
    from memory.config import Settings
    from memory.services.remember import remember_event

    settings = Settings()
    settings.MEMORY_NAMESPACE = "work"
    settings.DATABASE_URL = f"sqlite:///{(tmp_path / 'mem2.db').as_posix()}"
    settings.EMBEDDING_MODEL = "fresh-model"

    meta = EmbeddingIndexMeta(tmp_path / "mem2.db")
    assert meta.get_model("work") is None

    class _BoomStore:
        def get_by_event_id(self, namespace, event_id):
            # 写入路径在 embed 之前会先查幂等（A7），这里给「不存在」
            return None

        def insert_event(self, *a, **k):
            raise AssertionError("这条测试不该走到写入")

    class _StopAfterEmbed(Exception):
        pass

    def stop_embed(text: str, cfg) -> list[float]:
        raise _StopAfterEmbed("到此为止，只验证登记")

    with pytest.raises(_StopAfterEmbed):
        remember_event(
            namespace="work",
            summary="随便一句",
            settings=settings,
            store=_BoomStore(),
            embedder=stop_embed,
            meta=meta,
        )

    assert meta.get_model("work") == "fresh-model", "首次写入前应当已登记"
