"""spec 046：管理员字段完整性（A3/A5 真库验证暴露的三个缺口）。"""

from __future__ import annotations

from memory.services.weaviate_store import WeaviateStore


def test_admin_fields_include_session_id() -> None:
    """按会话取已有卡要 session_id；缺了它 `_recent_cards` 永远返回空。"""
    assert "session_id" in WeaviateStore._ADMIN_FIELDS


def test_admin_fields_include_resolves() -> None:
    """归并链要能看见；缺了它 A5 在真库上永远是空的。"""
    assert "resolves" in WeaviateStore._ADMIN_FIELDS


def test_admin_fields_no_nonexistent_props() -> None:
    """写进 GraphQL 的字段必须真实存在，否则整个 list_all 报错。

    实测：`supersedes` 不在 schema 里 → `Cannot query field "supersedes"`，
    管理员列表直接 500。
    """
    assert "supersedes" not in WeaviateStore._ADMIN_FIELDS


def test_list_all_requests_object_id() -> None:
    """对象 id 在 `_additional { id }`，不请求就拿不到删改依据。"""
    import inspect

    src = inspect.getsource(WeaviateStore.list_all)
    assert "_additional" in src and "id" in src
