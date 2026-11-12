"""A2 契约测试：EventCard 模型必须与 ADR 附录 B 的 JSON Schema 一致。

这些测试的意义在于「文档即契约」——文档改了而代码没跟，测试立刻红。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from memory.models.event_card import FIELD_NAMES, REQUIRED_FIELDS, EventCard

ADR = Path(__file__).resolve().parents[2] / "docs" / "ADR-001-memory-architecture.md"


@pytest.fixture(scope="module")
def schema() -> dict:
    text = ADR.read_text(encoding="utf-8")
    app_b = text.split("## 附录 B")[1].split("## 9.")[0]
    m = re.search(r"```json\s*(\{.*?\n\})\s*```", app_b, re.S)
    assert m, "附录 B 未找到 JSON Schema 代码块"
    return json.loads(m.group(1))


def _valid_payload() -> dict:
    return {
        "namespace": "agent-a",
        "event_id": "evt_0123456789abcdef",
        "date_start": "2026-03-01",
        "types": ["work"],
        "entities": [{"name": "换工作", "kind": "topic"}],
        "summary": "主人考虑接受新 offer，顾虑通勤",
        "retention": "normal",
        "ingested_at": "2026-10-30T10:21:14+08:00",
    }


# ---------- 结构对拍 ----------


def test_field_count_and_names(schema: dict) -> None:
    assert len(FIELD_NAMES) == 32, f"字段数应为 32，实际 {len(FIELD_NAMES)}"
    assert set(FIELD_NAMES) == set(schema["properties"]), (
        f"字段集合不一致："
        f"仅在代码={set(FIELD_NAMES) - set(schema['properties'])}，"
        f"仅在文档={set(schema['properties']) - set(FIELD_NAMES)}"
    )


def test_required_parity(schema: dict) -> None:
    assert set(REQUIRED_FIELDS) == set(schema["required"]), (
        f"必填集合不一致：代码={set(REQUIRED_FIELDS)} 文档={set(schema['required'])}"
    )


@pytest.mark.parametrize(
    ("prop", "code_enum"),
    [
        ("retention", ["normal", "sensitive"]),
        ("user_directive", ["remember", "important", None]),
        ("pinned_source", ["manual", "user_directive", None]),
    ],
)
def test_enum_parity(schema: dict, prop: str, code_enum: list) -> None:
    doc_enum = schema["properties"][prop]["enum"]
    assert doc_enum == code_enum, f"{prop} 枚举不一致：文档={doc_enum} 代码={code_enum}"


def test_entity_kind_parity(schema: dict) -> None:
    doc_enum = schema["properties"]["entities"]["items"]["properties"]["kind"]["enum"]
    assert doc_enum == ["person", "topic", "place", "org", "event"]


# ---------- 实例校验 ----------


def test_valid_instance_passes_schema(schema: dict) -> None:
    payload = _valid_payload()
    card = EventCard.model_validate(payload)
    card.check_invariants()
    Draft202012Validator(schema).validate(card.model_dump(mode="json"))


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("namespace", "Agent-A"),  # 大写非法
        ("namespace", "-lead"),  # 不能以连字符开头
        ("event_id", "evt_XYZ"),  # 非 16 位小写 hex
        ("types", []),  # 至少一个标签
        ("summary", ""),  # 非空
        ("importance", 1.5),  # 越界
        ("emotion_intensity", -0.1),  # 越界
    ],
)
def test_invalid_values_rejected(field: str, bad_value: object) -> None:
    payload = _valid_payload() | {field: bad_value}
    with pytest.raises(Exception):
        EventCard.model_validate(payload)


def test_extra_field_rejected() -> None:
    payload = _valid_payload() | {"unknown_field": 1}
    with pytest.raises(Exception):
        EventCard.model_validate(payload)


# ---------- 业务不变量 ----------


def test_date_end_before_start_rejected() -> None:
    payload = _valid_payload() | {"date_start": "2026-03-01", "date_end": "2026-02-01"}
    with pytest.raises(ValueError, match="date_end"):
        EventCard.model_validate(payload).check_invariants()


def test_pinned_without_source_rejected() -> None:
    payload = _valid_payload() | {"pinned": True}
    with pytest.raises(ValueError, match="pinned_source"):
        EventCard.model_validate(payload).check_invariants()


def test_pinned_with_source_ok() -> None:
    payload = _valid_payload() | {"pinned": True, "pinned_source": "user_directive"}
    EventCard.model_validate(payload).check_invariants()


def test_source_without_pinned_rejected() -> None:
    payload = _valid_payload() | {"pinned": False, "pinned_source": "manual"}
    with pytest.raises(ValueError, match="pinned=False"):
        EventCard.model_validate(payload).check_invariants()
