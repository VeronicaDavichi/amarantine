"""spec 044 前置：连续串内不切词（谐音/拼词会切错）。

用户 2026-11-18 指出：`新机子瓦一兔子` 是「真相只有一个」的谐音
（shinjitsu wa hitotsu），`瓦一` 只是音节，不是另一个名字。
"""

from __future__ import annotations

import pytest

from memory.services.name_marker import has_name_boundary


@pytest.mark.parametrize("alias,frag", [
    ("新机子瓦一兔子", "瓦一"),
    ("传讯员8号", "员8"),
    ("扑街少年MIKA", "MIKA"),
])
def test_no_boundary_inside_continuous_run(alias: str, frag: str) -> None:
    assert has_name_boundary(alias, frag) is False


@pytest.mark.parametrize("alias,frag", [
    ("Veronica Davichi", "Davichi"),
    ("ALICE + OLIVIA", "ALICE"),
    ("便携式🦆🍗", "便携式"),
    ("小特（DeepSeek）", "DeepSeek"),
    ("Heat？", "Heat"),
])
def test_boundary_at_separator(alias: str, frag: str) -> None:
    assert has_name_boundary(alias, frag) is True


def test_phrase_fragment_passes_but_needs_review() -> None:
    """`问题多 ⊂ 办法总比问题多♔` 有分隔符，所以边界判据放行。

    它是**短语碎片**（整句是「办法总比问题多」），不是连续串碎片 ——
    这类靠人工复核页拦（`docs/labeling.html` 的「人名碎片」组），
    不归这条规则管。记下来免得以后以为规则漏了。
    """
    assert has_name_boundary("办法总比问题多♔", "问题多") is True


def test_boundary_requires_frag_present() -> None:
    assert has_name_boundary("Veronica Davichi", "不存在") is False
    assert has_name_boundary("", "x") is False
    assert has_name_boundary("abc", "") is False
