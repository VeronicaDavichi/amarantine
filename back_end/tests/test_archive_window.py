"""A4：冷热分层窗口必须可配，且换值真的会改变划分。

ADR 原文：`ARCHIVE_WINDOW_DAYS=90` 与 `=365` 下，同一批事件的冷集/热集
划分不同且符合 D1 定义。

**发现的问题**：`window_days` 原来在 `ArchiveRequest` 里硬编码 `=180`，
`ARCHIVE_WINDOW_DAYS` 在 `src/` 里零消费 —— 改配置对行为没有任何影响。
这和 A10/A16/A27 是同一个模式：配置项在、逻辑不在。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from memory.config import Settings


class _FakeStore:
    """只实现 `count_older_than` 依赖的 `list_all`。"""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def list_all(self, namespace: str, *, limit: int = 1000):
        return self._rows

    def close(self) -> None:
        pass

    # 真实方法照抄语义：字符串比较即时间比较（ISO-8601 UTC）
    def count_older_than(self, namespace: str, *, cutoff: str, limit: int = 5000):
        rows = self.list_all(namespace, limit=limit)
        old = [r for r in rows if str(r.get("observed_at") or "") < cutoff]
        return len(old), len(rows)


def _row(days_ago: int) -> dict:
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"observed_at": ts.isoformat()}


#: 90 天内 2 条，90–365 之间 3 条 → 两个窗口必然分道扬镳
MIXED = [_row(10), _row(60), _row(120), _row(200), _row(300)]


@pytest.mark.parametrize(
    ("window", "expected_cold"),
    [(90, 3), (365, 0)],
)
def test_window_changes_split(window: int, expected_cold: int) -> None:
    """D3：同一批事件，window 不同 → 冷集不同。"""
    store = _FakeStore(MIXED)
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=window)
    ).isoformat()
    cold, scanned = store.count_older_than("work", cutoff=cutoff)
    assert scanned == 5
    assert cold == expected_cold, f"window={window} 的冷集算错了"


def test_config_drives_default_window(monkeypatch, tmp_path) -> None:
    """A4 的核心：`ARCHIVE_WINDOW_DAYS` 必须能改变默认窗口。"""
    import memory.entrypoints.admin_http as mod

    settings = Settings()
    settings.MEMORY_NAMESPACE = "work"
    settings.ADMIN_TOKEN = "tok"
    settings.DATABASE_URL = f"sqlite:///{(tmp_path / 'a.db').as_posix()}"
    settings.ARCHIVE_WINDOW_DAYS = 90

    monkeypatch.setattr(mod, "WeaviateStore", lambda cfg: _FakeStore(MIXED))

    client = TestClient(mod.create_app(settings))
    r = client.post(
        "/admin/archive",
        json={"dry_run": True},  # 不传 window_days
        headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["window_days"] == 90, "配置值没被采用"
    assert body["candidates"] == 3


def test_request_param_still_overrides_config(monkeypatch, tmp_path) -> None:
    """显式传参仍然优先 —— 修配置默认值不能把覆盖能力弄丢。"""
    import memory.entrypoints.admin_http as mod

    settings = Settings()
    settings.MEMORY_NAMESPACE = "work"
    settings.ADMIN_TOKEN = "tok"
    settings.DATABASE_URL = f"sqlite:///{(tmp_path / 'b.db').as_posix()}"
    settings.ARCHIVE_WINDOW_DAYS = 90

    monkeypatch.setattr(mod, "WeaviateStore", lambda cfg: _FakeStore(MIXED))

    client = TestClient(mod.create_app(settings))
    r = client.post(
        "/admin/archive",
        json={"window_days": 365, "dry_run": True},
        headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 200
    assert r.json()["window_days"] == 365
    assert r.json()["candidates"] == 0


def test_invalid_window_rejected(monkeypatch, tmp_path) -> None:
    import memory.entrypoints.admin_http as mod

    settings = Settings()
    settings.MEMORY_NAMESPACE = "work"
    settings.ADMIN_TOKEN = "tok"
    settings.DATABASE_URL = f"sqlite:///{(tmp_path / 'c.db').as_posix()}"

    monkeypatch.setattr(mod, "WeaviateStore", lambda cfg: _FakeStore(MIXED))

    client = TestClient(mod.create_app(settings))
    r = client.post(
        "/admin/archive",
        json={"window_days": 0},
        headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 400


def test_config_window_still_validated(monkeypatch, tmp_path) -> None:
    """配置值非法也要 400，不能因为「来自配置」就跳过校验。"""
    import memory.entrypoints.admin_http as mod

    settings = Settings()
    settings.MEMORY_NAMESPACE = "work"
    settings.ADMIN_TOKEN = "tok"
    settings.DATABASE_URL = f"sqlite:///{(tmp_path / 'd.db').as_posix()}"
    settings.ARCHIVE_WINDOW_DAYS = 0

    monkeypatch.setattr(mod, "WeaviateStore", lambda cfg: _FakeStore(MIXED))

    client = TestClient(mod.create_app(settings))
    r = client.post(
        "/admin/archive",
        json={"dry_run": True},
        headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 400
