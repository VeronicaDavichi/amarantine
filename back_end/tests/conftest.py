"""全局测试夹具。

## 为什么需要清理 jieba 词典

`load_entity_dictionary` 把实体名灌进 jieba 的**进程级**词典，
`remember_event` 每写一条就灌一次。测试之间不清的话会互相污染：

    test_dual_run   写入「张三决定项目A延期」→ 灌「项目A」
    test_labeler    再抽「项目A」→ 变成词组，抽取结果和预期不符

表现是**单跑通过、全量失败**，而且失败信息完全指不到真正的原因。
踩过一次，所以这里 autouse 清掉。

## 为什么意图日志要钉到 tmp 库

`remember_event` 每次写都会往 `pending_task` 记一笔（写前记账）。
不隔离的话它按 `DATABASE_URL` 落到**开发库** `data/memory.db`：

- 测试在真数据里留下任务行
- 而**启动补跑会去重放它们** —— 测试数据被当成真事件写进图

所以这里把意图日志钉到 `tmp_path`，而不是改各个测试的调用点
（`remember_event` 在三个文件里被调用，钉在夹具里只改一处）。
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


@pytest.fixture(autouse=True)
def _clean_declared_surnames():
    """每个测试结束后清掉登记过的姓氏。

    `label_by_rules` 会解析 `姓X` 并写进进程级 `_DECLARED_SURNAMES`
    （为了让同一句里的别名立刻受益）。不清理的话，
    前一个测试声明的姓会让后一个测试的判据悄悄变宽。
    """
    yield
    try:
        from memory.services.labeler import reset_surnames

        reset_surnames()
    except Exception:  # noqa: BLE001 - 清理失败不能影响测试结论
        pass


@pytest.fixture(autouse=True)
def _isolate_intent_log(tmp_path, monkeypatch):
    """把意图日志钉到临时库，别写进开发库。"""
    from memory.services import remember as rmod
    from memory.services.pending_task import PendingTaskStore

    store = PendingTaskStore(tmp_path / "intent.sqlite3")

    def _fake(settings, injected):
        return injected if injected is not None else store

    monkeypatch.setattr(rmod, "_intent_store", _fake)
    yield store
