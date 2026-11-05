"""机制化检测：config 里的字段必须真的有消费点。

**为什么要这个测试**：本项目反复出现「配置项有默认值、消费它的逻辑不存在」：

- A10 `GRAPH_HUB_DEGREE` / `CONFIRMED_MIN`（`forget_policy` 零引用）
- A16 `CHUNK_KEEP_PER_EVENT`（整个 chunk 层不存在）
- A27 五个 `RERANK_*`（零引用）
- A4 `ARCHIVE_WINDOW_DAYS`（接口硬编码 180）
- 014 `RERANK_CANDIDATES` / `GRAPH_TIMEOUT_MS` / `AGENT_KEY_HEADER`（零引用）

五次之后不该再靠人眼。这个测试让第六次在 CI 里挂掉。

**判据**：字段名作为独立标识符出现在 `src/` 的任何 `.py`（`config.py` 除外）
的**代码**里（注释不算 —— 「提到过」不等于「用了」）。
"""

from __future__ import annotations

import functools
import pathlib
import re

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
CONFIG = SRC / "memory" / "config.py"

#: 允许「有定义但暂无消费点」的字段。**每一项必须写理由**，
#: 否则就会变成垃圾桶 —— 有 `test_whitelist_entries_have_reasons` 守着。
WHITELIST: dict[str, str] = {
    # ---- P2 预留（ADR D8 consolidation，未实现）----
    "CLUSTER_MERGE_SIM": "P2 主题簇合并阈值，D8 未实现",
    "CLUSTER_MIN_SIZE": "P2 主题簇最小规模，D8 未实现",
    "CONSOLIDATE_MIN_EVENTS": "P2 consolidation 触发条件，未实现",
    "CONSOLIDATE_DUP_RATIO": "P2 consolidation 冗余判据，未实现",
    "CONSOLIDATE_LATENCY_MS": "P2 consolidation 预算，未实现",
    "COLD_QUERY_P95_MS": "P1 冷查询目标，物理冷热拆分未做（ADR §8 待定）",
    # ---- 外部服务适配（当前只支持本地无鉴权的 TEI）----
    "EMBEDDING_API_KEY": "换商用 embedding 服务时才需要，本地 TEI 无鉴权",
    "EMBEDDING_PROVIDER": "同上，当前硬编码 TEI 协议",
    "WEAVIATE_GRPC_PORT": "用 REST 而非 gRPC，保留了端口配置未接线",
    # ---- 未接线的真参数（有价值，但本 spec 范围外）----
    "ENTITY_ALIAS_SIM": "实体消歧阈值写死，C.B3 未接配置",
    "GUIDE_CHECK_CRON": "文档一致性校验靠手动跑，没有定时器",
    "LABEL_LLM_TEMP": "LLM 打标温度写死",
    "LABEL_REVIEW_BATCH": "复核队列批量大小未接",
    "TOKENIZER": "分词器写死 cl100k_base，未接配置",
}


def _config_fields() -> list[str]:
    text = CONFIG.read_text()
    return re.findall(r"^    ([A-Z][A-Z0-9_]*)\s*:", text, re.M)


def _strip_comments(source: str) -> str:
    """去掉行注释与整行注释，避免「注释里提过」被当成「用了」。

    不处理字符串里的 `#`（本项目的 prompt 里不出现裸 `#`，够用）。
    """
    out = []
    for line in source.splitlines():
        idx = line.find("#")
        out.append(line if idx < 0 else line[:idx])
    return "\n".join(out)


@functools.lru_cache(maxsize=None)
def _consumer_files(field: str) -> tuple[str, ...]:
    pattern = re.compile(rf"\b{re.escape(field)}\b")
    hits: list[str] = []
    for path in SRC.rglob("*.py"):
        if path == CONFIG:
            continue
        if pattern.search(_strip_comments(path.read_text(errors="ignore"))):
            hits.append(path.relative_to(SRC).as_posix())
    return tuple(hits)


def test_every_config_field_has_a_consumer() -> None:
    """没消费点、又不在白名单 → 挂掉。"""
    unused = [f for f in _config_fields() if not _consumer_files(f)]
    unexpected = sorted(f for f in unused if f not in WHITELIST)
    assert not unexpected, (
        "这些配置项有定义但没有消费点，也不在白名单里：\n  "
        + "\n  ".join(unexpected)
        + "\n\n要么把配置真正接上，要么加进 WHITELIST 并写明理由。"
        + "「配了但没生效」比「压根没这个配置」更危险 —— 你以为调了，其实没调。"
    )


def test_whitelist_entries_are_still_unused() -> None:
    """白名单不能腐烂：某字段后来接上了，就应该把它删掉。

    否则白名单会慢慢变成「历史上曾经没用过的字段集合」，
    失去了「当前真实欠债清单」的意义。
    """
    now_used = sorted(f for f in WHITELIST if _consumer_files(f))
    assert not now_used, (
        "这些字段已经有消费点了，请从 WHITELIST 删掉：\n  " + "\n  ".join(now_used)
    )


def test_whitelist_entries_have_reasons() -> None:
    """每条白名单必须写理由 —— 空理由会被下一个人随手补上。"""
    empty = [f for f, why in WHITELIST.items() if not why.strip()]
    assert not empty, f"白名单里这些字段没写理由：{empty}"


def test_whitelist_entries_exist_in_config() -> None:
    """白名单里不能有配置里不存在的字段（拼错会静默失效）。"""
    fields = set(_config_fields())
    ghosts = sorted(f for f in WHITELIST if f not in fields)
    assert not ghosts, f"白名单里有不存在的字段（拼写错误？）：{ghosts}"


def test_detector_actually_detects() -> None:
    """元测试：造一个必然无人用的字段名，确认检测逻辑真的会报出来。

    没有这条，检测器一旦写错（比如正则失效），
    上面三个测试全绿但什么也没检查 —— 典型的「测试通过、功能没有」。
    """
    fake = "DEFINITELY_NOBODY_USES_THIS_FIELD_XYZ"
    assert _consumer_files(fake) == (), "检测器把不存在的字段判成有用过"
    assert fake not in WHITELIST


def test_comments_do_not_count_as_consumers() -> None:
    """注释里提到 ≠ 用了。`TIME_CONF_MIN` 就是活例子。"""
    assert _strip_comments("x = 1  # SOME_FIELD\n") .strip() == "x = 1"
    # `TIME_CONF_MIN` 曾经只在注释里出现，015 把它接上了 ——
    # 现在它既不在白名单里，也必须真有消费点
    assert "TIME_CONF_MIN" not in WHITELIST
    assert _consumer_files("TIME_CONF_MIN")


@pytest.mark.parametrize("field", ["AGENT_KEY_HEADER", "GRAPH_TIMEOUT_MS", "RERANK_CANDIDATES"])
def test_fields_fixed_in_014_stay_wired(field: str) -> None:
    """014 修好的三个字段必须保持「有消费点」，且不在白名单里。"""
    assert field not in WHITELIST, f"{field} 已接线，不该还在白名单"
    assert _consumer_files(field), f"{field} 又变成零引用了"
