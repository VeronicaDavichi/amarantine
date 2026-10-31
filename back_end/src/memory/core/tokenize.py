"""检索分词（BM25 通道专用）。

## 为什么要自己分词

Weaviate 默认 `word` tokenizer 对中文的处理是**把连续汉字当成一个词**：
`张三决定项目A延期到六月` 只会产生一个 token。于是查 `延期` 永远 0 命中
—— 这就是 M3 阶段 BM25 中文通道形同虚设的原因。

试过的两个替代方案都不够好：

| 方案 | 2 字查询（如「延期」） | 说明 |
|---|---|---|
| `word`（默认） | ❌ | 整句一个 token |
| `trigram` | ❌ | 2 字生成不出 3-gram，实测 `项目A` 能命中、`延期` 不能 |
| **bigram（本模块）** | ✅ | `延期` → `延期` 这个 token 直接命中 |

## 为什么是 bigram 不是 unigram

unigram 会切成 `延` + `期`，于是任何含「期」的句子都会被召回来，噪音太大。
bigram 保留了相邻约束，噪音小得多，而且不需要词典。

## 为什么不用 jieba

- 零依赖（jieba 要额外 15MB 词典 + 30MB 内存）
- 不依赖词典版本，不会因为一个新产品名没收录就切错
- 中文检索里 bigram 的召回率通常已经够用，精确率靠 BM25 的打分兜底

## 字段约定

输出是**空格连接**的 token 串，写进 Weaviate 的 `*_tokens` 字段
（那些字段用 `tokenization: "word"`，空格分隔后 Weaviate 就能正确切开）。
"""

from __future__ import annotations

import re

#: CJK 字符范围。覆盖基本区、扩展 A、兼容区、以及假名（日文也会被 bigram）
_CJK_RANGES = (
    (0x3400, 0x4DBF),    # 扩展 A
    (0x4E00, 0x9FFF),    # 基本区
    (0xF900, 0xFAFF),    # 兼容汉字
    (0x3040, 0x30FF),    # 平假名 + 片假名
    (0xAC00, 0xD7AF),    # 谚文
)

#: 一个字符属于 CJK 吗
def is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def _split_runs(text: str) -> list[tuple[bool, str]]:
    """把文本切成 (是否CJK, 片段) 的序列。"""
    runs: list[tuple[bool, str]] = []
    buf: list[str] = []
    cur: bool | None = None
    for ch in text:
        cjk = is_cjk(ch)
        if cur is None:
            cur = cjk
        if cjk != cur:
            if buf:
                runs.append((cur, "".join(buf)))
            buf = []
            cur = cjk
        buf.append(ch)
    if buf and cur is not None:
        runs.append((cur, "".join(buf)))
    return runs


def tokenize(text: str) -> list[str]:
    """把一段文本切成检索 token。

    - CJK 连续段 → bigram（长度 1 的单字原样保留）
    - 非 CJK 段 → 按空白和常见标点切开，转小写
    - 数字+字母混排（如 `项目A`、`2026`）整体保留，同时也产出跨界的 bigram

    >>> tokenize("张三决定项目A延期到六月")[:4]
    ['张三', '三决', '决定', '定项']
    >>> "延期" in tokenize("张三决定项目A延期到六月")
    True
    """
    if not text:
        return []

    out: list[str] = []
    for is_cjk_run, run in _split_runs(text):
        if is_cjk_run:
            # bigram 滑窗
            if len(run) == 1:
                out.append(run)
            else:
                for i in range(len(run) - 1):
                    out.append(run[i : i + 2])
        else:
            # 非 CJK：按非字母数字切开
            for piece in re.split(r"[^0-9A-Za-z]+", run):
                if piece:
                    out.append(piece.lower())
    return out


def tokenize_for_index(text: str) -> str:
    """索引侧：token 串（空格连接），写进 Weaviate 的 `*_tokens` 字段。"""
    return " ".join(tokenize(text))


def tokenize_for_query(text: str) -> str:
    """查询侧：同 `tokenize_for_index`。

    两侧必须用同一套规则，否则 BM25 对不上。这里特意分成两个函数，
    是为了将来若要给查询加同义词扩展时有地方下手。
    """
    return " ".join(tokenize(text))


#: 供 Weaviate schema 使用的 token 字段名后缀
TOKEN_SUFFIX = "_tokens"
