"""全局测试夹具。

## 为什么需要清理 jieba 词典

`load_entity_dictionary` 把实体名灌进 jieba 的**进程级**词典，
`remember_event` 每写一条就灌一次。测试之间不清的话会互相污染：

    test_dual_run   写入「张三决定项目A延期」→ 灌「项目A」
    test_labeler    再抽「项目A」→ 变成词组，抽取结果和预期不符

表现是**单跑通过、全量失败**，而且失败信息完全指不到真正的原因。
踩过一次，所以这里 autouse 清掉。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean_jieba_dictionary():
    """每个测试结束后撤掉反哺进 jieba 的词。"""
    yield
    try:
        from memory.services.labeler import reset_dictionary

        reset_dictionary()
    except Exception:  # noqa: BLE001 - 清理失败不能影响测试结论
        pass
