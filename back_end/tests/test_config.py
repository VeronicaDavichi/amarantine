"""A1 配置加载测试：优先级、类型校验、namespace 规则。"""

from __future__ import annotations

from pathlib import Path

import pytest

from memory.config import Settings, load_settings


def test_defaults_match_adr_d6() -> None:
    s = Settings()
    assert s.ARCHIVE_WINDOW_DAYS == 180
    assert s.RECALL_TOP_K_MIN == 3
    assert s.RECALL_TOP_K_MAX == 30
    assert s.CONTEXT_TOKEN_BUDGET == 3000
    assert s.W_TIME_BASE == 0.7
    assert s.W_IMP_BASE == 0.8
    assert s.RECENCY_HALF_LIFE_DAYS == 180
    assert s.FORGET_GRACE_DAYS == 90
    assert s.EXPLICIT_IMPORTANT_FLOOR == 0.85
    assert s.EMBEDDING_DIM == 1024
    assert s.DATABASE_URL == "sqlite:///./data/memory.db"


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHIVE_WINDOW_DAYS", "90")
    monkeypatch.setenv("RERANK_ENABLED", "true")
    s = Settings()
    assert s.ARCHIVE_WINDOW_DAYS == 90
    assert s.RERANK_ENABLED is True


def test_yaml_override(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("ARCHIVE_WINDOW_DAYS: 365\nRECALL_TOP_K_MAX: 50\n", encoding="utf-8")
    s = load_settings(cfg)
    assert s.ARCHIVE_WINDOW_DAYS == 365
    assert s.RECALL_TOP_K_MAX == 50


def test_env_beats_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("ARCHIVE_WINDOW_DAYS: 365\n", encoding="utf-8")
    monkeypatch.setenv("ARCHIVE_WINDOW_DAYS", "42")
    s = load_settings(cfg)
    assert s.ARCHIVE_WINDOW_DAYS == 42, "env 必须覆盖 yaml"


@pytest.mark.parametrize("bad", ["Agent-A", "-lead", "有中文", "", "a" * 65])
def test_namespace_pattern_enforced(bad: str) -> None:
    with pytest.raises(Exception):
        Settings(MEMORY_NAMESPACE=bad)


@pytest.mark.parametrize("ok", ["agent-a", "a", "agent_1", "a0" * 24])
def test_namespace_valid_accepted(ok: str) -> None:
    assert Settings(MEMORY_NAMESPACE=ok).MEMORY_NAMESPACE == ok


@pytest.mark.parametrize(
    ("field", "bad"),
    [("W_TIME_BASE", 1.5), ("W_IMP_BASE", -0.1), ("RECALL_DROP_RATIO", 2.0)],
)
def test_ratio_bounds_enforced(field: str, bad: float) -> None:
    with pytest.raises(Exception):
        Settings(**{field: bad})
