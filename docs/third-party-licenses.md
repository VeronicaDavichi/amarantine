# 第三方依赖许可证

本项目以 **MIT** 发布（见 [`LICENSE`](../LICENSE)）。这份清单列出运行所需外部组件
及其许可，便于分发时履行义务。

数据来源：`harness/collect_licenses.py`（读**已安装包**的元数据，不是猜），
外加模型/镜像各自的官方声明。**标「待核」的项表示我没有拿到权威声明**，
不以推测填充。

## 结论先说

- Python 依赖里**没有 GPL / AGPL**（`grep -ic gpl deps.json` = 0），没有传染风险
- 有一项 **MPL-2.0**（`certifi`）与一项 **MPL-2.0 AND MIT**（`tqdm`）：
  MPL 是文件级 copyleft，**只要不改它的源码**、以未修改形式依赖即可，无需开源本项目
- **Neo4j Community 是 GPLv3**，但它是**独立进程/容器**，本项目通过 Bolt 协议通信，
  不构成链接或衍生作品。分发时不要把它打进同一个镜像即可（compose 里是独立服务，符合）
- LLM 与 embedding 模型**不进仓库**，由使用者自行拉取，其许可随模型走

## 一、Python 依赖（59 个，按许可归类）

| 许可 | 数量 | 代表包 |
|---|---|---|
| MIT | 27 | fastapi, anyio, alembic, pytest, uvicorn 等 |
| BSD-3-Clause | 12 | httpx, click, httpcore, markupsafe, pycparser |
| Apache-2.0 | 3 | grpcio, openai, pytest-asyncio |
| MIT / Apache 变体 | 6 | aiosqlite(MIT), uvloop(MIT), distro(Apache-2.0), neo4j(Apache-2.0), deprecation(Apache-2.0), cryptography(Apache-2.0 OR BSD-3) |
| MPL-2.0 相关 | 2 | certifi(MPL-2.0), tqdm(MPL-2.0 AND MIT) |
| 其它宽松 | 9 | protobuf(BSD-3), weaviate-client(BSD-3), typing_extensions(PSF-2.0), cffi(MIT-0), greenlet(MIT AND PSF-2.0), packaging(Apache-2.0 OR BSD-2), sniffio(MIT OR Apache-2.0) |

完整清单（含版本）由脚本生成：

```bash
cd back_end && uv run python ../harness/collect_licenses.py
```

## 二、服务与镜像（独立进程，不打进本项目镜像）

| 组件 | 许可 | 用法 | 义务 |
|---|---|---|---|
| Weaviate（`semitechnologies/weaviate`） | BSD-3-Clause | 独立容器，HTTP/GraphQL 通信 | 保留版权声明 |
| Neo4j Community 5.26 | **GPLv3** | 独立容器，Bolt 通信 | **不要静态链接/打包进同一镜像**；本项目符合 |
| text-embeddings-inference（TEI） | Apache-2.0 | 独立容器 | 保留声明与变更说明 |
| bge-m3（embedding 权重） | MIT（BAAI 官方声明，**待核**最新仓库页） | 由 TEI 拉取，**不进仓库** | 随模型分发时保留 |
| bge-reranker-v2-m3 | MIT（同上，**待核**） | 同上 | 同上 |

## 三、可选外部服务

| 组件 | 许可 | 说明 |
|---|---|---|
| [nbnhhsh](https://github.com/itorr/nbnhhsh) | Apache-2.0 | 拼音缩写猜全称；默认**关闭**。只发缩写字符串，不发上下文 |
| jieba | MIT | 中文分词，已随 Python 依赖安装 |

## 四、本项目自身的义务（MIT）

分发（源码或二进制）时需**附带**：

1. `LICENSE` 全文（版权声明 + 许可文本）
2. 上述第三方声明（本文档可作为清单；其中 Apache-2.0 组件建议一并提供其
   `LICENSE` 文本，见各自包目录）

MIT **不含**专利授权与商标授权条款——若将来需要专利保护，
再考虑迁移到 Apache-2.0（迁移需全体贡献者同意，目前贡献者仅作者本人）。

## 五、维护方式

新增依赖时：

```bash
cd back_end && uv run python ../harness/collect_licenses.py   # 看有没有新的 GPL/MPL
```

出现 GPL/AGPL 依赖要停下来讨论——它会改变整个项目的分发条件。
