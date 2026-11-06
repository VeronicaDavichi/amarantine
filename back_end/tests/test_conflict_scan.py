"""spec 033：健康报告的震荡扫描。

## 测什么

震荡扫描是**离线**能力：拿图里现有的实体名，重放一遍投票，看有多少对矛盾。

和运行时震荡日志的区别：

| | 运行时日志 | 离线扫描 |
|---|---|---|
| 数据 | 真实发生的候选扫描 | 图里所有可能的名字对 |
| 意味 | 「系统实际遇到了多少」 | 「这批数据里潜藏多少矛盾」 |

离线能回答「没人查过的数据会不会震荡」，日志答不了。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: `harness/` 不在 back_end 的 sys.path 里，测试要自己加
HARNESS = Path(__file__).resolve().parents[2] / "harness"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

import check_data_health as cdh  # noqa: E402

from memory.config import Settings  # noqa: E402


# ============================================================ 筛选逻辑


@pytest.mark.parametrize(
    "a,b,expected",
    [
        # 前缀关系 -> 值得扫（规则 1/2/3 的战场）
        ("上海", "上海市", True),
        ("张三", "张三丰", True),
        ("豆包", "豆包AI", True),
        ("张三", "张三哥", True),
        ("腾讯", "腾讯有限公司", True),
        # 无反向前缀关系 -> 不扫
        # 这几个是**第一版写错的地方**：我一开始用「编辑距离 ≤ 2」筛选，
        # 但中文两字名几乎恒满足 —— `张三`/`李四` 距离就是 2，
        # `北京`/`南京` 距离 1。等于没筛，白扫一堆永远 UNKNOWN 的对。
        ("张三", "李四", False),
        ("北京", "南京", False),
        ("项目A", "项目B", False),
        ("张三", "张四", False),
        ("项目A", "张三", False),
        # 同义词不靠字面，扫不到 —— 但它优先级最高不会震荡，漏掉无影响
        ("腾讯", "Tencent", False),
        # 完全相同 -> 跳过
        ("张三", "张三", False),
    ],
)
def test_possibly_related(a: str, b: str, expected: bool) -> None:
    """筛选只留**前缀关系**的对，避免全量 O(n²)。

    为什么不用编辑距离：中文两字名几乎都是距离 ≤ 2，筛不掉任何东西。
    规则 1/2/3 全都要求「一个是另一个的前缀」，这才是真战场。
    """
    assert cdh._possibly_related(a, b) is expected


# ============================================================ 扫描本身


def _run_scan(monkeypatch, settings, names, capsys) -> str:
    """跑 _scan_conflicts，把打印内容拿回来。

    **必须模拟真实的 `_graph_names` 行为：去重 + 排序。**
    第一版我写的是 `sorted(names)`，没去重 —— 于是
    `["张三", "张三"]` 进去还是 2 个名字，测出来的行为和线上不一致
    （真实的 `_graph_names` 用 set 去重）。测试替身必须和真身同构。
    """
    monkeypatch.setattr(cdh, "_graph_names", lambda s, ns: sorted(set(names)))
    cdh._scan_conflicts(settings, "work")
    return capsys.readouterr().out


def test_scan_finds_zero_on_related_but_not_conflicting(monkeypatch, capsys) -> None:
    """有前缀关系、但规则不矛盾 -> 扫到了，0 震荡。

    **不能用「互不相关的名字」测这条**：前缀筛选会把它们全过滤掉，
    于是根本不进「震荡 N 对」那行，测不到想测的东西。

    用 `张三` / `张三丰`：有前缀关系（会进扫描），正常配置下只命中规则 1，
    判 DIFFERENT，没有矛盾 -> 0 震荡。
    """
    out = _run_scan(monkeypatch, Settings(), ["张三", "张三丰", "李四"], capsys)
    assert "可扫描 1 对" in out, f"前缀关系没被扫到：{out}"
    assert "震荡 0 对" in out


def test_scan_warns_when_sample_too_small(monkeypatch, capsys) -> None:
    """**关键**：可扫描对少时，0 震荡必须带「没有统计意义」的警告。

    不加这句，将来有人看到「震荡 0」会以为规则层很健康 ——
    而实际只是数据太少。
    """
    out = _run_scan(monkeypatch, Settings(), ["张三", "张三丰"], capsys)
    assert "震荡 0 对" in out, f"没进入震荡统计那行：{out}"
    assert "没有统计意义" in out, f"小样本 0 震荡没警告：{out}"
    assert "数据太少" in out


def test_scan_finds_conflict_with_broken_config(monkeypatch, capsys) -> None:
    """人为造震荡 -> 必须扫出来并列出明细。

    怎么造：`上海` 是「姓+单字名」结构（`上` 在姓氏表），而「市」在后缀表。
    正常配置下 031 的弃权让它们不矛盾；这里用 monkeypatch 去掉弃权例外，
    回到 031 之前的状态 —— 规则 1 说不同、规则 3 说同。
    """
    from memory.services import entity_match as em

    monkeypatch.setattr(
        em,
        "_prefix_conflict_excluding_affix",
        lambda short, long, h, sfx: em.single_char_name_prefix_conflict(short, long),
    )

    s = Settings()
    s.ENTITY_SUFFIXES = "市,AI,公司"  # 「市」在后缀表
    # `阿里 / 阿里巴巴` 是关键的**对照组**：有前缀关系（会进扫描），但
    # 规则 3 要求「差**一个**后缀」，后缀表里没有「巴巴」，规则 1 也不命中
    # -> UNKNOWN / rule=""（不是 conflict）。
    #
    # 没有这一对，测试就区分不开「震荡」和「没命中」：
    # 如果判定写成 `r.ruling == UNKNOWN`，`上海/上海市` 照样算震荡，
    # 断言还是过 —— meta-test 注入时就是这么漏的。
    out = _run_scan(monkeypatch, s, ["上海", "上海市", "阿里", "阿里巴巴"], capsys)

    assert "震荡 1 对" in out, f"应该只有 1 对震荡（阿里那条是没命中不是震荡）：{out}"
    assert "上海" in out and "上海市" in out
    # 明细要同时列出说同和说不同的规则
    assert "suffix" in out, f"明细没列说同的规则：{out}"
    assert "single_char" in out or "prefix" in out, f"明细没列说不同的规则：{out}"
    # 对照组不能出现在震荡明细里
    assert "阿里" not in out, f"「没命中」被误判成震荡了：{out}"


def test_scan_dedupes_identical_names(monkeypatch, capsys) -> None:
    """同名进去 2 个，去重后只有 1 个 —— 不以「震荡 0」收场而是「名字少于 2 个」。

    第一版我期望「同名不算候选，报 0 对」，实测报的是「名字少于 2 个」：
    去重发生在 `_graph_names` 那一步，扫描层根本看不到重复。
    **测扫描逻辑时要注意上游已经做了什么。**
    """
    out = _run_scan(monkeypatch, Settings(), ["张三", "张三"], capsys)
    assert "名字少于 2 个" in out, f"同名没被去重：{out}"


def test_scan_handles_single_name(monkeypatch, capsys) -> None:
    """只有一个名字时不该崩，也扫不出什么。"""
    out = _run_scan(monkeypatch, Settings(), ["张三"], capsys)
    assert "名字少于 2 个" in out


def test_scan_reports_total_combinations(monkeypatch, capsys) -> None:
    """报告要写清「全部两两组合」和「可扫描」两个数。

    只报一个数会让人误解：4 个名字全量是 6 对，可扫描可能只有 2 对 ——
    读者需要知道筛掉了多少，才能判断结论的代表性。
    """
    out = _run_scan(
        monkeypatch, Settings(), ["张三", "张三丰", "李四", "项目A"], capsys
    )
    assert "全部两两组合 6 对" in out, f"没报全量组合数：{out}"


def test_scan_reports_rate(monkeypatch, capsys) -> None:
    """震荡率要有百分比，不是只有绝对数。"""
    out = _run_scan(monkeypatch, Settings(), ["张三", "张三丰", "李四"], capsys)
    assert "%" in out, f"没有震荡率：{out}"
